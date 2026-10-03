"""Attacks from the adversarial review of the sandbox, split and FDR stream; each must fail."""

import json
import os
import subprocess
import sys

import pytest

from rlab import fdr, sandbox
from rlab import questions as Q
from rlab.kernel import RUNTIME, Kernel
from rlab.worker import fake_worker
from test_confinement import NULL, _env_script, _with_script, needs_sandbox
from test_kernel import FAKES, GOOD, lab, results_script, verdicts  # noqa: F401

ATTEMPT = (
    "import os, pathlib\n"
    "def t(name, fn):\n"
    "    try:\n"
    "        fn(); print(name + ':yes')\n"
    "    except OSError:\n"
    "        print(name + ':no')\n")


def _probe(prof, code):
    return subprocess.run(sandbox.wrap([sys.executable, "-c", ATTEMPT + code], prof),
                          capture_output=True, text=True).stdout.split()


@needs_sandbox
def test_worker_profile_default_denies_writes_and_renames(lab, tmp_path):
    project, registry = lab
    k = Kernel(project, fake_worker(FAKES), registry)
    qd = Q.qdir(project, "Q-0002").resolve()
    (qd / "prereg.json").write_text("{}")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    code = (f"qd = pathlib.Path({str(qd)!r})\n"
            "t('code', lambda: (qd / 'code').mkdir(exist_ok=True) or (qd / 'code' / 'a.py').write_text('x'))\n"
            f"t('outside', lambda: pathlib.Path({str(outside)!r}, 'x').write_text('x'))\n"
            f"t('root', lambda: pathlib.Path({str(project.root)!r}, 'x').write_text('x'))\n"
            "t('rename_prereg', lambda: os.rename(qd / 'prereg.json', qd / 'p2.json'))\n"
            "t('rename_qdir', lambda: os.rename(qd, qd.parent / 'moved'))\n"
            "t('settings', lambda: open(pathlib.Path.home() / '.claude' / 'settings.json', 'a').close())\n"
            f"t('runtime', lambda: open({str(RUNTIME / 'rlab_split.py')!r}, 'a').close())\n")
    assert _probe(k._worker_profile(Q.load(project, "Q-0002"), "script"), code) == [
        "code:yes", "outside:no", "root:no", "rename_prereg:no", "rename_qdir:no", "settings:no", "runtime:no"]


@needs_sandbox
def test_reviewer_is_sealed_from_author_reasoning_and_transcripts(lab):
    project, registry = lab
    calls = []

    def review(call):
        calls.append(call)
        return FAKES["review"](call)

    Kernel(project, fake_worker({**FAKES, "review": review}), registry).run()
    call = calls[0]
    qd = Q.qdir(project, "Q-0002").resolve()
    assert call.cwd == qd / "review"
    assert "hypothesis" not in json.loads((qd / "review" / "rule.json").read_text())
    import rlab.kernel as K
    author_cfg = K.WORKER_HOME / project.name / "Q-0002" / "author"
    tdir = sandbox.transcripts_dir(qd, author_cfg)
    made = not tdir.exists()
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "author.jsonl").write_text("hypothesis")
    code = (f"qd = pathlib.Path({str(qd)!r})\n"
            "t('prereg', lambda: (qd / 'prereg.json').read_text())\n"
            "t('rename_prereg', lambda: os.rename(qd / 'prereg.json', qd / 'review' / 'work' / 'p.json'))\n"
            "t('triage', lambda: (qd / 'triage.json').read_text())\n"
            "t('rule', lambda: (qd / 'review' / 'rule.json').read_text())\n"
            "t('code', lambda: (qd / 'review' / 'code' / 'analysis.py').read_text())\n"
            f"t('transcripts', lambda: os.listdir({str(tdir)!r}))\n"
            f"t('rename_cfg', lambda: os.rename({str(author_cfg)!r}, qd / 'review' / 'work' / 'cfg'))\n"
            "t('real_projects', lambda: os.listdir(pathlib.Path.home() / '.claude' / 'projects'))\n"
            "t('work', lambda: (qd / 'review' / 'work' / 'x').write_text('x'))\n")
    try:
        assert _probe(call.sandbox, code) == ["prereg:no", "rename_prereg:no", "triage:no", "rule:yes", "code:yes",
                                              "transcripts:no", "rename_cfg:no", "real_projects:no", "work:yes"]
    finally:
        (tdir / "author.jsonl").unlink()
        if made:
            tdir.rmdir()


