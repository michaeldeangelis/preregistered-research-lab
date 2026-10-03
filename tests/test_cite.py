import http.client
import json
import urllib.request

import pytest

from rlab import cite
from rlab import questions as Q
from rlab.kernel import Kernel
from rlab.project import init_project, load_project
from rlab.worker import fake_worker

RECORDS = {"14772650": "Smoking and carcinoma of the lung", "15213107": "Mortality in relation to smoking",
           "12345": "Statin use and the risk of cancer: a meta-analysis", "777": "Framingham Heart Study cohort",
           "50001": "p53 and tumour suppression"}


def stub(url, timeout=15.0):
    if "esummary" in url:
        uid = url.rsplit("id=", 1)[1]
        if uid in RECORDS:
            return 200, json.dumps({"result": {uid: {"title": RECORDS[uid]}}})
        return 200, json.dumps({"result": {uid: {"error": "cannot get document summary"}}})
    if "api.crossref.org" in url:
        if "10.1000/real" in url:
            return 200, json.dumps({"message": {"title": ["A real paper about aspirin"]}})
        return 404, ""
    if "doi.org/api/handles" in url:
        return (200, json.dumps({"responseCode": 1})) if "zenodo" in url else (404, json.dumps({"responseCode": 100}))
    return 0, ""


def test_resolve_and_verify():
    assert cite.resolve("14772650", stub)["exists"] is True
    assert cite.resolve("31415926", stub)["exists"] is False
    assert cite.resolve("10.1000/real", stub)["exists"] is True
    assert cite.resolve("10.1000/real.", stub)["exists"] is True  # trailing prose punctuation
    assert cite.resolve("10.1000/fake", stub)["exists"] is False
    assert cite.resolve("10.5281/zenodo.123", stub)["exists"] is True  # DataCite, not in Crossref
    assert cite.resolve("PMID 012345", stub)["exists"] is True  # leading zeros are normalized
    assert cite.resolve("PMID: 12345", stub)["title"].startswith("Statin use")
    assert cite.resolve("2401.00001", lambda u, **k: (0, ""))["exists"] is None  # unreachable
    assert cite.ID_FORMAT.match("arXiv:2401.00001") and cite.ID_FORMAT.match("2401.00001v2")
    assert cite.ID_FORMAT.match("PMID: 1234")  # explicit short PMIDs are fine
    assert not cite.ID_FORMAT.match("１２３４５")  # fullwidth digits are not ids
    assert cite.resolve("１２３４５", stub)["exists"] is False  # and do not raise
    kept, rej = cite.verify_sources([
        {"id": "14772650", "title": "Smoking and carcinoma of the lung"},   # real, right title
        {"id": "15213107", "title": "Volcanic ash dispersal modelling"},    # real id, wrong title
        {"id": "31415926", "title": "Invented trial"},                      # does not resolve
        {"id": "10.1000/real", "title": "A real paper about aspirin"},
    ], stub)
    assert [s["id"] for s in kept] == ["14772650", "10.1000/real"]
    assert {r["id"] for r in rej} == {"15213107", "31415926"}
    kept, rej = cite.verify_sources([{"id": "14772650", "title": "x"}], lambda u, **k: (0, ""))
    assert kept[0]["verified"] is None and not rej  # unreachable resolver does not stall the lab


def test_titles_short_nonascii_and_plural_variants():
    ok = cite.verify_sources([
        {"id": "50001", "title": "p53"},                          # very short claimed title, contained in the real one
        {"id": "12345", "title": "深度学习"},     # non-ASCII title
        {"id": "PMID 777", "title": "Framingham Study"},          # short claimed title, long real one
        {"id": "12345", "title": "Statins and cancer risk"},      # plural vs singular
        {"id": "12345", "title": ""},                             # no claimed title: id only
    ], stub)
    kept, rej = ok
    assert len(kept) == 5 and not rej
    assert [s["verified"] for s in kept] == [True, "id_only", True, True, "id_only"]
    rejected = cite.verify_sources([{"id": "12345", "title": "p53"}], stub)[1]  # comparable and different
    assert len(rejected) == 1 and "title mismatch" in rejected[0]["reject_reason"]


def test_resolver_never_raises():
    def raising(url, timeout=15.0):
        raise http.client.IncompleteRead(b"x", 1000)

    assert cite.resolve("12345", raising)["exists"] is None
    bodies = ["", "<html>captive portal</html>", "[]", "{", '{"result": "x"}', '{"error": "rate limited"}',
              '{"result": {"12345": "text"}}', '{"message": []}']
    for b in bodies:
        assert cite.resolve("12345", lambda u, timeout=15.0, b=b: (200, b))["exists"] in (None, False)
        assert cite.resolve("10.1000/real", lambda u, timeout=15.0, b=b: (200, b))["exists"] in (None, True, False)
    assert cite.resolve("12345", lambda u, timeout=15.0: (200, '{"error": "rate limited"}'))["exists"] is None
    assert cite.resolve("12345", lambda u, timeout=15.0: (200, "{"))["exists"] is None


