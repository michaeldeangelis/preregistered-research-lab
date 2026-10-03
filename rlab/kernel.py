"""The lab kernel: moves each question through its stages, deterministically.

LLM workers do judgment inside one stage (triage, lit, protocol, review). The
kernel alone runs analysis code, applies the preregistered decision rule, writes
cards and registry entries, and enforces caps, attempts and write boundaries.

State -> handler:
  approved -> triage -> (lit | protocol | decided)
  lit -> decided
  protocol (rule: no data, no shell) -> rule_frozen -> script -> prereg_frozen -> run -> ran
    -> review -> reviewed -> decide -> (confirming -> confirm ->) decided -> report -> reported
  any failure beyond MAX_ATTEMPTS, cap breach, write violation or missing write boundary -> blocked

The decision rule is written and hashed before any worker can see data or run code, so it
cannot be tuned to the results. The script worker may test its code but cannot change the rule.

Confinement: every worker and analysis run executes under sandbox-exec (rlab/sandbox.py): it can
write only inside its question folder, and cannot read holdout data, the split salt or (for the
reviewer) the author's reasoning. With the sandbox, worker stages and analysis runs proceed in
parallel ([concurrency]); kernel stages (decide, report) stay serial so the FDR stream and the
registry are written in one order.

Confirmation: analysis scripts split units through runtime/rlab_split.py with a salt only the
kernel knows. A discovery pass is re-run by the kernel on the held-out units; that confirmatory
p-value enters the project's online FDR stream (LORD++, rlab/fdr.py). Only a pass that confirms
and clears its LORD++ level stays a pass.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import cite
from . import envs
from . import fdr
from . import sandbox
from . import teams
from . import questions as Q
from . import repo as RP
from . import report as R
from . import rules
from .project import Project
from .schema import validate
from .util import append_jsonl, atomic_write_json, atomic_write_text, now_iso, read_json, sha256_file, tree_sha
from .worker import TOKEN_FILE, StageCall, Worker, WorkerResult, claude_worker

PKG = Path(__file__).resolve().parent.parent
MAX_ATTEMPTS = 3
MAX_TURNS = {"triage": 12, "lit": 30, "protocol": 15, "script": 40, "review": 25, "writeup": 15}
# what the rerun must reproduce exactly
REPRO_KEYS = ("primary_endpoint", "effects", "baselines", "controls", "seeds", "split")
RUNTIME = PKG / "runtime"  # rlab_split.py, importable by analysis scripts
SALT_FILE = Path(os.environ.get("RLAB_SALT_FILE", Path.home() / ".config" / "rlab" / "split_salt"))
# per-question `claude` config dirs (author, review): workers never touch the real ~/.claude
WORKER_HOME = Path(os.environ.get("RLAB_WORKER_HOME", Path.home() / ".local" / "share" / "rlab" / "claude"))
USER_SETTINGS = Path.home() / ".claude" / "settings.json"
# shared folder of manually downloaded datasets, offered to the script stage
DATA_DIR = Path(os.environ.get("RLAB_DATA", Path.home() / ".dev" / "rlab-projects" / "_data"))
# workers refuse to start unless the user's Claude settings carry a spend_guard PreToolUse hook; RLAB_SPEND_GUARD=off
# drops the requirement (for machines without one)
REQUIRE_SPEND_GUARD = os.environ.get("RLAB_SPEND_GUARD", "on") != "off"
# files in a question folder that workers and analysis runs may never write (the kernel writes them)
PROTECTED = ("prereg.json", "prereg.md", "question.json", "events.jsonl", "triage.json", "stages", "lit", "review",
             "card.md", "run", "kernel", ".claude", "CLAUDE.md", "CLAUDE.local.md", ".mcp.json")
# kernel-owned folders inside a question; a symlink at any of them would redirect a sandbox rule
OWNED = ("code", "cache", "scratch", "run", "review", "review/work", "review/code", "kernel", "lit", "stages")
# literature search goes through these APIs (WebFetch) unless a project turns general WebSearch on
SEARCH_APIS = ("eutils.ncbi.nlm.nih.gov", "www.ebi.ac.uk", "api.openalex.org", "export.arxiv.org",
               "api.crossref.org", "api.semanticscholar.org")
KERNEL_STATES = ("reviewed", "confirm_ran", "awaiting_batch", "decided")  # cheap and order-sensitive: run serially
# a batch closes for multiple-testing purposes when none of its questions is still in one of these
ACTIVE = ("approved", "lit", "protocol", "rule_frozen", "prereg_frozen", "ran", "reviewed", "confirming", "confirm_ran")
CPU_STATES = ("prereg_frozen", "confirming")
# fields of prereg.json the reviewer sees; hypothesis and notes (the author's reasoning) are withheld
REVIEW_FIELDS = ("family", "alpha", "decision_rule", "analysis_plan", "baselines", "negative_controls", "data", "seeds",
                 "n_comparisons")
BASH_NOTE = ("Bash accepts one command per call that starts with an allowed program ({forms}); compound commands "
             "(`cd x; y`, `&&`, pipes) and other programs are denied. If a command is denied, rewrite it in an "
             "allowed form and continue; read files with the Read tool.")
_ID = cite.ID_FORMAT


class Kernel:
    def __init__(self, project: Project, worker: Worker | None = None,
                 registry_path: Path | None = None, python: str | None = None, verifier=None) -> None:
        self.p = project
        self.verifier = verifier or (cite.verify_sources if project.verify_citations else None)
        self.worker = worker or claude_worker
        self.registry_path = registry_path or R.default_registry()
        self.python = python  # override for tests; normally chosen per question from its needs
        self.handlers = {
            "approved": self.triage, "lit": self.lit, "protocol": self.protocol, "rule_frozen": self.script,
            "prereg_frozen": self.run_analysis, "ran": self.review, "reviewed": self.decide,
            "confirming": self.confirm, "confirm_ran": self.gate, "awaiting_batch": self.close_batch,
            "decided": self.report, "writing": self.writeup,
        }
        self._lock = threading.Lock()

    # ---- loop ----

    def ready(self) -> list[dict]:
        everything = Q.all_questions(self.p)
        open_batches = {q.get("import_batch") for q in everything if q["state"] in ACTIVE}
        qs = [q for q in everything if q["state"] in self.handlers
              and not (q["state"] == "awaiting_batch" and q.get("import_batch") in open_batches)]
        # later stages first so work in flight finishes; then value, then age
        order = list(self.handlers)
        return sorted(qs, key=lambda q: (-order.index(q["state"]), -(q.get("value") or 0), q["id"]))

    def parallel(self) -> bool:
        return sandbox.available() and (self.p.llm_concurrency > 1 or self.p.cpu_concurrency > 1)

    def tick(self) -> int:
        if not sandbox.available() and os.environ.get("RLAB_ALLOW_UNSANDBOXED") != "1":
            raise RuntimeError("sandbox-exec is unavailable, so workers could read withheld files and write anywhere; "
                               "set RLAB_ALLOW_UNSANDBOXED=1 to run anyway (serial, git write check only)")
        ready = self.ready()
        steps = 0
        for q in [q for q in ready if q["state"] in KERNEL_STATES]:
            steps += self._guarded(q)
        work = [q for q in ready if q["state"] not in KERNEL_STATES]
        if not self.parallel():
            for q in work:
                if self.day_spend() >= self.p.project_day_usd:
                    break
                steps += self._guarded(q)
        else:
            llm = threading.BoundedSemaphore(self.p.llm_concurrency)
            cpu = threading.BoundedSemaphore(self.p.cpu_concurrency)

            def step(q: dict) -> int:
                with (cpu if q["state"] in CPU_STATES else llm):
                    if self.day_spend() >= self.p.project_day_usd:
                        return 0
                    return self._guarded(q)

            # worker stages and analysis runs never overlap: a sandboxed script can read another process's
            # environment (sysctl KERN_PROCARGS2), and live workers carry the OAuth token
            for phase in ([q for q in work if q["state"] not in CPU_STATES], [q for q in work if q["state"] in CPU_STATES]):
                if phase:
                    with ThreadPoolExecutor(max_workers=max(self.p.llm_concurrency, self.p.cpu_concurrency)) as ex:
                        steps += sum(ex.map(step, phase))
        R.write_digest(self.p)
        return steps

    def _guarded(self, q: dict) -> int:
        """Run one handler; an exception blocks that question instead of stopping the kernel."""
        fresh = Q.load(self.p, q["id"])
        try:
            self.handlers[fresh["state"]](fresh)
        except Exception as e:
            Q.set_state(self.p, fresh, "blocked", blocked_from=fresh["state"],
                        blocked_reason=f"kernel error in {fresh['state']}: {type(e).__name__}: {e}"[:300])
        return 1

    def run(self, max_ticks: int = 1000) -> int:
        total = 0
        for _ in range(max_ticks):
            steps = self.tick()
            total += steps
            if steps == 0:
                break
        return total

    # ---- spend and attempts ----

    def day_spend(self) -> float:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        total = 0.0
        path = self.p.qbank / "spend.jsonl"
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("at", "").startswith(today):
                    total += float(e.get("usd", 0))
        return total

    def _spend(self, q: dict, stage: str, usd: float, session: str) -> None:
        q["spend_usd"] = round(q.get("spend_usd", 0.0) + usd, 6)
        with self._lock:
            append_jsonl(self.p.qbank / "spend.jsonl", {"at": now_iso(), "qid": q["id"], "stage": stage,
                                                       "usd": usd, "session": session})
        Q.save(self.p, q)

    def _attempt(self, q: dict, stage: str) -> bool:
        """Count an attempt before starting (a crash counts). False when attempts are exhausted."""
        attempts = q.setdefault("stage_attempts", {})
        if attempts.get(stage, 0) >= MAX_ATTEMPTS:
            Q.set_state(self.p, q, "blocked", blocked_reason=f"{stage}: {MAX_ATTEMPTS} attempts failed",
                        blocked_from=q["state"])
            return False
        attempts[stage] = attempts.get(stage, 0) + 1
        Q.save(self.p, q)
        return True

    def _fail(self, q: dict, stage: str, error: str, **extra) -> None:
        qd = Q.qdir(self.p, q["id"])
        atomic_write_json(qd / "stages" / f"{stage}.failed.json",
                          {"at": now_iso(), "error": error[-2000:], "attempt": q["stage_attempts"].get(stage), **extra})
        q.setdefault("stage_errors", {})[stage] = error[-1000:]
        Q.save(self.p, q)
        Q.event(self.p, q["id"], "stage_failed", stage=stage, error=error[-300:])
        if q["stage_attempts"].get(stage, 0) >= MAX_ATTEMPTS:
            Q.set_state(self.p, q, "blocked", blocked_reason=f"{stage}: {error[:200]}", blocked_from=q["state"])

    def _done(self, q: dict, stage: str, **info) -> None:
        atomic_write_json(Q.qdir(self.p, q["id"]) / "stages" / f"{stage}.done.json", {"at": now_iso(), **info})
        q.get("stage_errors", {}).pop(stage, None)

    # ---- write boundary ----

    def _git_dirty(self) -> set[str] | None:
        try:
            out = subprocess.run(["git", "status", "--porcelain", "-uall"], cwd=self.p.root,
                                 capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode != 0:
            return None
        return {line[3:].strip().strip('"') for line in out.stdout.splitlines() if len(line) > 3}

    def _outside_writes(self, before: set[str], q: dict) -> list[str] | None:
        """Paths changed outside the question folder; None when git cannot tell (fail closed)."""
        after = self._git_dirty()
        if after is None:
            return None
        rel = str(Q.qdir(self.p, q["id"]).relative_to(self.p.root))
        if sandbox.available():  # other questions and the lead change files concurrently; the sandbox confines workers
            rel = str(self.p.questions_dir.relative_to(self.p.root))
        allowed = (rel + "/", "qbank/spend.jsonl", "qbank/digest.md", "qbank/fdr.jsonl", "qbank/fdr_batches.jsonl",
                   "qbank/lead/", "qbank/NEEDS_MIKE.md", "qbank/run.pid", "qbank/run.log", "qbank/ledger.md")
        return sorted(p for p in after - before if not p.startswith(allowed))

    # ---- LLM stage runner ----

    def _verify(self, sources: list[dict]) -> tuple[list[dict], list[dict]]:
        """Run the citation verifier; any failure leaves the sources in place, unverified."""
        if not self.verifier or not sources:
            return sources, []
        try:
            kept, rejected = self.verifier(sources)
            return list(kept), list(rejected)
        except Exception:
            return [{**s, "verified": None} for s in sources], []

    def _master_salt(self) -> str:
        with self._lock:
            if not SALT_FILE.exists():
                SALT_FILE.parent.mkdir(parents=True, exist_ok=True)
                try:  # created 0600 from the start; O_EXCL so two kernels cannot both write one
                    fd = os.open(SALT_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as f:
                        f.write(secrets.token_hex(32))
                except FileExistsError:
                    pass
        master = SALT_FILE.read_text().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", master):
            raise RuntimeError(f"split salt {SALT_FILE} is malformed; refusing to run analyses")
        return master

    def _salt(self, qid: str) -> str:
        """Per-question salt: a leak exposes one question's holdout, not the project's."""
        return hmac.new(bytes.fromhex(self._master_salt()), f"{self.p.name}:{qid}".encode(), hashlib.sha256).hexdigest()

    def _hidden(self, holdout_readable: bool = False) -> list[Path]:
        return [SALT_FILE, TOKEN_FILE] + ([] if holdout_readable else [self.p.abs(h) for h in self.p.holdout])

    def _owned_dirs(self, qd: Path) -> None:
        """Refuse a planted symlink (or file) where the kernel expects its own folder."""
        for name in OWNED:
            path = qd / name
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise RuntimeError(f"{name} in {qd.name} is not a plain folder (symlink or file planted)")

    def _config_dir(self, q: dict, role: str) -> Path:
        """A fresh-enough `claude` config dir for this question and role, carrying the user's spend guard hook."""
        if WORKER_HOME.resolve().is_relative_to(self.p.root.resolve()):
            raise RuntimeError(f"worker config home {WORKER_HOME} is inside the project; set RLAB_WORKER_HOME elsewhere")
        d = WORKER_HOME / self.p.name / q["id"] / role
        d.mkdir(parents=True, exist_ok=True)
        (d / "projects").mkdir(exist_ok=True)  # the sandbox forbids creating it (it cannot be renamed away either)
        try:
            user = json.loads(USER_SETTINGS.read_text())
        except (OSError, ValueError):
            user = {}
        guard = [m for m in user.get("hooks", {}).get("PreToolUse", [])
                 if any("spend_guard" in h.get("command", "") for h in m.get("hooks", []))]
        if not guard and REQUIRE_SPEND_GUARD:
            raise RuntimeError(f"no spend_guard PreToolUse hook in {USER_SETTINGS}; refusing to start workers without it "
                               "(set RLAB_SPEND_GUARD=off to run without one)")
        atomic_write_json(d / "settings.json", {"hooks": {"PreToolUse": guard}} if guard else {})
        return d

    def _tmpdir(self, q: dict, stage: str) -> Path:
        qd = Q.qdir(self.p, q["id"])
        self._owned_dirs(qd)
        d = qd / "review" / "work" if stage == "review" else qd / "scratch"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _worker_profile(self, q: dict, stage: str) -> str | None:
        if not sandbox.available():
            return None
        qd = Q.qdir(self.p, q["id"])
        self._owned_dirs(qd)
        if stage == "review":  # sees only its own folder (copies of rule, code and results) and the data cache
            return sandbox.profile(claude_config=self._config_dir(q, "review"), write=[qd / "review" / "work"],
                                   deny_read=[*self._hidden(), self.p.questions_dir, WORKER_HOME / self.p.name],
                                   allow_read=[qd / "review", qd / "cache", self._config_dir(q, "review")],
                                   deny_write_exact=[qd, qd / "review"])
        return sandbox.profile(claude_config=self._config_dir(q, "author"), write=[qd],
                               deny_read=[*self._hidden(), self.p.questions_dir, WORKER_HOME / self.p.name],
                               allow_read=[qd, self._config_dir(q, "author")],
                               deny_write=[qd / f for f in PROTECTED], deny_write_exact=[qd])

    def _run_profile(self, qd: Path, split: str) -> str | None:
        if not sandbox.available():
            return None
        self._owned_dirs(qd)
        return sandbox.profile(write=[qd / "run", qd / "cache", qd / "scratch"],
                               deny_read=[*self._hidden(holdout_readable=split == "holdout"), self.p.questions_dir,
                                          WORKER_HOME], allow_read=[qd],
                               deny_write_exact=[qd, qd / "run", qd / "cache", qd / "scratch"])

    def _checked(self, sources: list[dict]) -> list[dict]:
        """Sources that count as evidence: all of them without a verifier, else only resolved ones."""
        if not self.verifier:
            return sources
        return [s for s in sources if s.get("verified") in (True, "id_only")]

    def _llm(self, q: dict, stage: str, prompt: str, tools: list[str], denied: list[str] | None = None,
             cwd: Path | None = None) -> dict | None:
        budget = self.p.stage_budget.get(stage, 1.0)
        if q.get("spend_usd", 0.0) + budget > self.p.question_usd:
            Q.set_state(self.p, q, "blocked", blocked_reason=f"question cap ${self.p.question_usd} would be exceeded by {stage}",
                        blocked_from=q["state"])
            return None
        if not self._attempt(q, stage):
            return None
        schema = json.loads((PKG / "schemas" / f"{stage}.json").read_text())
        if teams.TEAMS.get(stage):  # the stage worker leads a team; its output records each specialist's finding
            schema["properties"]["team"] = teams.TEAM_SCHEMA
            schema["required"] = [*schema["required"], "team"]
            prompt += teams.instructions(stage)
            tools = [*tools, "Agent"]
        previous = q.get("stage_errors", {}).get(stage)
        if previous:
            prompt += f"\n\nYour previous attempt failed validation or errored: {previous}\nFix that this time."
        qd = Q.qdir(self.p, q["id"])
        call = StageCall(
            stage=stage, prompt=prompt, system_prompt_file=PKG / "prompts" / f"{stage}.md", schema=schema,
            cwd=cwd or qd, model=self.p.models.get(stage, "sonnet"), budget_usd=budget,
            max_turns=MAX_TURNS.get(stage, 20), allowed_tools=tools,
            # "//" marks an absolute path in permission rules; a single "/" is relative to the settings file
            disallowed_tools=[f"Read(/{self.p.abs(h)}/**)" for h in self.p.holdout] + (denied or []),
            add_dirs=[self.p.abs(self.p.core_facility)] if self.p.core_facility else [],
            sandbox=self._worker_profile(q, stage),
            agents=teams.agents_for(stage),
            env={"TMPDIR": str(self._tmpdir(q, stage)), "CLAUDE_CODE_TMPDIR": str(self._tmpdir(q, stage)),
                 "CLAUDE_CONFIG_DIR": str(self._config_dir(q, "review" if stage == "review" else "author"))},
        )
        before = self._git_dirty()
        if before is None:
            Q.set_state(self.p, q, "blocked", blocked_from=q["state"],
                        blocked_reason=f"no write boundary: git status failed in {self.p.root} (is it a git repo?)")
            return None
        try:
            res: WorkerResult = self.worker(call)
        except Exception as e:  # a crashing worker is a failed attempt, not a crashed kernel
            self._fail(q, stage, f"worker raised {type(e).__name__}: {e}")
            return None
        self._spend(q, stage, res.cost_usd, res.session_id)
        outside = self._outside_writes(before, q)
        if outside is None:
            Q.set_state(self.p, q, "blocked", blocked_reason=f"{stage}: write boundary could not be checked after the stage",
                        blocked_from=q["state"])
            return None
        if outside:
            Q.set_state(self.p, q, "blocked", blocked_reason=f"{stage} wrote outside its folder: {outside[:5]}",
                        blocked_from=q["state"])
            return None
        if not res.ok:
            self._fail(q, stage, f"{res.subtype}: {res.error}", session=res.session_id)
            return None
        errors = validate(res.output, schema)
        if errors:
            self._fail(q, stage, "schema: " + "; ".join(errors[:8]), session=res.session_id)
            return None
        missing = teams.missing_roles(stage, res.output)
        if missing:
            self._fail(q, stage, f"team: no finding recorded from {', '.join(missing)}; consult every specialist",
                       session=res.session_id)
            return None
        self._done(q, stage, cost_usd=res.cost_usd, session=res.session_id)
        return res.output

    def _context(self, q: dict) -> str:
        lines = [f"Project: {self.p.name}. {self.p.description}".strip(),
                 f"Question {q['id']}: {q['text']}"]
        if q.get("decision_it_drives"):
            lines.append(f"Decision it drives: {q['decision_it_drives']}")
        if q.get("family_hint"):
            lines.append(f"Suggested family (from the question bank): {q['family_hint']}")
        if self.p.core_facility:
            lines.append(f"Shared core facility (read-only): {self.p.abs(self.p.core_facility)}")
        if self.p.holdout:
            lines.append("Holdout paths you must never read: " + ", ".join(str(self.p.abs(h)) for h in self.p.holdout))
        return "\n".join(lines)

    def _web_tools(self) -> list[str]:
        domains = list(dict.fromkeys([*self.p.web_domains, *SEARCH_APIS]))
        return (["WebSearch"] if self.p.web_search else []) + [f"WebFetch(domain:{d})" for d in domains]

    # ---- stages ----

    def triage(self, q: dict) -> None:
        others = [f"{o['id']}: {o['text']}" for o in Q.all_questions(self.p) if o["id"] != q["id"]][:200]
        prompt = self._context(q) + "\n\nOther questions in this project (for duplicate detection):\n" + "\n".join(others)
        out = self._llm(q, "triage", prompt, ["Read", "Grep", "Glob", *self._web_tools()])
        if out is None:
            return
        atomic_write_json(Q.qdir(self.p, q["id"]) / "triage.json", out)
        q["type"] = q.get("type") or out["type"]
        q.update(test_family=out["test_family"], value=out["value"], data_sources=out["data_sources"])
        route = out["route"]
        sourced = [a for a in out["prior_art"] if _ID.match(a.get("id", "").strip())]
        sourced, rejected = self._verify(sourced)
        q["rejected_sources"] = rejected
        if route == "already_answered" and not self._checked(sourced):
            route = "proceed"  # fail-closed: no citable source, so it is not answered yet
            q["type"] = "lit"
        if route == "proceed" or (route == "duplicate" and not out.get("duplicate_of")):
            gates = envs.unmet_gates(q.get("needs"), self.p.gates)
            if gates and q["type"] != "lit":
                Q.set_state(self.p, q, "blocked", blocked_from="protocol",
                            blocked_reason="needs project setting " + ", ".join(f"[gates] {g} = true" for g in gates))
                return
            Q.set_state(self.p, q, "lit" if q["type"] == "lit" else "protocol")
        elif route == "already_answered":
            Q.set_state(self.p, q, "decided", verdict="answered", evidence_grade=out.get("evidence_grade", "low"),
                        verdict_reason=out["reason"], sources=sourced)
        elif route == "duplicate":
            q.setdefault("links", {})["duplicate_of"] = out["duplicate_of"]
            Q.set_state(self.p, q, "decided", verdict="duplicate", verdict_reason=out["reason"])
        else:
            Q.set_state(self.p, q, "decided", verdict="parked", verdict_reason=out["reason"])

    def lit(self, q: dict) -> None:
        out = self._llm(q, "lit", self._context(q), ["Read", "Grep", "Glob", *self._web_tools()])
        if out is None:
            return
        qd = Q.qdir(self.p, q["id"])
        atomic_write_json(qd / "lit" / "evidence.json", out)
        sourced = [s for s in out["sources"] if _ID.match(s.get("id", "").strip())]
        sourced, rejected = self._verify(sourced)
        if out["answered"] and sourced and not self._checked(sourced):
            # fail closed, but keep the work: the evidence is saved and the question can be retried
            Q.set_state(self.p, q, "blocked", blocked_from="lit", sources=sourced,
                        blocked_reason="claimed answered but no cited source could be verified (resolver unreachable)")
            return
        answered = out["answered"] and bool(self._checked(sourced))
        reason = out["summary"] if answered else (
            out["summary"] if out["answered"] is False else "claimed answered but no citable source: " + out["summary"])
        if rejected:
            reason += " [rejected sources: " + "; ".join(f"{r.get('id')} ({r['reject_reason']})" for r in rejected) + "]"
        unverified = [s.get("id") for s in sourced if self.verifier and s.get("verified") is None]
        if answered and unverified:
            reason += f" [{len(unverified)} source(s) unverified: resolver unreachable]"
        Q.set_state(self.p, q, "decided", verdict="answered" if answered else "unanswerable",
                    evidence_grade=out["grade"] if answered else "none", verdict_reason=reason,
                    sources=sourced, rejected_sources=rejected, lit_checked_at=now_iso())

    def protocol(self, q: dict) -> None:
        """Rule stage: the worker writes the preregistration with no shell and no local data access."""
        qd = Q.qdir(self.p, q["id"])
        profile = envs.profile_for(q.get("needs"))
        prompt = (self._context(q) + f"\n\nQuestion type: {q.get('type')}. Data sources suggested at triage: "
                  f"{q.get('data_sources')}.\nAnalysis environment for the later script: '{profile}' with "
                  f"{envs.LIBS[profile]}.")
        if q.get("prior_work") or q.get("gap"):
            prompt += f"\nClosest prior work: {q.get('prior_work')}. Gap this question fills: {q.get('gap')}."
        out = self._llm(q, "protocol", prompt, self._web_tools(), denied=["Bash", "Read", "Write", "Edit", "Glob", "Grep"])
        if out is None:
            return
        out["script"] = "code/analysis.py"  # fixed: the script stage writes exactly this file
        problems = []
        if q.get("type") == "ml" and out["seeds"] < 3:
            problems.append("ml questions need seeds >= 3")
        if out["decision_rule"]["metric"] in out["negative_controls"]:
            problems.append("decision metric cannot also be a negative control")
        if problems:
            self._fail(q, "protocol", "; ".join(problems))
            return
        atomic_write_json(qd / "prereg.json", out)
        R.write_prereg_md(qd, q, out)
        Q.set_state(self.p, q, "rule_frozen", prereg_sha=sha256_file(qd / "prereg.json"), family=out["family"],
                    alpha=out["alpha"])

    def script(self, q: dict) -> None:
        """Script stage: the worker implements the frozen rule; changing prereg.json blocks the question."""
        qd = Q.qdir(self.p, q["id"])
        if sha256_file(qd / "prereg.json") != q.get("prereg_sha"):
            Q.set_state(self.p, q, "blocked", blocked_reason="preregistration changed after the rule was frozen",
                        blocked_from=q["state"])
            return
        (qd / "code").mkdir(parents=True, exist_ok=True)
        profile = envs.profile_for(q.get("needs"))
        python, real_env = envs.python_for(profile)
        prompt = (self._context(q) + "\n\nThe preregistration in prereg.json is frozen (hashed): implement it exactly "
                  "in code/analysis.py and do not edit prereg.json.\n"
                  f"Analysis environment: '{profile}' with {envs.LIBS[profile]}. The kernel will run your script with "
                  f"{python}; test it the same way ({python} code/analysis.py --out scratch/check.json, on a small slice "
                  f"first).\nThe kernel's unit splitter rlab_split.py is in {RUNTIME}.\n" + BASH_NOTE.format(forms=f"`{python}`, `python3`, `mkdir`, `ls`"))
        if DATA_DIR.is_dir():
            prompt += f"\nManually downloaded datasets (if needed) are under {DATA_DIR}/<dataset>/."
        tools = ["Read", "Grep", "Glob", "Write(code/**)", "Edit(code/**)", "Bash(python3 *)", f"Bash({python} *)",
                 "Bash(mkdir *)", "Bash(ls *)", *self._web_tools()]
        out = self._llm(q, "script", prompt, tools, denied=["Write(prereg.json)", "Edit(prereg.json)"])
        if out is None:
            return
        if sha256_file(qd / "prereg.json") != q.get("prereg_sha"):
            Q.set_state(self.p, q, "blocked", blocked_reason="script stage changed the frozen preregistration",
                        blocked_from=q["state"])
            return
        script = qd / "code" / "analysis.py"
        if not script.is_file():
            self._fail(q, "script", "code/analysis.py was not written")
            return
        odd = [p.relative_to(qd).as_posix() for p in (qd / "code").rglob("*")
               if p.is_symlink() or p.name == "__pycache__" or p.suffix == ".pyc"]
        if odd:
            self._fail(q, "script", f"code/ may hold only source files (no symlinks or bytecode): {odd[:5]}")
            return
        Q.set_state(self.p, q, "prereg_frozen", script_sha=tree_sha(qd / "code"), env=profile, env_python=python,
                    env_fallback=not real_env, script_tested=out["tested"])

    def _python_for(self, q: dict) -> str:
        if self.python:
            return self.python
        return q.get("env_python") or envs.python_for(envs.profile_for(q.get("needs")))[0]

    def _exec(self, qd: Path, script: Path, out: Path, python: str, split: str = "discovery") -> tuple[int, str]:
        env = sandbox.clean_env()
        env.update(RLAB_SPLIT=split, RLAB_SPLIT_SALT=self._salt(qd.name), RLAB_HOLDOUT_FRAC=str(self.p.holdout_frac),
                   PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(qd / "scratch"),
                   MPLCONFIGDIR=str(qd / "cache" / "mpl"), XDG_CACHE_HOME=str(qd / "cache"), HF_HOME=str(qd / "cache" / "hf"),
                   PIP_CACHE_DIR=str(qd / "cache" / "pip"), TORCH_HOME=str(qd / "cache" / "torch"),
                   PYTHONPATH=os.pathsep.join(filter(None, [str(RUNTIME), env.get("PYTHONPATH", "")])))
        run = qd / "run"
        if run.is_symlink() or (run.exists() and not run.is_dir()):
            return 1, "run/ is not a plain directory"
        run.mkdir(exist_ok=True)
        (qd / "cache").mkdir(exist_ok=True)
        (qd / "scratch").mkdir(exist_ok=True)
        out.unlink(missing_ok=True)  # never read a stale or planted result
        argv = sandbox.wrap([python, str(script), "--out", str(out)], self._run_profile(qd, split))
        try:
            proc = subprocess.run(argv, cwd=qd, env=env, capture_output=True, text=True, timeout=self.p.run_timeout_s)
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {self.p.run_timeout_s}s"
        return proc.returncode, (proc.stdout[-4000:] + "\n" + proc.stderr[-4000:]).strip()

    def _result(self, path: Path):
        """A result file the script wrote: a plain file inside its folder, never a symlink."""
        if path.is_symlink() or not path.is_file():
            return None
        return read_json(path)

    def _frozen(self, q: dict, qd: Path) -> bool:
        return sha256_file(qd / "prereg.json") == q.get("prereg_sha") and tree_sha(qd / "code") == q.get("script_sha")

    def run_analysis(self, q: dict) -> None:
        qd = Q.qdir(self.p, q["id"])
        prereg = read_json(qd / "prereg.json", {})
        script = qd / prereg.get("script", "code/analysis.py")
        if not script.is_file() or not self._frozen(q, qd):
            Q.set_state(self.p, q, "blocked", blocked_reason="preregistration or script changed after freezing",
                        blocked_from=q["state"])
            return
        if not self._attempt(q, "run"):
            return
        started = datetime.now(timezone.utc)
        python = self._python_for(q)
        split = "discovery" if self.p.require_confirmation else "all"
        code, log = self._exec(qd, script, qd / "run" / "results.json", python, split)
        atomic_write_text(qd / "run" / "log.txt", log)  # replaces, never follows, a planted symlink
        results = self._result(qd / "run" / "results.json")
        if code != 0 or not isinstance(results, dict) or not isinstance(results.get("effects"), dict):
            self._fail(q, "run", f"exit {code}; results missing or malformed. Log tail: {log[-800:]}")
            return
        seconds = (datetime.now(timezone.utc) - started).total_seconds()
        reproduced = None  # None = not rerun; decide() treats that as unconfirmed
        if self.p.reproduce_max_s is None or seconds <= self.p.reproduce_max_s:
            code2, _ = self._exec(qd, script, qd / "run" / "results_repro.json", python, split)
            again = self._result(qd / "run" / "results_repro.json")
            reproduced = code2 == 0 and isinstance(again, dict) and all(
                again.get(k) == results.get(k) for k in REPRO_KEYS)
        self._done(q, "run", seconds=round(seconds, 2), reproduced=reproduced)
        (qd / "kernel").mkdir(exist_ok=True)  # later runs can write run/, never kernel/
        atomic_write_json(qd / "kernel" / "results.json", results)
        Q.set_state(self.p, q, "ran", results_sha=sha256_file(qd / "kernel" / "results.json"),
                    run_seconds=round(seconds, 2), reproduced=reproduced,
                    salt_sha=hashlib.sha256(self._salt(q["id"]).encode()).hexdigest())

    def review(self, q: dict) -> None:
        """Independent review, sealed by the sandbox: the reviewer works in review/ and can read only copies of the
        frozen rule (without hypothesis or notes), the code and the results, plus the data cache."""
        qd = Q.qdir(self.p, q["id"])
        rd = qd / "review"
        if rd.exists():
            shutil.rmtree(rd)
        (rd / "work").mkdir(parents=True)
        prereg = read_json(qd / "prereg.json", {})
        atomic_write_json(rd / "rule.json", {k: prereg[k] for k in REVIEW_FIELDS if k in prereg})
        shutil.copytree(qd / "code", rd / "code", symlinks=True)  # never dereference a planted link
        shutil.copyfile(qd / "kernel" / "results.json", rd / "results.json")
        python = self._python_for(q)
        prompt = (f"Review question {q['id']}: {q['text']}\n"
                  "Files in your working directory: rule.json (the frozen rule), code/analysis.py, results.json. "
                  f"Re-run with {python} code/analysis.py --out work/review_results.json (work/ is writable).\n"
                  f"The kernel's unit splitter rlab_split.py is in {RUNTIME}.\n"
                  + BASH_NOTE.format(forms=f"`{python}`, `python3`"))
        out = self._llm(q, "review", prompt, ["Read", "Grep", "Glob", "Bash(python3 *)", f"Bash({python} *)"], cwd=rd)
        if out is None:
            return
        atomic_write_json(rd / "review.json", out)
        Q.set_state(self.p, q, "reviewed", review_severity=out["severity"])

    @staticmethod
    def _split_ok(split, mode: str) -> bool:
        if not isinstance(split, dict):
            return False
        seen, kept = split.get("units_seen"), split.get("units_kept")
        return (split.get("mode") == mode and split.get("kernel_salt") is True and rules._num(seen)
                and rules._num(kept) and 0 < kept < seen)

    def _scaled(self, rule: dict, split: str) -> dict:
        """min_n is preregistered for all units; each split sees only its share (10% slack for hash variation)."""
        share = {"discovery": 1 - self.p.holdout_frac, "holdout": self.p.holdout_frac}.get(split, 1.0)
        if isinstance(rule, dict) and rules._num(rule.get("min_n")) and share < 1:
            return {**rule, "min_n": int(rule["min_n"] * share * 0.9)}
        return rule

    def decide(self, q: dict) -> None:
        qd = Q.qdir(self.p, q["id"])
        if not self._frozen(q, qd) or sha256_file(qd / "kernel" / "results.json") != q.get("results_sha"):
            Q.set_state(self.p, q, "blocked", blocked_from=q["state"],
                        blocked_reason="preregistration, script or results changed after the run")
            return
        prereg, results = read_json(qd / "prereg.json", {}), read_json(qd / "kernel" / "results.json", {})
        try:
            v = rules.evaluate(self._scaled(prereg.get("decision_rule"), "discovery" if self.p.require_confirmation
                                            else "all"), results, prereg.get("negative_controls"),
                               n_declared=prereg.get("n_comparisons") or 1, alpha=prereg.get("alpha") or 0.05)
        except Exception as e:  # malformed script output must never wedge the kernel
            v = rules.Verdict("invalid", f"results could not be evaluated: {type(e).__name__}")
        verdict, reason = v.verdict, v.reason
        review = read_json(qd / "review" / "review.json", {})
        if verdict == "pass":
            if q.get("type") == "ml" and (results.get("seeds") or 0) < 3:
                verdict, reason = "provisional", "ML result from fewer than 3 seeds"
            elif review.get("severity") == "major":
                verdict, reason = "provisional", "reviewer found major issues: " + "; ".join(review.get("issues", [])[:3])
            elif q.get("reproduced") is False:
                verdict, reason = "provisional", "rerun did not reproduce the results"
            elif q.get("reproduced") is None:
                verdict, reason = "provisional", "analysis was not rerun (longer than reproduce_max_s), so it is unconfirmed"
        rule = prereg.get("decision_rule") or {}
        metric = rule.get("metric")
        effects = results.get("effects") if isinstance(results.get("effects"), dict) else {}
        effect = effects.get(metric)
        fields = dict(effect=effect, metric=metric, opposite_direction=v.opposite, n_comparisons=v.n_comparisons)
        if self.p.require_confirmation:
            if verdict == "pass":
                if self._split_ok(results.get("split"), "discovery"):
                    Q.set_state(self.p, q, "confirming", discovery_verdict=verdict, discovery_reason=reason, **fields)
                    return
                verdict, reason = "provisional", ("discovery pass, but the analysis did not split units through "
                                                  "rlab_split, so it cannot be confirmed on held-out data")
        elif rule and v.verdict != "invalid":
            # no confirmation step: every decided test enters multiple-testing control, passes and failures alike
            self._finish(q, rule, effect, v.n_comparisons, reason, verdict, **fields)
            return
        Q.set_state(self.p, q, "decided", verdict=verdict, verdict_reason=reason, **fields)

    def _finish(self, q: dict, rule: dict, block, m: int, reason: str, verdict: str, **fields) -> None:
        """Multiple-testing step for a test that reached its final p-value (holdout, or discovery when there is no
        confirmation). batch_bh: wait for the import batch to close, then Benjamini-Hochberg across its tests.
        lord: decide now against the project's LORD++ stream."""
        p = rules.one_sided_p(rule, block, m)
        p = 1.0 if p is None else p
        if self.p.fdr_mode == "lord":
            verdict, reason, extra = self._fdr_gate(q, p, reason, verdict)
            Q.set_state(self.p, q, "decided", verdict=verdict, verdict_reason=reason, **fields, **extra)
        else:
            Q.set_state(self.p, q, "awaiting_batch", pending_verdict=verdict, pending_reason=reason, fdr_p=p, **fields)

    def close_batch(self, q: dict) -> None:
        """Benjamini-Hochberg across every test of this import batch that is waiting (all of them, once the batch
        has nothing left in flight; questions finishing later form their own set). Each verdict is made once."""
        batch = q.get("import_batch")
        members = sorted((o for o in Q.all_questions(self.p) if o["state"] == "awaiting_batch"
                          and o.get("import_batch") == batch), key=lambda o: o["id"])
        ps = [o["fdr_p"] for o in members]
        adj = fdr.bh_adjusted(ps)
        alpha = self.p.fdr_alpha
        with self._lock:
            append_jsonl(self.p.qbank / "fdr_batches.jsonl", {"at": now_iso(), "batch": batch, "alpha": alpha,
                                                              "tests": [[o["id"], p, a] for o, p, a in zip(members, ps, adj)]})
        for o, p, a in zip(members, ps, adj):
            o = Q.load(self.p, o["id"])
            verdict, reason = o.get("pending_verdict"), o.get("pending_reason", "")
            tag = f"BH across {len(members)} test(s) of batch {batch}: p={p:.3g}, q={a:.3g}"
            if verdict == "pass":
                if a <= alpha:
                    reason = f"{reason}; clears {tag} <= {alpha}"
                else:
                    verdict, reason = "provisional", f"{reason}; does not clear {tag} > {alpha}"
            else:
                reason = f"{reason}; {tag}"
            Q.set_state(self.p, o, "decided", verdict=verdict, verdict_reason=reason, fdr_q=a, fdr_m=len(members),
                        fdr_method="BH")

    def _fdr_gate(self, q: dict, p: float, reason: str, verdict: str) -> tuple[str, str, dict]:
        """Record this test in the LORD++ stream. A nominal pass stays a pass only if it clears its LORD++ level;
        any other verdict is returned unchanged."""
        with self._lock:
            e = fdr.test(self.p.qbank / "fdr.jsonl", q["id"], p, self.p.fdr_alpha)
        extra = {"fdr_p": e["p"], "fdr_alpha_t": e["alpha_t"], "fdr_t": e["t"]}
        if verdict != "pass":
            return verdict, reason, extra
        if e["reject"]:
            return "pass", f"{reason}; clears online FDR (LORD++ test {e['t']}: p={e['p']:.3g} <= {e['alpha_t']:.3g})", extra
        return "provisional", (f"{reason}; does not clear online FDR (LORD++ test {e['t']}: "
                               f"p={e['p']:.3g} > level {e['alpha_t']:.3g})"), extra

    def confirm(self, q: dict) -> None:
        """Kernel-only, parallel: run the frozen script on the held-out units. The verdict is made by gate()."""
        qd = Q.qdir(self.p, q["id"])
        prereg = read_json(qd / "prereg.json", {})
        script = qd / prereg.get("script", "code/analysis.py")
        if not self._frozen(q, qd) or hashlib.sha256(self._salt(q["id"]).encode()).hexdigest() != q.get("salt_sha"):
            Q.set_state(self.p, q, "blocked", blocked_reason="preregistration, script or split salt changed before "
                        "confirmation", blocked_from=q["state"])
            return
        if not self._attempt(q, "confirm"):
            return
        code, log = self._exec(qd, script, qd / "run" / "results_holdout.json", self._python_for(q), "holdout")
        atomic_write_text(qd / "run" / "log_holdout.txt", log)
        held = self._result(qd / "run" / "results_holdout.json")
        if code != 0 or not isinstance(held, dict) or not isinstance(held.get("effects"), dict):
            self._fail(q, "confirm", f"exit {code}; holdout results missing or malformed. Log tail: {log[-800:]}")
            return
        self._done(q, "confirm")
        atomic_write_json(qd / "kernel" / "results_holdout.json", held)
        Q.set_state(self.p, q, "confirm_ran", holdout_sha=sha256_file(qd / "kernel" / "results_holdout.json"))

    def gate(self, q: dict) -> None:
        """Kernel-only, serial: apply the rule to the holdout run and enter it in the FDR stream in one order."""
        qd = Q.qdir(self.p, q["id"])
        prereg, disc = read_json(qd / "prereg.json", {}), read_json(qd / "kernel" / "results.json", {})
        held = read_json(qd / "kernel" / "results_holdout.json")
        if (not isinstance(held, dict) or sha256_file(qd / "kernel" / "results_holdout.json") != q.get("holdout_sha")
                or sha256_file(qd / "kernel" / "results.json") != q.get("results_sha")):
            Q.set_state(self.p, q, "blocked", blocked_from=q["state"], blocked_reason="discovery or holdout results changed")
            return
        rule = prereg.get("decision_rule") or {}
        try:
            v = rules.evaluate(self._scaled(rule, "holdout"), held, prereg.get("negative_controls"),
                               n_declared=prereg.get("n_comparisons") or 1, alpha=prereg.get("alpha") or 0.05)
        except Exception as e:
            v = rules.Verdict("invalid", f"holdout results could not be evaluated: {type(e).__name__}")
        effects = held.get("effects") if isinstance(held.get("effects"), dict) else {}
        block = effects.get(rule.get("metric"))
        problems = []
        ds, hs = disc.get("split") or {}, held.get("split")
        if not self._split_ok(hs, "holdout"):
            problems.append("holdout run did not report a valid holdout split")
        elif hs["units_seen"] != ds.get("units_seen") or hs["units_kept"] + ds.get("units_kept", 0) != hs["units_seen"]:
            problems.append(f"discovery and holdout splits are not complementary (seen {ds.get('units_seen')}/"
                            f"{hs['units_seen']}, kept {ds.get('units_kept')}+{hs['units_kept']})")
        if effects and effects == disc.get("effects"):
            problems.append("holdout effects are identical to discovery, so the split did not reach the analysis")
        confirmed = v.verdict == "pass" and not problems
        base = f"discovery: {q.get('discovery_reason', '')}; holdout: {v.reason}"
        self._finish(q, rule, block if not problems else None, v.n_comparisons,
                     ("confirmed on held-out data. " if confirmed else "did not confirm on held-out data"
                      + (" (" + "; ".join(problems) + ")" if problems else "") + ". ") + base,
                     "pass" if confirmed else "provisional",
                     confirmed=confirmed, holdout_effect=block, holdout_verdict=v.verdict)

    def report(self, q: dict) -> None:
        R.write_card(self.p, q)
        R.register(self.p, q, self.registry_path)
        R.append_ledger(self.p, q)
        Q.set_state(self.p, q, "writing" if q.get("verdict") in RP.WRITE_UP else "reported")

    def writeup(self, q: dict) -> None:
        """GitHub write-up in the house style. Prose from a worker (read-only), structure and numbers from the kernel.
        A failed write-up is recorded, never blocks the question: the verdict is already final."""
        prompt = (f"Write up question {q['id']}: {q['text']}\nVerdict (final, from the kernel): {q.get('verdict')}. "
                  f"{q.get('verdict_reason', '')}\nRead the files listed in your instructions; return the fields.")
        out = self._llm(q, "writeup", prompt, ["Read", "Grep", "Glob"], denied=["Write", "Edit", "Bash"])
        if out is not None:
            problems = RP.style_problems(out)
            if problems:
                self._fail(q, "writeup", "house style: " + "; ".join(problems))
                out = None
        if out is None:
            if q.get("state") == "writing" and q["stage_attempts"].get("writeup", 0) < MAX_ATTEMPTS:
                return  # retried next tick with the error as feedback
            fresh = Q.load(self.p, q["id"])
            Q.set_state(self.p, fresh, "reported", writeup_error=(fresh.get("stage_errors") or {}).get("writeup")
                        or fresh.get("blocked_reason", "writeup failed"))
            return
        repo = RP.assemble(self.p, q, out, RUNTIME)
        Q.set_state(self.p, q, "reported", repo=str(repo.relative_to(Q.qdir(self.p, q["id"]))), repo_name=out["repo_name"])
        R.write_card(self.p, q)