@needs_sandbox
def test_analysis_run_cannot_read_salt_or_holdout_or_touch_code(lab):
    import rlab.kernel as K
    project, registry = lab
    held = project.root / "holdout_data"
    held.mkdir()
    (held / "y.csv").write_text("1")
    project.holdout = ["holdout_data"]
    k = Kernel(project, fake_worker(FAKES), registry)
    qd = Q.qdir(project, "Q-0002").resolve()
    (qd / "code").mkdir(parents=True, exist_ok=True)
    (qd / "code" / "analysis.py").write_text("x")
    (qd / "run").mkdir(exist_ok=True)  # the kernel creates run/ before each analysis run
    k._salt("Q-0002")
    code = (f"qd = pathlib.Path({str(qd)!r})\n"
            f"t('salt', lambda: pathlib.Path({str(K.SALT_FILE)!r}).read_text())\n"
            f"t('rename_salt', lambda: os.rename({str(K.SALT_FILE)!r}, qd / 'run' / 's'))\n"
            f"t('holdout', lambda: pathlib.Path({str(held / 'y.csv')!r}).read_text())\n"
            "t('code', lambda: (qd / 'code' / 'analysis.py').write_text('y'))\n"
            "t('run', lambda: (qd / 'run').mkdir(exist_ok=True) or (qd / 'run' / 'r.json').write_text('x'))\n")
    assert _probe(k._run_profile(qd, "discovery"), code) == ["salt:no", "rename_salt:no", "holdout:no", "code:no", "run:yes"]
    assert "holdout:yes" in _probe(k._run_profile(qd, "holdout"), code)


def test_salts_are_per_question_and_validated(lab):
    import rlab.kernel as K
    project, registry = lab
    k = Kernel(project, fake_worker(FAKES), registry)
    a, b = k._salt("Q-0001"), k._salt("Q-0002")
    assert a != b and len(a) == 64 and k._salt("Q-0001") == a
    K.SALT_FILE.write_text("short")
    with pytest.raises(RuntimeError):
        k._salt("Q-0001")


def test_kernel_refuses_to_run_unsandboxed(lab, monkeypatch):
    project, registry = lab
    monkeypatch.setenv("RLAB_NO_SANDBOX", "1")
    monkeypatch.delenv("RLAB_ALLOW_UNSANDBOXED", raising=False)
    with pytest.raises(RuntimeError, match="sandbox"):
        Kernel(project, fake_worker(FAKES), registry).run()


def test_decoy_split_is_not_confirmation(lab):
    project, registry = lab  # keep() on unrelated ids while the analysis uses every row
    Kernel(project, fake_worker(_with_script(results_script(GOOD, vary=False))), registry).run()
    q = Q.load(project, "Q-0002")
    assert q["verdict"] == "provisional" and "identical to discovery" in q["verdict_reason"] and not q["confirmed"]


def test_planted_holdout_result_is_not_read(lab):
    project, registry = lab
    planted = (
        "import argparse, json, os, pathlib\n"
        "a = argparse.ArgumentParser(); a.add_argument('--out'); args = a.parse_args()\n"
        "import rlab_split; rlab_split.keep(range(400))\n"
        f"r = {json.dumps(GOOD)}; r['split'] = rlab_split.summary()\n"
        "if os.environ['RLAB_SPLIT'] == 'discovery':\n"
        "    fake = dict(r, effects={'delta': {'point': 0.09, 'lo': 0.05, 'hi': 0.13, 'n': 120}})\n"
        "    fake['split'] = dict(r['split'], mode='holdout')\n"
        "    pathlib.Path('run/results_holdout.json').write_text(json.dumps(fake))\n"
        "    pathlib.Path(args.out).write_text(json.dumps(r))\n")  # the holdout run writes nothing
    Kernel(project, fake_worker(_with_script(planted)), registry).run()
    q = Q.load(project, "Q-0002")
    assert q["state"] == "blocked" and "confirm" in q["blocked_reason"]


@needs_sandbox
def test_symlinked_log_does_not_redirect_kernel_writes(lab):
    project, registry = lab
    target = project.qbank / "fdr.jsonl"
    target.write_text("")
    plant = f"import os\nos.path.lexists('run/log.txt') or os.symlink({str(target)!r}, 'run/log.txt')\nimport rlab_split"
    Kernel(project, fake_worker(_with_script(_env_script(GOOD, NULL).replace("import rlab_split", plant))), registry).run()
    assert all("qid" in json.loads(line) for line in target.read_text().splitlines())