def test_http_get_swallows_truncated_reads(monkeypatch):
    def boom(req, timeout=None):
        raise http.client.IncompleteRead(b"partial", 1000)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert cite.http_get("https://example.invalid/x") == (0, "")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    init_project(root, "demo", "d")
    pr = load_project(root)
    src = tmp_path / "b.md"
    src.write_text("- Does smoking cause lung cancer? [type:lit]\n- What did the BRIGHTLINE-3 trial find? [type:lit]\n")
    Q.approve_batch(pr, Q.import_batch(pr, src)[0])
    return pr


def lit_worker(call):
    settled = "smoking" in call.prompt.lower()
    ids = [("14772650", "Smoking and carcinoma of the lung"), ("15213107", "Mortality in relation to smoking")] if settled \
        else [("31415926", "BRIGHTLINE-3 results"), ("27182818", "BRIGHTLINE-3 secondary")]
    return {"answered": True, "grade": "high", "summary": "Established.", "contradicting": [], "queries": ["q"],
            "sources": [{"id": i, "title": t, "finding": "f"} for i, t in ids]}


def triage(call):
    return {"type": "lit", "route": "proceed", "reason": "r", "feasible": True, "prior_art": [], "data_sources": [],
            "cheapest_test": "t", "test_family": "f", "value": 3}


def test_kernel_rejects_invented_citations(project, tmp_path):
    assert project.verify_citations is True  # new projects verify by default
    k = Kernel(project, fake_worker({"triage": triage, "lit": lit_worker}), tmp_path / "r.jsonl",
               verifier=lambda sources: cite.verify_sources(sources, stub))
    k.run()
    real, fake = Q.load(project, "Q-0001"), Q.load(project, "Q-0002")
    assert real["verdict"] == "answered" and len(real["sources"]) == 2
    assert fake["verdict"] == "unanswerable" and len(fake["rejected_sources"]) == 2
    assert "rejected sources" in fake["verdict_reason"]
    card = (Q.qdir(project, "Q-0002") / "card.md").read_text()
    assert "Rejected citations" in card


def test_kernel_without_verifier_keeps_old_behavior(project, tmp_path):
    project.verify_citations = False
    Kernel(project, fake_worker({"triage": triage, "lit": lit_worker}), tmp_path / "r.jsonl").run()
    assert Q.load(project, "Q-0002")["verdict"] == "answered"  # format-only check accepts invented ids


def test_verifier_crash_degrades_to_unverified_without_a_second_paid_call(project, tmp_path):
    calls = []

    def counting_lit(call):
        calls.append(call.stage)
        return lit_worker(call)

    def exploding(sources):
        raise http.client.IncompleteRead(b"x", 1000)

    Kernel(project, fake_worker({"triage": triage, "lit": counting_lit}), tmp_path / "r.jsonl", verifier=exploding).run()
    assert calls.count("lit") == 2  # one per question, never a retry
    q = Q.load(project, "Q-0001")
    # fail closed: unverifiable evidence cannot answer a question; it is kept for a retry
    assert q["state"] == "blocked" and "resolver unreachable" in q["blocked_reason"]
    assert all(s["verified"] is None for s in q["sources"])


def test_malformed_results_do_not_wedge_the_kernel(tmp_path):
    from test_kernel import FAKES, PREREG

    root = tmp_path / "p2"
    root.mkdir()
    init_project(root, "demo", "d")
    pr = load_project(root)
    pr.verify_citations = False
    src = tmp_path / "c.md"
    src.write_text("- Does X predict Y? [type:compute]\n")
    Q.approve_batch(pr, Q.import_batch(pr, src)[0])

    def protocol(call):
        (call.cwd / "code").mkdir(exist_ok=True)
        bad = {"effects": {"delta": {"point": 0.08, "lo": 0.04, "hi": 0.12, "n": 400},
                           "diag": {"point": 1, "lo": [1], "hi": {"a": 2}, "n": 3}},
               "controls": {"shuffled": {"point": 0.0, "lo": -0.02, "hi": 0.02}}, "seeds": 1}
        (call.cwd / "code" / "analysis.py").write_text(
            "import argparse, json, pathlib\na = argparse.ArgumentParser(); a.add_argument('--out'); args = a.parse_args()\n"
            f"pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)\npathlib.Path(args.out).write_text(json.dumps({json.dumps(bad)}))\n")
        return PREREG

    Kernel(pr, fake_worker({**FAKES, "protocol": protocol}), tmp_path / "r2.jsonl").run()
    q = Q.load(pr, "Q-0001")
    assert q["state"] == "reported" and q["verdict"] in ("pass", "provisional", "invalid")
