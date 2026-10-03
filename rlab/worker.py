"""The single place that invokes an LLM worker (`claude -p`), plus a fake for token-free tests."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from . import sandbox

# Stripped from worker environments so a worker cannot start paid compute even if a hook misses it.
CLOUD_ENV = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
             "GOOGLE_APPLICATION_CREDENTIALS", "AZURE_CLIENT_SECRET")


TOKEN_FILE = Path(os.environ.get("RLAB_TOKEN_FILE", Path.home() / ".config" / "rlab" / "oauth_token"))


def worker_env() -> dict:
    """Environment for workers: an allowlist (no cloud credentials, API keys or SSH agent) plus, if a long-lived
    token from `claude setup-token` is stored in TOKEN_FILE, that token, so headless workers never read the macOS
    keychain (which prompts for a password when locked)."""
    env = sandbox.clean_env({"PYTHONDONTWRITEBYTECODE": "1"})
    try:
        token = TOKEN_FILE.read_text().strip()
    except OSError:
        token = ""
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


@dataclass
class StageCall:
    stage: str
    prompt: str
    system_prompt_file: Path
    schema: dict
    cwd: Path
    model: str
    budget_usd: float
    max_turns: int
    allowed_tools: list[str]
    disallowed_tools: list[str] = field(default_factory=list)
    add_dirs: list[Path] = field(default_factory=list)
    sandbox: str | None = None  # sandbox-exec profile; None runs unconfined
    env: dict = field(default_factory=dict)  # extra environment (e.g. TMPDIR inside the question folder)
    agents: dict = field(default_factory=dict)  # specialist subagents for this stage's team (--agents)


@dataclass
class WorkerResult:
    ok: bool
    output: dict | None
    cost_usd: float
    subtype: str
    session_id: str
    error: str = ""


class Worker(Protocol):
    def __call__(self, call: StageCall) -> WorkerResult: ...


def claude_worker(call: StageCall, timeout_s: int = 1800) -> WorkerResult:
    session_id = str(uuid.uuid4())
    argv = [
        "claude", "-p", call.prompt,
        "--output-format", "json",
        "--json-schema", json.dumps(call.schema),
        "--append-system-prompt-file", str(call.system_prompt_file),
        "--model", call.model,
        "--max-turns", str(call.max_turns),
        "--max-budget-usd", f"{call.budget_usd:.2f}",
        "--permission-mode", "dontAsk",
        "--session-id", session_id,
    ]
    if call.allowed_tools:
        argv += ["--allowedTools", *call.allowed_tools]
    if call.disallowed_tools:
        argv += ["--disallowedTools", *call.disallowed_tools]
    for d in call.add_dirs:
        argv += ["--add-dir", str(d)]
    if call.agents:
        argv += ["--agents", json.dumps(call.agents)]
    env = {**worker_env(), **call.env}
    argv = sandbox.wrap(argv, call.sandbox)
    try:
        proc = subprocess.run(argv, cwd=call.cwd, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return WorkerResult(False, None, 0.0, "timeout", session_id, f"timed out after {timeout_s}s")
    except OSError as e:
        return WorkerResult(False, None, 0.0, "spawn_error", session_id, str(e))
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return WorkerResult(False, None, 0.0, "bad_output", session_id,
                            (proc.stdout[-500:] + proc.stderr[-500:]).strip())
    cost = float(data.get("total_cost_usd") or 0.0)
    subtype = data.get("subtype", "")
    output = data.get("structured_output")
    if data.get("is_error") or subtype != "success" or not isinstance(output, dict):
        return WorkerResult(False, output if isinstance(output, dict) else None, cost, subtype or "error",
                            session_id, str(data.get("result", ""))[-500:])
    return WorkerResult(True, output, cost, subtype, session_id)


def fake_worker(responses: dict[str, Callable[[StageCall], dict]], cost: float = 0.01, team: bool = True) -> Worker:
    """Token-free worker: responses[stage](call) returns the structured output (and may write files).
    With team=True, a stage that has specialists (call.agents) reports one finding per specialist unless the fake
    already returned a `team`."""

    def run(call: StageCall) -> WorkerResult:
        fn = responses.get(call.stage)
        if fn is None:
            return WorkerResult(False, None, 0.0, "no_fake", "fake", f"no fake for stage {call.stage}")
        out = fn(call)
        if isinstance(out, WorkerResult):
            return out
        if team and call.agents and isinstance(out, dict) and "team" not in out:
            out = {**out, "team": [{"role": r, "finding": "consulted"} for r in call.agents]}
        return WorkerResult(True, out, cost, "success", f"fake-{call.stage}")

    return run