def test_every_holdout_test_enters_the_fdr_stream(lab):
    project, registry = lab
    project.fdr_mode = "lord"
    Kernel(project, fake_worker(_with_script(_env_script(GOOD, NULL))), registry).run()
    stream = fdr.read(project.qbank / "fdr.jsonl")
    assert [e["qid"] for e in stream] == ["Q-0002"] and not stream[0]["reject"]


def test_malformed_split_blocks_nothing_else(lab):
    project, registry = lab
    bad = results_script({**GOOD, "split": {"mode": "discovery", "kernel_salt": True, "units_seen": "x",
                                            "units_kept": [1]}}, split=False)
    Kernel(project, fake_worker(_with_script(bad)), registry).run()
    v = verdicts(project)
    assert v["Does citation count "][1] == "provisional" and v["Is aspirin associate"] == ("reported", "answered")


def test_tampered_results_block_decide(lab):
    project, registry = lab
    k = Kernel(project, fake_worker(FAKES), registry)
    for _ in range(20):
        if Q.load(project, "Q-0002")["state"] == "reviewed":
            break
        k.tick()
    run_copy = Q.qdir(project, "Q-0002") / "run" / "results.json"
    run_copy.write_text(run_copy.read_text().replace("0.04", "0.05"))  # scripts can write run/: ignored
    k.tick()
    assert Q.load(project, "Q-0002")["state"] == "confirming"
    kept = Q.qdir(project, "Q-0002") / "kernel" / "results.json"
    kept.write_text(kept.read_text().replace("0.04", "0.05"))  # the kernel's own copy: detected
    k.tick()
    k.tick()
    assert Q.load(project, "Q-0002")["state"] == "blocked"


@needs_sandbox
def test_runs_cannot_write_shared_caches_or_read_credentials(lab):
    project, registry = lab
    k = Kernel(project, fake_worker(FAKES), registry)
    qd = Q.qdir(project, "Q-0002").resolve()
    for d in ("run", "cache", "scratch"):
        (qd / d).mkdir(parents=True, exist_ok=True)
    probe = os.path.expanduser("~/.cache/rlab-sandbox-probe")
    code = (f"t('home_cache', lambda: open({probe!r}, 'w').close())\n"
            "t('tmp', lambda: open('/private/tmp/rlab-sandbox-probe', 'w').close())\n"
            "t('aws', lambda: os.listdir(pathlib.Path.home() / '.aws'))\n"
            "t('ssh', lambda: os.listdir(pathlib.Path.home() / '.ssh'))\n"
            f"t('own_cache', lambda: (pathlib.Path({str(qd)!r}) / 'cache' / 'x').write_text('x'))\n")
    try:
        assert _probe(k._run_profile(qd, "discovery"), code) == [
            "home_cache:no", "tmp:no", "aws:no", "ssh:no", "own_cache:yes"]
    finally:
        for f in (probe, "/private/tmp/rlab-sandbox-probe"):
            if os.path.exists(f):
                os.unlink(f)


def test_planted_cache_symlink_blocks_the_question(lab):
    project, registry = lab

    def plants(call):
        out = FAKES["script"](call)
        if "citation count" in call.prompt.split("\n")[1]:
            os.symlink(project.root, call.cwd / "cache")
        return out

    Kernel(project, fake_worker({**FAKES, "script": plants}), registry).run()
    q = Q.load(project, "Q-0002")
    assert q["state"] == "blocked" and "not a plain folder" in q["blocked_reason"]
    assert not (project.root / "stray.txt").exists()


def test_symlink_in_code_fails_the_freeze(lab):
    import rlab.kernel as K
    project, registry = lab

    def links(call):
        out = FAKES["script"](call)
        if "citation count" in call.prompt.split("\n")[1] and not os.path.lexists(call.cwd / "code" / "x"):
            os.symlink(K.SALT_FILE, call.cwd / "code" / "x")
        return out

    Kernel(project, fake_worker({**FAKES, "script": links}), registry).run()
    q = Q.load(project, "Q-0002")
    assert q["state"] == "blocked" and "symlinks or bytecode" in q["blocked_reason"]
    assert not (Q.qdir(project, "Q-0002") / "review" / "code" / "x").exists()


def test_single_tick_also_refuses_unsandboxed(lab, monkeypatch):
    project, registry = lab
    monkeypatch.setenv("RLAB_NO_SANDBOX", "1")
    monkeypatch.delenv("RLAB_ALLOW_UNSANDBOXED", raising=False)
    with pytest.raises(RuntimeError, match="sandbox"):
        Kernel(project, fake_worker(FAKES), registry).tick()


