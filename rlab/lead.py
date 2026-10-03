"""The project lead: an agent that runs a project on the director's cycle so the owner is needed as little as possible.

`lab lead --root <project>` builds a status packet, runs one sandboxed lead worker (with its analyst), and applies
the actions it returns. The kernel, not the lead, carries out every action and checks each against the charter in
project.toml [lead]: caps on approvals and work in flight, no question whose needs are blocked by a project setting,
one retry per question and never for an integrity block, private publishing only if the charter allows it. Anything
only the owner can do goes to qbank/NEEDS_MIKE.md.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from . import envs
from . import questions as Q
from . import repo as RP
from . import sandbox
from . import teams
from .project import Project
from .schema import validate
from .util import append_jsonl, atomic_write_json, now_iso, read_json
from .worker import StageCall, Worker, WorkerResult, claude_worker

PKG = Path(__file__).resolve().parent.parent
FINISHED = ("decided", "reported", "writing", "awaiting_batch")
IN_FLIGHT = ("approved", "lit", "protocol", "rule_frozen", "prereg_frozen", "ran", "reviewed", "confirming", "confirm_ran")
INTEGRITY = ("changed", "write boundary", "wrote outside", "tamper", "not a plain folder", "symlink", "salt")
DEFAULTS = {"enabled": False, "max_approvals_per_cycle": 3, "max_in_flight": 6, "may_requeue": True, "may_park": True,
            "may_publish_private": False, "model": "opus"}


def charter(project: Project) -> dict:
    return {**DEFAULTS, **project.lead}


def running_pid(project: Project) -> int | None:
    try:
        pid = int((project.qbank / "run.pid").read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def start_run(project: Project, popen=subprocess.Popen) -> int:
    """Start `lab run` detached (same convention as ccc: qbank/run.pid, qbank/run.log)."""
    with open(project.qbank / "run.log", "a") as log:
        proc = popen([sys.executable, "-m", "rlab.cli", "run", "--root", str(project.root)], cwd=project.root,
                     stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                     env={**os.environ, "PYTHONPATH": str(PKG)})
    (project.qbank / "run.pid").write_text(str(proc.pid))
    return proc.pid


def packet(project: Project) -> dict:
    qs = Q.all_questions(project)
    counts: dict[str, int] = {}
    for q in qs:
        counts[q["state"]] = counts.get(q["state"], 0) + 1
    spend = 0.0
    today = now_iso()[:10]
    path = project.qbank / "spend.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("at", "").startswith(today):
                spend += float(e.get("usd", 0))
    # every waiting question (a cap once hid a whole family from the lead); long fields trimmed instead
    waiting = [{"id": q["id"], "text": q["text"][:300], "type": q.get("type"), "family": q.get("family_hint"),
                "batch": q.get("import_batch"), "needs": q.get("needs"), "prior_work": (q.get("prior_work") or "")[:200],
                "gap": (q.get("gap") or "")[:200], "decision": (q.get("decision_it_drives") or "")[:160],
                "blocked_by_settings": envs.unmet_gates(q.get("needs"), project.gates)}
               for q in qs if q["state"] == "imported"]
    blocked = [{"id": q["id"], "text": q["text"][:200], "reason": q.get("blocked_reason"),
                "requeued_before": q.get("requeues", 0)} for q in qs if q["state"] == "blocked"]
    recent = sorted((q for q in qs if q["state"] in FINISHED), key=lambda q: q.get("updated_at", ""))[-15:]
    return {"project": project.name, "description": project.description, "charter": charter(project),
            "counts": counts, "in_flight": sum(counts.get(s, 0) for s in IN_FLIGHT),
            "run_in_progress": running_pid(project) is not None, "usage_today_usd": round(spend, 2),
            "usage_cap_per_day_usd": project.project_day_usd, "waiting": waiting, "blocked": blocked,
            "recent": [{"id": q["id"], "verdict": q.get("verdict"), "reason": (q.get("verdict_reason") or "")[:240],
                        "repo": bool(q.get("repo")), "published": q.get("published")} for q in recent]}


def apply(project: Project, out: dict, publish=RP.publish, popen=subprocess.Popen) -> dict:
    """Carry out the lead's actions within the charter. Returns {"done": [...], "refused": [...]}."""
    c = charter(project)
    done, refused = [], []
    by_id = {q["id"]: q for q in Q.all_questions(project)}
    in_flight = sum(1 for q in by_id.values() if q["state"] in IN_FLIGHT)

    for a in out.get("park", []):
        q = by_id.get(a["id"])
        if not c["may_park"] or not q or q["state"] not in ("imported", "blocked"):
            refused.append(f"park {a['id']}: not allowed or not waiting/blocked")
            continue
        Q.set_state(project, q, "decided", verdict="parked", verdict_reason=f"parked by the project lead: {a['why']}")
        done.append(f"parked {a['id']}")
    approved = 0
    for a in out.get("approve", []):
        q = by_id.get(a["id"])
        if not q or q["state"] != "imported":
            refused.append(f"approve {a['id']}: not waiting for approval")
        elif envs.unmet_gates(q.get("needs"), project.gates):
            refused.append(f"approve {a['id']}: needs a project setting the owner has not turned on")
        elif approved >= c["max_approvals_per_cycle"] or in_flight >= c["max_in_flight"]:
            refused.append(f"approve {a['id']}: charter cap reached")
        else:
            Q.set_state(project, q, "approved", approved_by="lead", approved_why=a["why"])
            approved += 1
            in_flight += 1
            done.append(f"approved {a['id']}")
    for a in out.get("requeue", []):
        q = by_id.get(a["id"])
        reason = (q or {}).get("blocked_reason") or ""
        if not c["may_requeue"] or not q or q["state"] != "blocked":
            refused.append(f"requeue {a['id']}: not allowed or not blocked")
        elif any(k in reason.lower() for k in INTEGRITY):
            refused.append(f"requeue {a['id']}: integrity block, owner only ({reason[:80]})")
        elif q.get("requeues", 0) >= 1:
            refused.append(f"requeue {a['id']}: already retried once")
        else:
            back = q.get("blocked_from") or "approved"
            stage_attempts = {k: v for k, v in (q.get("stage_attempts") or {}).items()}
            for k in list(stage_attempts):
                stage_attempts[k] = min(stage_attempts[k], 1)
            Q.set_state(project, q, back, requeues=q.get("requeues", 0) + 1, stage_attempts=stage_attempts,
                        blocked_reason=None, requeue_why=a["why"])
            done.append(f"requeued {a['id']} to {back}")
    for a in out.get("prioritise", []):
        q = by_id.get(a["id"])
        if q and q["state"] not in FINISHED:
            Q.set_state(project, q, q["state"], value=a["value"])
            done.append(f"priority {a['id']} = {a['value']}")
    for a in out.get("publish_private", []):
        q = by_id.get(a["id"])
        if not c["may_publish_private"]:
            refused.append(f"publish {a['id']}: charter does not allow publishing")
        elif not q or not q.get("repo") or q.get("published"):
            refused.append(f"publish {a['id']}: no unpublished repo")
        else:
            ok, msg = publish(Q.qdir(project, q["id"]) / q["repo"], public=False)
            if ok:
                Q.set_state(project, q, q["state"], published=msg)
                done.append(f"published {a['id']} privately: {msg}")
            else:
                refused.append(f"publish {a['id']}: {msg}")
    if out.get("needs_mike"):
        lines = [f"- {now_iso()[:16]} {project.name}: {n['item']} ({n['why']})" for n in out["needs_mike"]]
        with open(project.qbank / "NEEDS_MIKE.md", "a") as f:
            f.write("\n".join(lines) + "\n")
        done.append(f"{len(lines)} item(s) for the owner")
    if out.get("run"):
        if running_pid(project):
            refused.append("run: already running")
        else:
            done.append(f"started lab run (pid {start_run(project, popen)})")
    return {"done": done, "refused": refused}


