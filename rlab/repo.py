"""Per-question GitHub repo in the house style of Michael DeAngelis's research repos.

The writeup worker returns prose fields only (title, lead, plain-English abstract, result section,
description, topics). The kernel assembles everything else from its own records, so structure,
numbers and verdict cannot drift: README.md (fixed section order), hypothesis.md, experiments.md,
results/*.json, src/ + requirements.txt, CITATION.cff (abstract included), LICENSE (MIT), .gitignore,
github.json (name, description, topics). Publishing is a separate, explicit command (`lab publish`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import questions as Q
from .project import Project
from .util import atomic_write_json, atomic_write_text, read_json

# family and given name for CITATION.cff and LICENSE, and the GitHub owner for `lab publish`
AUTHOR = tuple(os.environ.get("RLAB_AUTHOR", "DeAngelis,Michael").split(",", 1))
OWNER = os.environ.get("RLAB_GH_OWNER", "michaeldeangelis")
WRITE_UP = ("pass", "kill", "provisional", "invalid", "answered", "unanswerable")  # duplicates and parked are not
BANNED = ("novel", "groundbreaking", "revolutionary", "promising", "robust", "cutting-edge", "state-of-the-art",
          "game-changing", "exciting", "remarkable")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
GITIGNORE = ".venv/\n__pycache__/\n*.pyc\n.pytest_cache/\n.DS_Store\n.env*\ndata/\ncache/\nscratch/\n"
MIT = """MIT License

Copyright (c) {year} {holder}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


def style_problems(out: dict) -> list[str]:
    """House-style checks on the worker's prose; any problem fails the attempt with feedback."""
    problems = []
    prose = " ".join(str(out.get(k, "")) for k in ("title", "description", "lead", "abstract", "result_heading",
                                                   "result_body", "reproduce_notes"))
    if _EMOJI.search(prose):
        problems.append("no emojis anywhere")
    hits = sorted({w for w in BANNED if re.search(rf"\b{re.escape(w)}\b", prose, re.I)})
    if hits:
        problems.append(f"no marketing or hedging adjectives: {', '.join(hits)}")
    paras = [p for p in str(out.get("abstract", "")).split("\n\n") if p.strip()]
    if not 2 <= len(paras) <= 6:
        problems.append(f"abstract must be 3-5 plain-English paragraphs separated by blank lines (got {len(paras)})")
    topics = out.get("topics") or []
    if "research" not in topics or not 4 <= len(topics) <= 6:
        problems.append("topics: 5-6 lowercase topics including 'research'")
    title = str(out.get("title", ""))
    if not title or title != title[0].upper() + title[1:] or any(w[:1].isupper() for w in title.split()[1:]
                                                                  if not w.isupper()):
        problems.append("title: plain words, first word capitalised only")
    if len(str(out.get("description", ""))) > 350:
        problems.append("description: at most 3 short sentences")
    if not re.search(r"\b(I|my|me)\b", str(out.get("abstract", ""))):
        problems.append("abstract in the first person ('I tested ...')")
    return problems


def _rule_text(rule: dict) -> str:
    if not rule:
        return "no decision rule"
    side = "above" if rule.get("comparator") == "gt" else "below"
    ci = "the whole 95% interval" if rule.get("ci_must_exclude", True) else "the point estimate"
    return (f"{ci} of `{rule.get('metric')}` {side} {rule.get('threshold')}, with at least {rule.get('min_n')} units "
            f"across both splits; negative controls must read null")


def _hypothesis(q: dict, prereg: dict) -> str:
    why = " ".join(x for x in (q.get("decision_it_drives") and f"It decides: {q['decision_it_drives']}.",
                               q.get("gap") and f"The gap it fills: {q['gap']}",
                               q.get("prior_work") and f"Closest prior work: {q['prior_work']}.") if x)
    count = (f"Fixed before the run in [experiments.md](experiments.md): {_rule_text(prereg.get('decision_rule') or {})}."
             if prereg else "An answer counts when published evidence addresses the question directly, graded by "
             "strength; every cited source resolved to its identifier and title.")
    return (f"# Hypothesis\n\n## Question\n\n{q['text']}\n\n## Why this question\n\n{why or 'See the README.'}\n\n"
            f"## What would count\n\n{count}\n\n## What it cannot show\n\n"
            f"{prereg.get('notes') or 'What published evidence has not measured.'}\n")