def test_workers_need_the_spend_guard(lab, tmp_path, monkeypatch):
    project, registry = lab
    bare = tmp_path / "bare.json"
    bare.write_text("{}")
    monkeypatch.setattr("rlab.kernel.USER_SETTINGS", bare)
    Kernel(project, fake_worker(FAKES), registry).run()
    assert all(q["state"] == "blocked" and "spend_guard" in q["blocked_reason"] for q in Q.all_questions(project))


def test_run_environment_is_an_allowlist(lab, monkeypatch):
    project, registry = lab
    monkeypatch.setenv("RAPIDAPI_KEY", "metered")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    body = results_script(GOOD).replace(
        "import rlab_split", "import rlab_split\nr['audit'] = {'env': sorted(os.environ)}")
    Kernel(project, fake_worker(_with_script(body)), registry).run()
    seen = json.loads((Q.qdir(project, "Q-0002") / "kernel" / "results.json").read_text())["audit"]["env"]
    assert not {"RAPIDAPI_KEY", "SSH_AUTH_SOCK", "CLAUDE_CODE_OAUTH_TOKEN"} & set(seen) and "RLAB_SPLIT" in seen


@needs_sandbox
def test_runs_and_authors_cannot_read_other_questions(lab):
    project, registry = lab
    k = Kernel(project, fake_worker(FAKES), registry)
    other = Q.qdir(project, "Q-0003").resolve()
    (other / "kernel").mkdir(parents=True, exist_ok=True)
    (other / "kernel" / "results_holdout.json").write_text("{}")
    qd = Q.qdir(project, "Q-0002").resolve()
    for d in ("run", "cache", "scratch"):
        (qd / d).mkdir(parents=True, exist_ok=True)
    code = (f"t('other', lambda: open({str(other / 'kernel' / 'results_holdout.json')!r}).read())\n"
            f"t('own', lambda: open({str(qd / 'question.json')!r}).read())\n")
    assert _probe(k._run_profile(qd, "discovery"), code) == ["other:no", "own:yes"]
    assert _probe(k._worker_profile(Q.load(project, "Q-0002"), "script"), code) == ["other:no", "own:yes"]


@needs_sandbox
def test_symlinked_holdout_is_closed_by_its_real_path_too(lab, tmp_path):
    project, registry = lab
    store = tmp_path / "store"
    store.mkdir()
    (store / "labels.csv").write_text("LABELS")
    os.symlink(store, project.root / "held")
    project.holdout = ["held"]
    k = Kernel(project, fake_worker(FAKES), registry)
    qd = Q.qdir(project, "Q-0002").resolve()
    code = (f"t('via_link', lambda: open({str(project.root / 'held' / 'labels.csv')!r}).read())\n"
            f"t('via_real', lambda: open({str(store / 'labels.csv')!r}).read())\n")
    assert _probe(k._run_profile(qd, "discovery"), code) == ["via_link:no", "via_real:no"]


@needs_sandbox
def test_reviewer_can_write_its_own_config_but_not_its_settings(lab):
    import rlab.kernel as K
    project, registry = lab
    k = Kernel(project, fake_worker(FAKES), registry)
    q = Q.load(project, "Q-0002")
    prof = k._worker_profile(q, "review")
    cfg = K.WORKER_HOME / project.name / "Q-0002" / "review"
    code = (f"cfg = pathlib.Path({str(cfg)!r})\n"
            "t('claude_json', lambda: (cfg / '.claude.json').write_text('{}'))\n"
            "t('transcript', lambda: (cfg / 'projects' / 'x.jsonl').write_text('{}'))\n"
            "t('settings', lambda: (cfg / 'settings.json').write_text('{}'))\n"
            "t('rename_projects', lambda: os.rename(cfg / 'projects', cfg / 'p2'))\n")
    assert _probe(prof, code) == ["claude_json:yes", "transcript:yes", "settings:no", "rename_projects:no"]


def test_spend_guard_requirement_can_be_switched_off(lab, tmp_path, monkeypatch):
    project, registry = lab
    bare = tmp_path / "bare.json"
    bare.write_text("{}")
    monkeypatch.setattr("rlab.kernel.USER_SETTINGS", bare)
    monkeypatch.setattr("rlab.kernel.REQUIRE_SPEND_GUARD", False)
    Kernel(project, fake_worker(FAKES), registry).run()
    assert Q.load(project, "Q-0002")["state"] == "reported"
