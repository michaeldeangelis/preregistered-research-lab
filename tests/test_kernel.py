import json
import subprocess
from pathlib import Path

import pytest

from rlab import questions as Q
from rlab.kernel import Kernel
from rlab.project import init_project, load_project
from rlab.util import read_json
from rlab.worker import WorkerResult, fake_worker

IMPORT = """# batch
- Is aspirin associated with lower colorectal cancer risk? [type:lit]
- Does citation count in year 1 predict clinical use by year 5? [type:compute]
- Overclaim trap: does journal impact factor predict retraction? [type:compute]
- Leaky trap: does the outcome predict itself? [type:compute]
- Does aspirin reduce colorectal cancer risk? [type:lit]
"""


def results_script(results: dict, split: bool = True, vary: bool = True) -> str:
    """Analysis stub: fixed results; with split, units go through the kernel's rlab_split, and (vary) the
    held-out units give slightly different numbers, as a real split would."""
    lines = ["import argparse, json, os, pathlib",
             "a = argparse.ArgumentParser(); a.add_argument('--out'); args = a.parse_args()",
             f"r = json.loads({json.dumps(json.dumps(results))})"]
    if split:
        lines += ["import rlab_split", "rlab_split.keep(range(400))", "r['split'] = rlab_split.summary()"]
        if vary:
            lines += ["if os.environ.get('RLAB_SPLIT') == 'holdout':",
                      "    for b in r['effects'].values():",
                      "        b.update({k: round(v * 0.97, 6) for k, v in b.items() if k in ('point', 'lo', 'hi')})"]
    lines += ["pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)",
              "pathlib.Path(args.out).write_text(json.dumps(r))"]
    return "\n".join(lines) + "\n"


GOOD = {"effects": {"delta": {"point": 0.08, "lo": 0.04, "hi": 0.12, "n": 400}},
        "controls": {"shuffled": {"point": 0.0, "lo": -0.02, "hi": 0.02}}, "seeds": 1}
OVERCLAIM = {"effects": {"delta": {"point": 0.08, "lo": -0.01, "hi": 0.17, "n": 400}},
             "controls": {"shuffled": {"point": 0.0, "lo": -0.02, "hi": 0.02}}, "seeds": 1}
LEAKY = {"effects": {"delta": {"point": 0.3, "lo": 0.25, "hi": 0.35, "n": 400}},
         "controls": {"shuffled": {"point": 0.29, "lo": 0.24, "hi": 0.34}}, "seeds": 1}
PREREG = {"hypothesis": "h", "family": "fam", "alpha": 0.05,
          "decision_rule": {"metric": "delta", "comparator": "gt", "threshold": 0.0, "ci_must_exclude": True,
                            "min_n": 100, "null_value": 0.0},
          "analysis_plan": "one logistic model, fixed covariates", "baselines": ["prevalence", "citations_only"],
          "negative_controls": ["shuffled"], "data": ["x"], "seeds": 1, "notes": "n"}


def text_of(call):
    return call.prompt.split("\n")[1] if "\n" in call.prompt else call.prompt


def triage(call):
    t = text_of(call)
    dup = "Does aspirin reduce" in t
    return {"type": "lit" if "aspirin" in t else "compute", "route": "duplicate" if dup else "proceed",
            "reason": "r", "feasible": True, "prior_art": [], "data_sources": ["pubmed"], "cheapest_test": "t",
            "test_family": "f", "value": 3, **({"duplicate_of": "Q-0001"} if dup else {})}


def lit(call):
    return {"answered": True, "grade": "moderate", "summary": "meta-analyses show lower risk",
            "sources": [{"id": "31234567", "title": "Aspirin and CRC", "finding": "RR 0.8"}],
            "contradicting": [], "queries": ["aspirin colorectal"]}


def protocol(call):
    return PREREG


def script(call):
    t = text_of(call)
    results = OVERCLAIM if "Overclaim" in t else LEAKY if "Leaky" in t else GOOD
    (call.cwd / "code").mkdir(exist_ok=True)
    (call.cwd / "code" / "analysis.py").write_text(results_script(results))
    return {"tested": True, "deviations": [], "notes": ""}


def review(call):
    return {"conforms": True, "severity": "none", "issues": [], "checked": ["metric"]}


