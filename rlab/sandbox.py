"""OS-level confinement for workers and analysis runs (macOS sandbox-exec).

Permission rules in `claude -p` only govern Claude's own tools; a python process started through
Bash can open any file. The kernel therefore wraps each worker and each analysis run in a profile
that its child processes inherit:
  - writes are default-deny: only the listed folders are writable (the question's working folders,
    /dev, and for `claude` workers their own kernel-made config dir, never the real ~/.claude);
  - every path hidden from reading is also unwritable, because renaming a file is a write;
  - credentials (cloud, ssh, gh, keychains, Claude's own transcripts and config) are never readable.

`available()` is False where sandbox-exec is missing (or RLAB_NO_SANDBOX=1); the kernel then refuses
to run unless RLAB_ALLOW_UNSANDBOXED=1.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

HOME = Path.home()
# device files a process may write; TMPDIR is set inside the question folder, shared /tmp and /var/folders stay closed
DEV = ("/dev/null", "/dev/zero", "/dev/tty", "/dev/dtracehelper", "/dev/stdout", "/dev/stderr")
DNS_SOCKET = "/private/var/run/mDNSResponder"  # the only unix socket allowed (name resolution)
SECRETS = tuple(HOME / p for p in (
    ".aws", ".ssh", ".config/gcloud", ".config/gh", ".config/rlab", ".netrc", ".docker", ".kube", ".gnupg",
    ".npmrc", ".pypirc", "Library/Keychains", ".claude/projects", ".claude.json", ".claude/.credentials.json",
    ".claude/jobs", ".codex", ".config/anthropic", ".config/openai", ".zsh_history", ".bash_history", ".zshrc",
    ".zshenv", ".zprofile", ".bashrc", ".bash_profile", ".profile", ".env"))

# environment that may pass into a sandboxed process; everything else (API keys, tokens, SSH_AUTH_SOCK) is dropped
ENV_ALLOW = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "SSL_CERT_FILE",
             "REQUESTS_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")


def clean_env(extra: dict | None = None) -> dict:
    return {**{k: v for k, v in os.environ.items() if k in ENV_ALLOW}, **(extra or {})}


def available() -> bool:
    return os.environ.get("RLAB_NO_SANDBOX") != "1" and shutil.which("sandbox-exec") is not None


def _lit(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _q(p: Path | str) -> str:
    """Write allows: resolve only the parent, so a symlink at the last component cannot redirect the rule."""
    path = Path(p).expanduser()
    parent = path.parent.resolve() if path.parent != path else path
    return _lit(str(parent / path.name) if path.name else str(parent))


def _qd(p: Path | str) -> list[str]:
    """Denies: both the path as named and its fully resolved target, so a symlinked secret is closed both ways."""
    named, real = _q(p), _lit(str(Path(p).expanduser().resolve()))
    return [named] if named == real else [named, real]


def transcripts_dir(cwd: Path | str, config_dir: Path | str) -> Path:
    """Where `claude` keeps session transcripts for a working directory under a config dir."""
    return Path(config_dir) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))


def profile(*, write: list = (), deny_read: list = (), allow_read: list = (), deny_write: list = (),
            deny_write_exact: list = (), claude_config: Path | None = None) -> str:
    """deny_read/allow_read: subpaths (allow_read carves exceptions out of deny_read).
    write: subpaths writable; deny_write: subpaths carved back out; deny_write_exact: single entries
    (e.g. the question folder itself, so it cannot be renamed away)."""
    deny_read = [*SECRETS, *deny_read]
    r = ["(version 1)", "(allow default)",
         "(deny network-outbound (remote unix-socket))",  # ssh-agent, daemons; network is otherwise open
         f"(allow network-outbound (literal {_lit(DNS_SOCKET)}))"]
    r += [f"(deny file-read* (subpath {q}))" for p in deny_read for q in _qd(p)]
    r += [f"(allow file-read* (subpath {_q(p)}))" for p in allow_read]
    r.append("(deny file-write*)")
    r += [f"(allow file-write* (literal {_lit(d)}))" for d in DEV] + ['(allow file-write* (subpath "/dev/fd"))']
    r += [f"(allow file-write* (subpath {_q(p)}))" for p in (*write, *([claude_config] if claude_config else []))]
    r += [f"(deny file-write* (subpath {q}))" for p in deny_read for q in _qd(p)]  # renaming a hidden file is a write
    # writable folders inside a hidden area that is explicitly readable again (the reviewer's review/work)
    r += [f"(allow file-write* (subpath {_q(w)}))" for w in (*write, *([claude_config] if claude_config else []))
          if any(Path(w).resolve().is_relative_to(Path(a).resolve()) for a in allow_read)]
    protected = [Path(claude_config) / n for n in ("settings.json", "hooks")] if claude_config else []
    r += [f"(deny file-write* (subpath {_q(p)}))" for p in (*deny_write, *protected)]
    exact = [*deny_write_exact, *([claude_config, Path(claude_config) / "projects"] if claude_config else [])]
    r += [f"(deny file-write* (literal {_q(p)}))" for p in exact]
    return "".join(r)


def wrap(argv: list[str], prof: str | None) -> list[str]:
    return ["sandbox-exec", "-p", prof, *argv] if prof else argv