def _ledger(q: dict, prereg: dict, events: list[dict]) -> str:
    lines = ["# Experiments", "", "Append-only record kept by the lab kernel. Rules were fixed and hashed before any code "
             "ran; the kernel ran the analysis and applied the rule.", ""]
    frozen = next((e["at"][:10] for e in events if e.get("new") == "rule_frozen"), None)
    if prereg:
        lines += [f"## {frozen or ''} Preregistration (rules fixed before the run)".replace("##  ", "## "), "",
                  f"- Hypothesis: {prereg.get('hypothesis', '')}", f"- Analysis plan: {prereg.get('analysis_plan', '')}",
                  f"- Decision rule: {_rule_text(prereg.get('decision_rule') or {})}",
                  f"- Baselines: {', '.join(prereg.get('baselines', []))}",
                  f"- Negative controls: {', '.join(prereg.get('negative_controls', []))}",
                  f"- Comparisons declared: {prereg.get('n_comparisons', 1)}; alpha {prereg.get('alpha')}",
                  f"- Preregistration sha256: `{q.get('prereg_sha', '')}`", ""]
    if q.get("effect"):
        e = q["effect"]
        lines += [f"## RUN RESULT (discovery split)", "",
                  f"- `{q.get('metric')}` {e.get('point')} (95% interval {e.get('lo')} to {e.get('hi')}), n={e.get('n')}",
                  f"- Rerun reproduced: {q.get('reproduced')}; review: {q.get('review_severity')}", ""]
    if q.get("holdout_effect"):
        h = q["holdout_effect"]
        lines += ["## HOLDOUT RESULT", "",
                  f"- `{q.get('metric')}` {h.get('point')} (95% interval {h.get('lo')} to {h.get('hi')}), n={h.get('n')}",
                  f"- Held-out verdict: {q.get('holdout_verdict')}; confirmed: {q.get('confirmed')}", ""]
    lines += ["## VERDICT", "", f"{q.get('verdict')}: {q.get('verdict_reason', '')}", ""]
    return "\n".join(lines)