def cycle(project: Project, worker: Worker | None = None, publish=RP.publish, popen=subprocess.Popen,
          dry_run: bool = False) -> dict:
    """One lead cycle. Returns the applied summary and the digest."""
    from .kernel import WORKER_HOME, Kernel  # the kernel owns the sandbox helpers

    c = charter(project)
    if not c["enabled"] and not dry_run:
        return {"skipped": "lead not enabled in project.toml [lead]"}
    work = project.qbank / "lead"
    work.mkdir(parents=True, exist_ok=True)
    atomic_write_json(work / "packet.json", packet(project))
    k = Kernel(project, worker or claude_worker)
    pseudo = {"id": "_lead"}
    cfg = k._config_dir(pseudo, "lead")
    schema = json.loads((PKG / "schemas" / "lead.json").read_text())
    schema["properties"]["team"] = teams.TEAM_SCHEMA
    schema["required"] = [*schema["required"], "team"]
    prof = None
    if sandbox.available():
        prof = sandbox.profile(claude_config=cfg, write=[work], deny_read=[*k._hidden(), WORKER_HOME / project.name],
                               allow_read=[cfg], deny_write_exact=[work])
    call = StageCall(stage="lead", prompt=f"Run one lead cycle for project {project.name}. Read packet.json.",
                     system_prompt_file=PKG / "prompts" / "lead.md", schema=schema, cwd=work, model=c["model"],
                     budget_usd=project.stage_budget.get("lead", 1.0), max_turns=20,
                     allowed_tools=["Read", "Grep", "Glob", "Agent"], disallowed_tools=["Write", "Edit", "Bash"],
                     sandbox=prof, agents=teams.agents_for("lead"),
                     env={"TMPDIR": str(work), "CLAUDE_CODE_TMPDIR": str(work), "CLAUDE_CONFIG_DIR": str(cfg)})
    res: WorkerResult = (worker or claude_worker)(call)
    append_jsonl(project.qbank / "spend.jsonl", {"at": now_iso(), "qid": "_lead", "stage": "lead", "usd": res.cost_usd,
                                                 "session": res.session_id})
    if not res.ok:
        return {"error": f"{res.subtype}: {res.error}"}
    errors = validate(res.output, schema) + [f"team: no finding from {r}" for r in teams.missing_roles("lead", res.output)]
    if errors:
        return {"error": "; ".join(errors[:6])}
    out = res.output
    summary = {"done": [], "refused": []} if dry_run else apply(project, out, publish, popen)
    entry = {"at": now_iso(), "digest": out["digest"], **summary, "proposed": {k: out[k] for k in
             ("approve", "park", "requeue", "prioritise", "publish_private", "run")}}
    append_jsonl(project.qbank / "lead" / "log.jsonl", entry)
    with open(project.qbank / "lead" / "digest.md", "a") as f:
        f.write(f"\n## {entry['at'][:16]}\n\n{out['digest']}\n\n" + "".join(f"- {d}\n" for d in summary["done"])
                + "".join(f"- refused: {r}\n" for r in summary["refused"]))
    return entry
