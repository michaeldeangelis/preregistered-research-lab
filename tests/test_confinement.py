"""Sealed reviewer, restricted search, holdout confirmation, online FDR, parallel runs."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from rlab import fdr, sandbox
from rlab import questions as Q
from rlab.kernel import RUNTIME, SEARCH_APIS, Kernel
from rlab.worker import fake_worker
from test_kernel import FAKES, GOOD, lab, results_script, script, text_of, verdicts  # noqa: F401

needs_sandbox = pytest.mark.skipif(not sandbox.available(), reason="sandbox-exec not available")


def _split(mode, salt="s", units=range(1000)):
    code = ("import rlab_split, json; m = rlab_split.keep(range(1000)); "
            "print(json.dumps([i for i, k in zip(range(1000), m) if k]))")
    env = {**os.environ, "PYTHONPATH": str(RUNTIME), "RLAB_SPLIT": mode, "RLAB_SPLIT_SALT": salt}
    return set(json.loads(subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                                         text=True, check=True).stdout))


def test_split_is_disjoint_complete_and_salted():
    disc, hold = _split("discovery"), _split("holdout")
    assert not disc & hold and len(disc | hold) == 1000 and 200 < len(hold) < 400
    assert _split("dev", salt="") != disc  # the worker's dev split is not the kernel's
    assert len(_split("all")) == 1000


def test_web_search_is_off_by_default_and_apis_are_reachable(lab):
    project, registry = lab
    tools = Kernel(project, fake_worker(FAKES), registry)._web_tools()
    assert "WebSearch" not in tools and all(f"WebFetch(domain:{d})" in tools for d in SEARCH_APIS)
    project.web_search = True
    assert "WebSearch" in Kernel(project, fake_worker(FAKES), registry)._web_tools()


def _env_script(discovery: dict, holdout: dict, split=True) -> str:
    """Analysis stub whose results depend on the split the kernel runs it on."""
    lines = ["import argparse, json, os, pathlib",
             "a = argparse.ArgumentParser(); a.add_argument('--out'); args = a.parse_args()",
             f"r = json.loads({json.dumps(json.dumps(holdout))}) if os.environ.get('RLAB_SPLIT') == 'holdout' "
             f"else json.loads({json.dumps(json.dumps(discovery))})"]
    if split:
        lines += ["import rlab_split", "rlab_split.keep(range(400))", "r['split'] = rlab_split.summary()"]
    lines += ["pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)",
              "pathlib.Path(args.out).write_text(json.dumps(r))"]
    return "\n".join(lines) + "\n"


NULL = {**GOOD, "effects": {"delta": {"point": 0.0, "lo": -0.03, "hi": 0.03, "n": 120}}}
WEAK = {**GOOD, "effects": {"delta": {"point": 0.04, "lo": 0.004, "hi": 0.076, "n": 400}}}  # p ~ 0.015
WEAK_H = {**GOOD, "effects": {"delta": {"point": 0.041, "lo": 0.005, "hi": 0.077, "n": 170}}}


def _with_script(body):
    def fn(call):
        out = script(call)
        if "citation count" in text_of(call):
            (call.cwd / "code" / "analysis.py").write_text(body)
        return out
    return {**FAKES, "script": fn}


def test_discovery_pass_that_fails_holdout_is_provisional(lab):
    project, registry = lab
    Kernel(project, fake_worker(_with_script(_env_script(GOOD, NULL))), registry).run()
    q = Q.load(project, "Q-0002")
    assert q["verdict"] == "provisional" and "did not confirm" in q["verdict_reason"] and q["confirmed"] is False
    assert q["holdout_verdict"] in ("kill", "provisional")


def test_unsplit_analysis_cannot_be_confirmed(lab):
    project, registry = lab
    Kernel(project, fake_worker(_with_script(results_script(GOOD, split=False))), registry).run()
    q = Q.load(project, "Q-0002")
    assert q["verdict"] == "provisional" and "rlab_split" in q["verdict_reason"]


def test_confirmed_pass_records_lord_level(lab):
    project, registry = lab
    project.fdr_mode = "lord"
    Kernel(project, fake_worker(FAKES), registry).run()
    q = Q.load(project, "Q-0002")
    assert q["verdict"] == "pass" and q["confirmed"] and q["fdr_t"] == 1 and q["fdr_p"] <= q["fdr_alpha_t"]
    assert "Confirmed on held-out units: True" in (Q.qdir(project, "Q-0002") / "card.md").read_text()


def test_nominal_pass_can_fail_online_fdr(lab):
    project, registry = lab
    project.fdr_mode = "lord"
    Kernel(project, fake_worker(_with_script(_env_script(WEAK, WEAK_H))), registry).run()
    q = Q.load(project, "Q-0002")
    assert q["holdout_verdict"] == "pass" and q["verdict"] == "provisional" and "online FDR" in q["verdict_reason"]


def test_lord_levels_spend_and_earn_wealth(tmp_path):
    a = 0.05
    assert fdr.level(1, [], a) < a / 2 and fdr.level(50, [], a) < fdr.level(1, [], a)
    assert fdr.level(2, [1], a) > fdr.level(2, [], a)  # a discovery earns wealth
    stream = tmp_path / "fdr.jsonl"
    first = fdr.test(stream, "Q-1", 1e-6, a)
    assert first["reject"] and fdr.test(stream, "Q-1", 0.9, a) == first  # idempotent per question
    assert not fdr.test(stream, "Q-2", 0.04, a)["reject"]


@needs_sandbox
def test_parallel_matches_serial_and_overlaps(lab):
    project, registry = lab
    project.llm_concurrency, project.cpu_concurrency = 3, 2
    live, peak = [0], [0]
    lock = threading.Lock()

    def slow(fn):
        def run(call):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.2)
            try:
                return fn(call)
            finally:
                with lock:
                    live[0] -= 1
        return run

    k = Kernel(project, fake_worker({s: slow(f) for s, f in FAKES.items()}), registry)
    assert k.parallel()
    k.run()
    v = verdicts(project)
    assert v["Does citation count "] == ("reported", "pass")
    assert v["Overclaim trap: does"][1] == "provisional" and v["Leaky trap: does the"][1] == "invalid"
    assert v["Is aspirin associate"] == ("reported", "answered") and v["Does aspirin reduce "][1] == "duplicate"
    assert 1 < peak[0] <= 3


def test_split_normalises_unit_types_and_index():
    code = ("import rlab_split, json, pandas as pd\n"
            "a = list(rlab_split.keep([5, 6, 7])); b = list(rlab_split.keep([5.0, 6.0, 7.0]))\n"
            "df = pd.DataFrame({'x': range(6)}, index=[10, 11, 12, 13, 14, 15])\n"
            "sub = df.loc[rlab_split.keep(df.index)]\n"
            "print(json.dumps([a == b, len(sub) <= 6]))")
    env = {**os.environ, "PYTHONPATH": str(RUNTIME), "RLAB_SPLIT": "discovery", "RLAB_SPLIT_SALT": "s"}
    py = Path.home() / ".dev/research-lab/envs/base/bin/python"
    out = subprocess.run([str(py if py.exists() else sys.executable), "-c", code], env=env, capture_output=True, text=True)
    assert json.loads(out.stdout) == [True, True], out.stderr


def test_min_n_is_scaled_to_each_split(lab):
    project, registry = lab
    k = Kernel(project, fake_worker(FAKES), registry)
    rule = {"metric": "d", "min_n": 569}
    assert k._scaled(rule, "discovery")["min_n"] == int(569 * 0.7 * 0.9)
    assert k._scaled(rule, "holdout")["min_n"] == int(569 * 0.3 * 0.9)
    assert k._scaled(rule, "all")["min_n"] == 569


def test_batch_bh_decides_once_the_batch_closes(lab):
    project, registry = lab
    seen = {}

    def script_two(call):
        out = script(call)
        t = text_of(call)
        if "citation count" in t:
            (call.cwd / "code" / "analysis.py").write_text(_env_script(GOOD, WEAK_H))  # holdout p ~ 0.013
        if "Overclaim" in t:  # turn the overclaim trap into a second, strong confirmed test
            (call.cwd / "code" / "analysis.py").write_text(_env_script(GOOD, {**GOOD, "effects": {"delta": {
                "point": 0.07, "lo": 0.03, "hi": 0.11, "n": 120}}}))
        return out

    k = Kernel(project, fake_worker({**FAKES, "script": script_two}), registry)
    for _ in range(40):
        k.tick()
        for qid in ("Q-0002", "Q-0003"):
            st = Q.load(project, qid)["state"]
            seen.setdefault(qid, []).append(st)
        if all(Q.load(project, i)["state"] == "reported" for i in ("Q-0002", "Q-0003")):
            break
    a, b = Q.load(project, "Q-0002"), Q.load(project, "Q-0003")
    assert "awaiting_batch" in seen["Q-0002"] + seen["Q-0003"]
    assert a["fdr_method"] == b["fdr_method"] == "BH" and a["fdr_m"] == b["fdr_m"] == 2
    assert a["verdict"] == b["verdict"] == "pass"  # each passes at BH 0.05 across the batch's two tests
    assert "Benjamini-Hochberg across 2" in (Q.qdir(project, "Q-0002") / "card.md").read_text()
    rec = [json.loads(l) for l in (project.qbank / "fdr_batches.jsonl").read_text().splitlines()]
    assert len(rec) == 1 and len(rec[0]["tests"]) == 2


def test_bh_adjusted_values():
    assert [round(x, 4) for x in fdr.bh_adjusted([0.01, 0.04, 0.03, 0.5])] == [0.04, 0.0533, 0.0533, 0.5]
    assert fdr.bh_adjusted([]) == []