WRITEUP = {"title": "Citation count and later use", "repo_name": "citation-count-later-use",
           "description": "Year-one citations predicted clinical use in held-out papers.",
           "topics": ["research", "metascience", "citations", "preregistration", "bootstrap"],
           "lead": "I tested whether early citations predict later clinical use. They did, in held-out papers too.",
           "abstract": "I tested whether papers cited more in their first year are used more in clinics later.\n\n"
                       "I fixed the rule before the run and checked the result on papers the analysis never saw.\n\n"
                       "This does not show that citations cause use.",
           "result_heading": "Year-one citations", "result_body": "The delta was 0.08 (0.04 to 0.12).",
           "reproduce_notes": "CPU is enough.", "data_acknowledgements": ["PubMed, public domain."]}


def writeup(call):
    return dict(WRITEUP)


FAKES = {"triage": triage, "lit": lit, "protocol": protocol, "script": script, "review": review, "writeup": writeup}


@pytest.fixture
def lab(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    init_project(root, "demo", "test project")
    project = load_project(root)
    project.verify_citations = False  # no network in tests; see test_cite.py
    project.llm_concurrency = project.cpu_concurrency = 1  # deterministic order; see the parallel tests
    src = tmp_path / "b1.md"
    src.write_text(IMPORT)
    batch, ids = Q.import_batch(project, src)
    Q.approve_batch(project, batch)
    return project, tmp_path / "registry.jsonl"


def verdicts(project):
    return {q["text"][:20]: (q["state"], q.get("verdict")) for q in Q.all_questions(project)}


def test_end_to_end_fake(lab):
    project, registry = lab
    Kernel(project, fake_worker(FAKES), registry).run()
    v = verdicts(project)
    assert v["Is aspirin associate"] == ("reported", "answered")
    assert v["Does citation count "] == ("reported", "pass")
    assert v["Overclaim trap: does"][1] == "provisional"  # kernel refuses the overclaim
    assert v["Leaky trap: does the"][1] == "invalid"  # non-null control
    assert v["Does aspirin reduce "] == ("reported", "duplicate")
    claims = [json.loads(l) for l in registry.read_text().splitlines()]
    assert {c["verdict"] for c in claims} == {"answered", "pass", "provisional", "invalid"}  # duplicate not registered
    q2 = Q.load(project, "Q-0002")
    card = (Q.qdir(project, "Q-0002") / "card.md").read_text()
    assert "Verdict: pass" in card and "delta" in card and q2["reproduced"] is True
    assert (project.qbank / "digest.md").read_text().count("reported: 5") == 1
    assert (project.qbank / "ledger.md").exists()


def test_import_stops_at_notes_and_reads_family():
    items = Q.parse_import("# bank\n## fam-a: theme\n- Q1? [type:ml] [family:fam-a] [decision:d]\n"
                           "## Notes\n- excluded GPU ideas\n")
    assert items == [{"text": "Q1?", "type": "ml", "family": "fam-a", "decision": "d"}]


def test_import_records_needs_tag(lab, tmp_path):
    project, _ = lab
    src = tmp_path / "n.md"
    src.write_text("- Do embeddings converge across domains? [type:ml] [needs:torch, sklearn] [decision:d]\n")
    _, [qid] = Q.import_batch(project, src)
    assert Q.load(project, qid)["needs"] == ["torch", "sklearn"]
    src.write_text("- Is X open? [type:compute] [prior:arXiv:2401.00001; doi:10.1/x] [gap:no one used Y] [decision:d]\n")
    _, [qid2] = Q.import_batch(project, src)
    q = Q.load(project, qid2)
    assert q["prior_work"].startswith("arXiv:2401") and q["gap"] == "no one used Y" and q["text"] == "Is X open?"


def test_lit_answered_needs_a_real_source(lab):
    project, registry = lab
    fakes = {**FAKES, "lit": lambda c: {**lit(c), "sources": [{"id": "made up", "title": "x", "finding": "y"}]}}
    Kernel(project, fake_worker(fakes), registry).run()
    assert Q.load(project, "Q-0001")["verdict"] == "unanswerable"


def test_schema_error_is_retried_with_feedback(lab):
    project, registry = lab
    seen = []

    def flaky(call):
        seen.append(call.prompt)
        out = triage(call)
        if len(seen) == 1:
            out.pop("value")
        return out

    Kernel(project, fake_worker({**FAKES, "triage": flaky}), registry).run()
    q1 = [p for p in seen if "Question Q-0001:" in p]
    assert len(q1) == 2 and "missing required 'value'" in q1[1]
    assert Q.load(project, "Q-0001")["state"] == "reported"


def test_crashing_worker_blocks_after_three_attempts(lab):
    project, registry = lab

    def boom(call):
        raise RuntimeError("worker died")

    Kernel(project, fake_worker({**FAKES, "triage": boom}), registry).run()
    q = Q.load(project, "Q-0001")
    assert q["state"] == "blocked" and "triage" in q["blocked_reason"]
    assert (Q.qdir(project, "Q-0001") / "stages" / "triage.failed.json").exists()


def test_question_cap_blocks(lab):
    project, registry = lab
    project.question_usd = 0.2  # below triage budget
    Kernel(project, fake_worker(FAKES), registry).run()
    assert all(q["state"] == "blocked" and "cap" in q["blocked_reason"] for q in Q.all_questions(project))


def test_resume_skips_finished_stages(lab):
    project, registry = lab
    calls = []
    counting = {k: (lambda f: (lambda c: (calls.append(c.stage), f(c))[1]))(f) for k, f in FAKES.items()}
    k = Kernel(project, fake_worker(counting), registry)
    k.tick()  # one pass: every question advances one stage
    first = len(calls)
    Kernel(project, fake_worker(counting), registry).run()  # fresh kernel resumes from files
    assert calls.count("triage") == 5 and first == 5


def test_writes_outside_question_folder_block(lab):
    project, registry = lab
    subprocess.run(["git", "init", "-q"], cwd=project.root, check=True)

    def sneaky(call):
        (project.root / "stray.txt").write_text("x")
        return triage(call)

    Kernel(project, fake_worker({**FAKES, "triage": sneaky}), registry).tick()
    q = Q.load(project, "Q-0001")
    assert q["state"] == "blocked" and "outside" in q["blocked_reason"]


def test_tampered_prereg_blocks_run(lab):
    project, registry = lab
    k = Kernel(project, fake_worker(FAKES), registry)
    for _ in range(3):  # triage, rule, script
        k.tick()
    q = Q.load(project, "Q-0002")
    assert q["state"] == "prereg_frozen"
    pre = Q.qdir(project, "Q-0002") / "prereg.json"
    data = read_json(pre)
    data["decision_rule"]["threshold"] = -1
    pre.write_text(json.dumps(data))
    k.tick()
    assert Q.load(project, "Q-0002")["state"] == "blocked"


def _stage_calls(project, registry, fakes=None):
    calls = {}
    rec = {k: (lambda k, f: (lambda c: (calls.setdefault(k, []).append(c), f(c))[1]))(k, f)
           for k, f in (fakes or FAKES).items()}
    Kernel(project, fake_worker(rec), registry).run()
    return calls


def test_rule_stage_has_no_shell_or_local_reads(lab):
    project, registry = lab
    calls = _stage_calls(project, registry)
    for c in calls["protocol"]:
        assert not any(t.startswith(("Bash", "Read", "Write", "Edit")) for t in c.allowed_tools)
        assert "Bash" in c.disallowed_tools and "Read" in c.disallowed_tools
    assert any(t.startswith("Bash(") for t in calls["script"][0].allowed_tools)


def test_script_stage_cannot_change_the_rule(lab):
    project, registry = lab

    def rewrites_rule(call):
        out = script(call)
        (call.cwd / "prereg.json").write_text(json.dumps({**PREREG, "decision_rule": {**PREREG["decision_rule"], "threshold": -9}}))
        return out

    Kernel(project, fake_worker({**FAKES, "script": rewrites_rule}), registry).run()
    q = Q.load(project, "Q-0002")
    assert q["state"] == "blocked" and "preregistration" in q["blocked_reason"]


def test_reviewer_sees_rule_without_author_reasoning(lab):
    project, registry = lab
    calls = _stage_calls(project, registry)
    rd = Q.qdir(project, "Q-0002") / "review"
    rule = read_json(rd / "rule.json")
    assert rule["decision_rule"] == PREREG["decision_rule"] and "hypothesis" not in rule and "notes" not in rule
    assert calls["review"][0].cwd == rd and (rd / "code" / "analysis.py").exists() and (rd / "results.json").exists()


def test_no_git_repo_blocks_instead_of_running_unchecked(lab):
    project, registry = lab
    import shutil
    shutil.rmtree(project.root / ".git")
    Kernel(project, fake_worker(FAKES), registry).run()
    assert all(q["state"] == "blocked" and "write boundary" in q["blocked_reason"] for q in Q.all_questions(project))


def test_rerun_must_match_controls_not_only_effects(lab):
    project, registry = lab

    def drifting(call):
        out = script(call)
        if "citation count" in text_of(call):  # effects fixed, control drifts between runs
            (call.cwd / "code" / "analysis.py").write_text(
                "import argparse, json, pathlib, random\n"
                "a = argparse.ArgumentParser(); a.add_argument('--out'); args = a.parse_args()\n"
                "r = {'effects': {'delta': {'point': 0.08, 'lo': 0.04, 'hi': 0.12, 'n': 400}},\n"
                "     'controls': {'shuffled': {'point': random.random() / 100, 'lo': -0.02, 'hi': 0.02}}, 'seeds': 1}\n"
                "pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)\n"
                "pathlib.Path(args.out).write_text(json.dumps(r))\n")
        return out

    Kernel(project, fake_worker({**FAKES, "script": drifting}), registry).run()
    q = Q.load(project, "Q-0002")
    assert q["reproduced"] is False and q["verdict"] == "provisional"


def test_unrerun_long_analysis_cannot_pass(lab):
    project, registry = lab
    project.reproduce_max_s = 0.0
    Kernel(project, fake_worker(FAKES), registry).run()
    q = Q.load(project, "Q-0002")
    assert q["reproduced"] is None and q["verdict"] == "provisional" and "not rerun" in q["verdict_reason"]


def test_unverifiable_sources_do_not_answer(lab):
    project, registry = lab
    unreachable = lambda sources: ([{**s, "verified": None} for s in sources], [])
    Kernel(project, fake_worker(FAKES), registry, verifier=unreachable).run()
    q = Q.load(project, "Q-0001")
    assert q["state"] == "blocked" and "resolver unreachable" in q["blocked_reason"]
    ok = lambda sources: ([{**s, "verified": True} for s in sources], [])
    q["state"] = "lit"
    Q.save(project, q)
    Kernel(project, fake_worker(FAKES), registry, verifier=ok).run()
    assert Q.load(project, "Q-0001")["verdict"] == "answered"


def test_approve_single_questions(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    init_project(root, "p")
    project = load_project(root)
    src = tmp_path / "b.md"
    src.write_text(IMPORT)
    Q.import_batch(project, src)
    ok, skipped = Q.approve_ids(project, ["Q-0002", "Q-0009"])
    assert ok == ["Q-0002"] and skipped == ["Q-0009: not found"]
    assert [q["state"] for q in Q.all_questions(project)].count("approved") == 1
    assert Q.approve_ids(project, ["Q-0002"])[1] == ["Q-0002: already approved"]


def test_stages_run_as_teams_and_every_specialist_must_report(lab):
    from rlab import teams
    project, registry = lab
    calls = _stage_calls(project, registry)
    for stage, roles in teams.TEAMS.items():
        for c in calls.get(stage, []):
            assert set(c.agents) == set(roles) and "Agent" in c.allowed_tools and "consult each" in c.prompt.lower()
    assert calls["review"][0].agents["code_reviewer"]["tools"] == ["Read", "Grep", "Glob", "Bash"]

    def half_team(call):
        out = protocol(call)
        return {**out, "team": []}  # skipped the statistician

    root2 = project.root.parent / "p2"
    root2.mkdir()
    init_project(root2, "p2")
    p2 = load_project(root2)
    p2.verify_citations = False
    p2.llm_concurrency = p2.cpu_concurrency = 1
    src = project.root.parent / "b2.md"
    src.write_text("- Does citation count in year 1 predict clinical use? [type:compute]\n")
    batch, _ = Q.import_batch(p2, src)
    Q.approve_batch(p2, batch)
    Kernel(p2, fake_worker({**FAKES, "protocol": half_team}), registry).run()
    q = Q.load(p2, "Q-0001")
    assert q["state"] == "blocked" and "statistician" in q["blocked_reason"]
