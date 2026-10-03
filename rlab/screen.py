"""Apply a frontier screen: drop solved/incremental/untestable questions, annotate frontier ones,
import replacements. Only questions that were never approved or run are removed.

Screen JSON: [{"id", "verdict": SOLVED|INCREMENTAL|FRONTIER|UNTESTABLE, "prior", "gap", "reason"}]
Replacements: the "## Replacements" section of the screen markdown, in import format.
Every removal is recorded with its reason in qbank/SCREEN.md.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from . import questions as Q
from .project import Project
from .util import now_iso

DROP = {"SOLVED", "INCREMENTAL", "UNTESTABLE"}
REMOVABLE_STATES = {"imported"}


def replacements_section(markdown: str) -> str:
    """Text between a '## Replacements' heading and the next '## ' heading."""
    m = re.search(r"^##\s*Replacements\b.*?$(.*?)(?=^##\s|\Z)", markdown, re.M | re.S | re.I)
    return m.group(1) if m else ""


def apply(project: Project, screen: list[dict], replacements_md: str = "", batch: str = "frontier-v1") -> dict:
    by_id = {s.get("id"): s for s in screen if isinstance(s, dict)}
    removed, kept_locked, annotated, unscreened = [], [], [], []
    for q in Q.all_questions(project):
        s = by_id.get(q["id"])
        if s is None:
            unscreened.append(q["id"])
            continue
        verdict = str(s.get("verdict", "")).upper()
        if verdict in DROP:
            if q["state"] in REMOVABLE_STATES and not q.get("spend_usd"):
                removed.append((q["id"], verdict, q["text"], s.get("prior", ""), s.get("reason", "")))
                shutil.rmtree(Q.qdir(project, q["id"]))
            else:
                kept_locked.append(q["id"])  # approved or started: Mike's call, not the screen's
        elif verdict == "FRONTIER":
            q.update(prior_work=s.get("prior", ""), gap=s.get("gap", ""), screen="frontier")
            Q.save(project, q)
            Q.event(project, q["id"], "screened", verdict="FRONTIER")
            annotated.append(q["id"])
    created = []
    section = replacements_section(replacements_md)
    if section.strip():
        src = project.qbank / "imports" / f"{batch}.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(f"# {batch}: frontier replacements\n{section}")
        _, created = Q.import_batch(project, src, batch)
    log = [f"\n## Frontier screen {now_iso()}",
           f"Kept as frontier: {len(annotated)} · removed: {len(removed)} · replacements imported: {len(created)}"
           + (f" · not removed (approved or started): {', '.join(kept_locked)}" if kept_locked else "")
           + (f" · not screened: {', '.join(unscreened)}" if unscreened else "")]
    log += [f"- {qid} {verdict}: {text} | prior: {prior} | {reason}" for qid, verdict, text, prior, reason in removed]
    with (project.qbank / "SCREEN.md").open("a") as f:
        f.write("\n".join(log) + "\n")
    return {"annotated": len(annotated), "removed": len(removed), "created": len(created),
            "kept_locked": kept_locked, "unscreened": unscreened}


def load_screen(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    return data if isinstance(data, list) else data.get("questions", [])
