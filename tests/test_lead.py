"""The project lead acts only within its charter; the kernel carries out every action."""

import json
import subprocess

from rlab import lead
from rlab import questions as Q
from rlab.kernel import Kernel
from rlab.project import init_project, load_project
from rlab.worker import fake_worker
from test_kernel import FAKES

BANK = """# bank
- Does year-one citation count predict clinical use? [type:compute]
- Does impact factor predict retraction? [type:compute]
- Do agents overclaim in their reports? [type:compute] [needs:llm]
- Is aspirin associated with lower colorectal cancer risk? [type:lit]
- Does sample size predict replication? [type:compute]
"""


def make(tmp_path, **charter):
    root = tmp_path / "proj"
    root.mkdir()
    init_project(root, "demo", "test")
    p = load_project(root)
    p.verify_citations = False
    p.llm_concurrency = p.cpu_concurrency = 1
    p.lead = {"enabled": True, **charter}
    src = tmp_path / "bank.md"
    src.write_text(BANK)
    Q.import_batch(p, src)
    return p


class FakePopen:
    def __init__(self, *a, **k):
        self.pid = 4242
        FakePopen.calls.append(a[0])
    calls = []


def lead_says(**actions):
    base = {"digest": "Started the strongest open questions.", "approve": [], "park": [], "requeue": [], "prioritise": [],
            "publish_private": [], "run": False, "needs_mike": []}
    return fake_worker({"lead": lambda call: {**base, **actions}})


def test_lead_approves_within_caps_and_never_past_a_setting(tmp_path):
    p = make(tmp_path, max_approvals_per_cycle=2)
    out = lead.cycle(p, lead_says(approve=[{"id": i, "why": "w"} for i in ("Q-0003", "Q-0001", "Q-0002", "Q-0005")],
                                  run=True), popen=FakePopen)
    st = {q["id"]: q["state"] for q in Q.all_questions(p)}
    assert st["Q-0003"] == "imported" and "project setting" in " ".join(out["refused"])
    assert [st[i] for i in ("Q-0001", "Q-0002", "Q-0005")].count("approved") == 2
    assert "cap reached" in " ".join(out["refused"]) and any("started lab run" in d for d in out["done"])
    assert (p.qbank / "lead" / "digest.md").exists() and json.loads((p.qbank / "lead" / "packet.json").read_text())["waiting"]


def test_lead_parks_and_reports_owner_items(tmp_path):
    p = make(tmp_path)
    lead.cycle(p, lead_says(park=[{"id": "Q-0004", "why": "answered by meta-analyses"}],
                            needs_mike=[{"item": "turn on allow_llm_calls for demo", "why": "Q-0003 waits on it"}]))
    q = Q.load(p, "Q-0004")
    assert q["state"] == "decided" and q["verdict"] == "parked" and "meta-analyses" in q["verdict_reason"]
    assert "allow_llm_calls" in (p.qbank / "NEEDS_MIKE.md").read_text()


def test_requeue_once_and_never_an_integrity_block(tmp_path):
    p = make(tmp_path)
    a, b = Q.load(p, "Q-0001"), Q.load(p, "Q-0002")
    Q.set_state(p, a, "blocked", blocked_from="protocol", blocked_reason="protocol: 3 attempts failed",
                stage_attempts={"protocol": 3})
    Q.set_state(p, b, "blocked", blocked_from="ran", blocked_reason="preregistration, script or results changed after the run")
    out = lead.cycle(p, lead_says(requeue=[{"id": "Q-0001", "why": "transient"}, {"id": "Q-0002", "why": "try"}]))
    a = Q.load(p, "Q-0001")
    assert a["state"] == "protocol" and a["requeues"] == 1 and a["stage_attempts"]["protocol"] == 1
    assert Q.load(p, "Q-0002")["state"] == "blocked" and "integrity" in " ".join(out["refused"])
    Q.set_state(p, a, "blocked", blocked_from="protocol", blocked_reason="protocol: 3 attempts failed")
    out = lead.cycle(p, lead_says(requeue=[{"id": "Q-0001", "why": "again"}]))
    assert "already retried once" in " ".join(out["refused"])


def test_publishing_needs_the_charter(tmp_path):
    p = make(tmp_path)
    q = Q.load(p, "Q-0001")
    Q.set_state(p, q, "reported", repo="repo")
    calls = []
    out = lead.cycle(p, lead_says(publish_private=[{"id": "Q-0001"}]),
                     publish=lambda repo, public: calls.append(public) or (True, "url"))
    assert not calls and "does not allow" in " ".join(out["refused"])
    p.lead["may_publish_private"] = True
    lead.cycle(p, lead_says(publish_private=[{"id": "Q-0001"}]),
               publish=lambda repo, public: calls.append(public) or (True, "https://github.com/x/y"))
    assert calls == [False] and Q.load(p, "Q-0001")["published"] == "https://github.com/x/y"


def test_disabled_lead_does_nothing_and_missing_analyst_fails(tmp_path):
    p = make(tmp_path)
    p.lead["enabled"] = False
    assert "skipped" in lead.cycle(p, lead_says(approve=[{"id": "Q-0001", "why": "w"}]))
    p.lead["enabled"] = True
    out = lead.cycle(p, fake_worker({"lead": lambda c: {"digest": "d", "approve": [], "park": [], "requeue": [],
                                                       "prioritise": [], "publish_private": [], "run": False,
                                                       "needs_mike": []}}, team=False))
    assert "analyst" in out["error"]


def test_lead_and_kernel_together(tmp_path):
    """The lead approves, the kernel runs: the owner does nothing between import and verdict."""
    p = make(tmp_path)
    lead.cycle(p, lead_says(approve=[{"id": "Q-0001", "why": "w"}, {"id": "Q-0004", "why": "w"}]))
    Kernel(p, fake_worker(FAKES), tmp_path / "reg.jsonl").run()
    assert Q.load(p, "Q-0004")["state"] == "reported" and Q.load(p, "Q-0001")["state"] in ("reported", "blocked")


def test_packet_lists_every_waiting_question(tmp_path):
    p = make(tmp_path)
    src = tmp_path / "more.md"
    src.write_text("".join(f"- Extra question {i}? [type:compute]\n" for i in range(150)))
    Q.import_batch(p, src)
    assert len(lead.packet(p)["waiting"]) == 155
