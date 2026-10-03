"""Per-question GitHub repo in the house style."""

import json
import subprocess

from rlab import questions as Q
from rlab import repo as RP
from rlab.kernel import Kernel
from rlab.worker import fake_worker
from test_kernel import FAKES, WRITEUP, lab  # noqa: F401


def test_repo_is_assembled_in_house_order(lab):
    project, registry = lab
    Kernel(project, fake_worker(FAKES), registry).run()
    q = Q.load(project, "Q-0002")
    repo = Q.qdir(project, "Q-0002") / q["repo"]
    readme = (repo / "README.md").read_text()
    heads = [l for l in readme.splitlines() if l.startswith("#")]
    assert heads == ["# Citation count and later use", "## Abstract", "## Year-one citations", "## Reproduce", "## Layout",
                     "## Acknowledgements and data licenses", "## Cite", "## License"]
    assert "Record: `results/discovery.json`, `results/holdout.json`" in readme and "Kernel verdict: pass" in readme
    for f in ("hypothesis.md", "experiments.md", "CITATION.cff", "LICENSE", ".gitignore", "src/analysis.py",
              "src/rlab_split.py", "results/discovery.json", "results/holdout.json", "github.json"):
        assert (repo / f).exists(), f
    assert "abstract: |" in (repo / "CITATION.cff").read_text() and "I tested" in (repo / "CITATION.cff").read_text()
    assert "## What would count" in (repo / "hypothesis.md").read_text()
    assert "HOLDOUT RESULT" in (repo / "experiments.md").read_text()
    lit_repo = Q.qdir(project, "Q-0001") / Q.load(project, "Q-0001")["repo"]
    assert "## Sources" in (lit_repo / "README.md").read_text() and not (lit_repo / "src").exists()
    assert "GitHub-ready repo" in (Q.qdir(project, "Q-0002") / "card.md").read_text()


def test_house_style_is_enforced_with_feedback(lab):
    project, registry = lab
    prompts = []

    def sloppy(call):
        prompts.append(call.prompt)
        if len([p for p in prompts if "Q-0002" in p]) == 1 and "Q-0002" in call.prompt:
            return {**WRITEUP, "abstract": "A novel, groundbreaking result. \U0001F680"}
        return dict(WRITEUP)

    Kernel(project, fake_worker({**FAKES, "writeup": sloppy}), registry).run()
    retry = [p for p in prompts if "Q-0002" in p][1]
    assert "house style" in retry and "emoji" in retry and "novel" in retry
    assert Q.load(project, "Q-0002").get("repo")


def test_failed_writeup_does_not_block_the_verdict(lab):
    project, registry = lab
    Kernel(project, fake_worker({**FAKES, "writeup": lambda c: {**WRITEUP, "topics": ["x"]}}), registry).run()
    q = Q.load(project, "Q-0002")
    assert q["state"] == "reported" and q["verdict"] == "pass" and "topics" in q["writeup_error"] and not q.get("repo")


def test_publish_runs_the_expected_commands(lab):
    project, registry = lab
    Kernel(project, fake_worker(FAKES), registry).run()
    repo = Q.qdir(project, "Q-0002") / Q.load(project, "Q-0002")["repo"]
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    ok, url = RP.publish(repo, public=False, runner=runner)
    assert ok and url.endswith("/citation-count-later-use")
    create = next(c for c in calls if c[:3] == ["gh", "repo", "create"])
    assert "--private" in create and "--public" not in create
    edit = next(c for c in calls if c[:3] == ["gh", "repo", "edit"])
    assert edit.count("--add-topic") == 5 and "research" in edit
