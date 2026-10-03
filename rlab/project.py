"""Per-project configuration: <root>/qbank/project.toml."""

from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TOML = """\
# rlab project adapter. Paths are relative to the project root unless absolute.
name = "{name}"
description = "{description}"

[paths]
ledger = "experiments.md"        # verdicts are appended here too
core_facility = ""               # shared loaders/harness, read-only for workers
data_cache = "data"
holdout = []                     # workers may never read these (confirmation only)

[web]
domains = ["eutils.ncbi.nlm.nih.gov", "www.ebi.ac.uk", "pubmed.ncbi.nlm.nih.gov"]
search = false                  # general WebSearch; off = literature search only through scholarly APIs via WebFetch

[checks]
verify_citations = true         # cited ids must resolve and titles must match (needs network)
require_confirmation = true     # a pass must also pass on the kernel-held holdout split
holdout_frac = 0.3              # share of units the kernel holds out (rlab_split)
fdr = "batch_bh"                # "batch_bh": Benjamini-Hochberg per import batch at closure; "lord": online LORD++
fdr_alpha = 0.05                # FDR level across this project's confirmatory tests

[concurrency]
llm = 3                         # worker stages in parallel (needs sandbox-exec; else 1)
cpu = 2                         # analysis runs in parallel

[caps]
question_usd = 8.0
project_day_usd = 40.0
run_timeout_s = 1800
# reproduce_max_s = 600   # skip the kernel rerun for runs longer than this (default: always rerun)

[models]
triage = "sonnet"
lit = "sonnet"
protocol = "sonnet"
script = "sonnet"
review = "opus"

[gates]
allow_llm_calls = false     # analysis scripts may call a language model (uses Mike's subscription)
allow_local_logs = false    # analysis may read Mike's local Claude Code session logs

[stage_budget_usd]
triage = 0.30
lit = 1.50
protocol = 0.50
script = 1.00
review = 1.00
"""


@dataclass
class Project:
    name: str
    root: Path
    description: str = ""
    ledger: str = "experiments.md"
    core_facility: str = ""
    data_cache: str = "data"
    holdout: list[str] = field(default_factory=list)
    web_domains: list[str] = field(default_factory=list)
    question_usd: float = 8.0
    project_day_usd: float = 40.0
    run_timeout_s: int = 1800
    reproduce_max_s: float | None = None  # None: always rerun
    verify_citations: bool = False
    web_search: bool = False
    require_confirmation: bool = True
    holdout_frac: float = 0.3
    fdr_alpha: float = 0.05
    fdr_mode: str = "batch_bh"
    llm_concurrency: int = 3
    cpu_concurrency: int = 2
    models: dict = field(default_factory=dict)
    stage_budget: dict = field(default_factory=dict)
    gates: dict = field(default_factory=dict)

    @property
    def qbank(self) -> Path:
        return self.root / "qbank"

    @property
    def questions_dir(self) -> Path:
        return self.qbank / "questions"

    def abs(self, p: str) -> Path:
        path = Path(os.path.expanduser(p))
        return path if path.is_absolute() else self.root / path


def init_project(root: Path, name: str, description: str = "") -> Path:
    toml = root / "qbank" / "project.toml"
    if not toml.exists():
        toml.parent.mkdir(parents=True, exist_ok=True)
        toml.write_text(DEFAULT_TOML.format(name=name, description=description.replace('"', "'")))
    (root / "qbank" / "questions").mkdir(parents=True, exist_ok=True)
    (root / "qbank" / "imports").mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():  # the kernel's write boundary is git status; no repo means no runs
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return toml


def load_project(root: Path) -> Project:
    root = Path(root).expanduser().resolve()
    cfg = tomllib.loads((root / "qbank" / "project.toml").read_text())
    paths, caps = cfg.get("paths", {}), cfg.get("caps", {})
    return Project(
        name=cfg.get("name", root.name),
        root=root,
        description=cfg.get("description", ""),
        ledger=paths.get("ledger", "experiments.md"),
        core_facility=paths.get("core_facility", ""),
        data_cache=paths.get("data_cache", "data"),
        holdout=list(paths.get("holdout", [])),
        web_domains=list(cfg.get("web", {}).get("domains", [])),
        question_usd=float(caps.get("question_usd", 8.0)),
        project_day_usd=float(caps.get("project_day_usd", 40.0)),
        run_timeout_s=int(caps.get("run_timeout_s", 1800)),
        reproduce_max_s=float(caps["reproduce_max_s"]) if "reproduce_max_s" in caps else None,
        verify_citations=bool(cfg.get("checks", {}).get("verify_citations", True)),  # on unless a project opts out
        web_search=bool(cfg.get("web", {}).get("search", False)),
        require_confirmation=bool(cfg.get("checks", {}).get("require_confirmation", True)),
        holdout_frac=float(cfg.get("checks", {}).get("holdout_frac", 0.3)),
        fdr_alpha=float(cfg.get("checks", {}).get("fdr_alpha", 0.05)),
        fdr_mode=str(cfg.get("checks", {}).get("fdr", "batch_bh")),
        llm_concurrency=max(1, int(cfg.get("concurrency", {}).get("llm", 3))),
        cpu_concurrency=max(1, int(cfg.get("concurrency", {}).get("cpu", 2))),
        models=dict(cfg.get("models", {})),
        stage_budget={k: float(v) for k, v in cfg.get("stage_budget_usd", {}).items()},
        gates={k: bool(v) for k, v in cfg.get("gates", {}).items()},
    )
