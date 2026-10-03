"""Question folders: <qbank>/questions/Q-0001/ with question.json + events.jsonl + stage markers.

Files are the source of truth. Every state change is an atomic rewrite of
question.json plus one appended event.
"""

from __future__ import annotations

import re
from pathlib import Path

from .project import Project
from .util import append_jsonl, atomic_write_json, now_iso, read_json, sha256_json

TERMINAL = {"reported", "blocked"}
TYPES = ("lit", "compute", "ml")


def qdir(project: Project, qid: str) -> Path:
    return project.questions_dir / qid


def load(project: Project, qid: str) -> dict:
    q = read_json(qdir(project, qid) / "question.json")
    if q is None:
        raise KeyError(qid)
    return q


def save(project: Project, q: dict) -> None:
    atomic_write_json(qdir(project, q["id"]) / "question.json", q)


def event(project: Project, qid: str, kind: str, **fields) -> None:
    append_jsonl(qdir(project, qid) / "events.jsonl", {"at": now_iso(), "event": kind, **fields})


def set_state(project: Project, q: dict, state: str, **fields) -> dict:
    old = q.get("state")
    q.update(fields)
    q["state"] = state
    q["updated_at"] = now_iso()
    save(project, q)
    event(project, q["id"], "state", old=old, new=state, **{k: v for k, v in fields.items() if k != "text"})
    return q


def all_ids(project: Project) -> list[str]:
    if not project.questions_dir.is_dir():
        return []
    return sorted(p.name for p in project.questions_dir.iterdir() if (p / "question.json").is_file())


def all_questions(project: Project) -> list[dict]:
    return [load(project, qid) for qid in all_ids(project)]


def next_id(project: Project) -> str:
    nums = [int(m.group(1)) for qid in all_ids(project) if (m := re.match(r"Q-(\d+)$", qid))]
    return f"Q-{(max(nums) + 1 if nums else 1):04d}"


_ITEM = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.*\S)\s*$")
_TAG = re.compile(r"\[(type|decision|depends|family|needs|prior|gap):([^\]]+)\]")


def parse_import(text: str) -> list[dict]:
    """Markdown list items become questions. Optional tags: [type:lit|compute|ml] [decision:...] ."""
    items = []
    for line in text.splitlines():
        if re.match(r"^\s*#+\s*(notes|exclusions|excluded)\b", line, re.I):
            break  # commentary after the question list is not questions
        m = _ITEM.match(line)
        if not m:
            continue
        body, tags = m.group(1), {}
        for key, value in _TAG.findall(body):
            tags[key] = value.strip()
        body = _TAG.sub("", body).strip()
        if body:
            items.append({"text": body, **tags})
    return items


def import_batch(project: Project, source: Path, batch: str | None = None) -> tuple[str, list[str]]:
    text = source.read_text()
    items = parse_import(text)
    batch = batch or source.stem
    imports = project.qbank / "imports"
    imports.mkdir(parents=True, exist_ok=True)
    (imports / f"{batch}.md").write_text(text)
    existing = {q["text"].strip().lower() for q in all_questions(project)}
    created = []
    for item in items:
        if item["text"].strip().lower() in existing:
            continue
        qid = next_id(project)
        q = {
            "id": qid, "project": project.name, "text": item["text"], "import_batch": batch,
            "import_sha": sha256_json(item), "type": item.get("type") if item.get("type") in TYPES else None,
            "decision_it_drives": item.get("decision", ""), "family_hint": item.get("family", ""), "needs": [n.strip() for n in item.get("needs", "").split(",") if n.strip()], "prior_work": item.get("prior", ""), "gap": item.get("gap", ""), "state": "imported", "stage_attempts": {},
            "spend_usd": 0.0, "verdict": None, "links": {}, "created_at": now_iso(),
        }
        qdir(project, qid).mkdir(parents=True, exist_ok=True)
        save(project, q)
        event(project, qid, "imported", batch=batch)
        existing.add(item["text"].strip().lower())
        created.append(qid)
    return batch, created


def approve_ids(project: Project, ids: list[str]) -> tuple[list[str], list[str]]:
    """Approve individual imported questions; returns (approved, skipped with reason)."""
    approved, skipped = [], []
    for qid in ids:
        try:
            q = load(project, qid)
        except (OSError, ValueError, KeyError):
            skipped.append(f"{qid}: not found")
            continue
        if q["state"] != "imported":
            skipped.append(f"{qid}: already {q['state']}")
            continue
        set_state(project, q, "approved")
        approved.append(qid)
    return approved, skipped


def approve_batch(project: Project, batch: str) -> list[str]:
    approved = []
    for q in all_questions(project):
        if q.get("import_batch") == batch and q["state"] == "imported":
            set_state(project, q, "approved")
            approved.append(q["id"])
    return approved