def _requirements(python: str | None) -> str:
    if not python:
        return ""
    try:
        out = subprocess.run([python, "-m", "pip", "list", "--format=freeze"], capture_output=True, text=True,
                             timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    keep = {"numpy", "pandas", "requests", "scipy", "statsmodels", "pyarrow", "scikit-learn", "rdkit", "torch",
            "transformers", "safetensors", "openpyxl", "pdfplumber", "lifelines", "psycopg"}
    return "".join(f"{l}\n" for l in out.splitlines() if l.split("==")[0].lower() in keep)


def assemble(project: Project, q: dict, out: dict, runtime: Path) -> Path:
    """Write qd/repo/ from the worker's prose and the kernel's records. Returns the repo path."""
    qd = Q.qdir(project, q["id"])
    repo = qd / "repo"
    if repo.exists():
        shutil.rmtree(repo)
    (repo / "results").mkdir(parents=True)
    prereg = read_json(qd / "prereg.json", {})
    events = [json.loads(l) for l in (qd / "events.jsonl").read_text().splitlines()] if (qd / "events.jsonl").exists() else []
    year = datetime.now(timezone.utc).year
    is_lit = not prereg
    records = []
    for src, name in ((qd / "kernel" / "results.json", "discovery.json"), (qd / "kernel" / "results_holdout.json", "holdout.json"),
                      (qd / "lit" / "evidence.json", "evidence.json"), (qd / "review" / "review.json", "review.json")):
        if src.is_file():
            shutil.copyfile(src, repo / "results" / name)
            records.append(f"`results/{name}`")
    atomic_write_text(repo / "hypothesis.md", _hypothesis(q, prereg))
    atomic_write_text(repo / "experiments.md", _ledger(q, prereg, events))
    layout = [("hypothesis.md", "The question and what would count as an answer"),
              ("experiments.md", "The ledger: preregistration, run, held-out check, verdict"),
              ("results/", "Kernel records: " + ", ".join(r.strip("`").removeprefix("results/") for r in records))]
    if not is_lit and (qd / "code").is_dir():
        shutil.copytree(qd / "code", repo / "src", symlinks=True)
        shutil.copyfile(runtime / "rlab_split.py", repo / "src" / "rlab_split.py")
        atomic_write_text(repo / "requirements.txt", _requirements(q.get("env_python")))
        layout += [("src/analysis.py", "The analysis, exactly as frozen and run"),
                   ("src/rlab_split.py", "The unit splitter used for the held-out check"),
                   ("requirements.txt", "Pinned versions of the libraries the analysis used")]
    verdict_line = f"Record: {', '.join(records)}. Kernel verdict: {q.get('verdict')}. {q.get('verdict_reason', '')}"
    readme = [f"# {out['title']}", "", out["lead"].strip(), "", "## Abstract", "", out["abstract"].strip(), "",
              f"## {out['result_heading'].strip()}", "", out["result_body"].strip(), "", verdict_line, ""]
    if is_lit:
        sources = q.get("sources") or []
        readme += ["## Sources", ""] + [f"- {s.get('id')}: {s.get('title', '')}" for s in sources] + [""]
    else:
        unit = "units"
        readme += ["## Reproduce", "", "```bash", "python -m venv .venv", "source .venv/bin/activate",
                   "pip install -r requirements.txt", "RLAB_SPLIT=all python src/analysis.py --out results/rerun.json",
                   "```", "",
                   f"The lab ran the analysis on a random 70% of the {unit} and confirmed it on the other 30%. The split used "
                   "a private key, so a rerun with `RLAB_SPLIT=all` uses every unit and gives numbers close to, not equal "
                   "to, the record. " + out.get("reproduce_notes", "").strip(), ""]
    readme += ["## Layout", "", "| Path | Contents |", "|---|---|"] + [f"| `{p}` | {c} |" for p, c in layout] + [""]
    if out.get("data_acknowledgements"):
        readme += ["## Acknowledgements and data licenses", ""] + [f"- {a}" for a in out["data_acknowledgements"]] + [
            "- No raw data is committed.", ""]
    readme += ["## Cite", "", "Use the Cite this repository button. It reads `CITATION.cff`.", "",
               "## License", "", f"[MIT](LICENSE). Copyright {year} {AUTHOR[1]} {AUTHOR[0]}.", ""]
    atomic_write_text(repo / "README.md", "\n".join(readme))
    abstract = "\n".join("  " + l if l else "" for l in out["abstract"].strip().splitlines())
    atomic_write_text(repo / "CITATION.cff", (
        'cff-version: 1.2.0\nmessage: "If you use this software, please cite it as below."\n'
        f'title: "{out["title"]}"\ntype: software\nauthors:\n  - family-names: {AUTHOR[0]}\n    given-names: {AUTHOR[1]}\n'
        f'version: 0.1.0\ndate-released: "{datetime.now(timezone.utc):%Y-%m-%d}"\n'
        f'repository-code: "https://github.com/{OWNER}/{out["repo_name"]}"\nlicense: MIT\nabstract: |\n{abstract}\n'))
    atomic_write_text(repo / "LICENSE", MIT.format(year=year, holder=f"{AUTHOR[1]} {AUTHOR[0]}"))
    atomic_write_text(repo / ".gitignore", GITIGNORE)
    atomic_write_json(repo / "github.json", {"owner": OWNER, "name": out["repo_name"], "description": out["description"],
                                             "topics": out["topics"]})
    return repo


def publish(repo: Path, public: bool, runner=subprocess.run) -> tuple[bool, str]:
    """Create the GitHub repo from qd/repo/ and push it. Outward-facing: only ever run on Mike's explicit command."""
    meta = read_json(repo / "github.json")
    if not isinstance(meta, dict):
        return False, f"no github.json in {repo}; run the writeup stage first"
    full = f"{meta['owner']}/{meta['name']}"
    steps = [["git", "init", "-q", "-b", "main"], ["git", "add", "-A"],
             ["git", "commit", "-q", "-m", meta["description"]],
             ["gh", "repo", "create", full, "--public" if public else "--private", "--source", ".", "--push",
              "--description", meta["description"], "--disable-wiki"],
             ["gh", "repo", "edit", full, *sum((["--add-topic", t] for t in meta["topics"]), [])]]
    if (repo / ".git").exists():
        steps = steps[1:]
    for cmd in steps:
        r = runner(cmd, cwd=repo, capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"{' '.join(cmd[:3])} failed: {(r.stdout + r.stderr).strip()[-400:]}"
    return True, f"https://github.com/{full}"
