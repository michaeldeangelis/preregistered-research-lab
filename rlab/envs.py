"""Analysis environments: which Python runs a question's code, chosen from its [needs:...] tags.

Shared virtualenvs live in RLAB_ENVS (default ~/.dev/research-lab/envs):
  base   numpy pandas requests scipy statsmodels pyarrow openpyxl pdfplumber lifelines psycopg
  ml     base + scikit-learn rdkit
  torch  ml + torch (CPU) transformers safetensors
A missing env falls back to the interpreter running the kernel, recorded on the question.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENVS = Path(os.environ.get("RLAB_ENVS", Path.home() / ".dev" / "research-lab" / "envs"))
LIBS = {
    "base": "numpy, pandas, requests, scipy, statsmodels, pyarrow, openpyxl, pdfplumber, lifelines, psycopg",
    "ml": "numpy, pandas, requests, scipy, statsmodels, pyarrow, openpyxl, pdfplumber, lifelines, psycopg, scikit-learn, rdkit",
    "torch": "numpy, pandas, requests, scipy, statsmodels, pyarrow, openpyxl, pdfplumber, lifelines, psycopg, scikit-learn, rdkit, torch (CPU only), transformers, safetensors",
}
# needs tags that are permissions rather than libraries, and the project.toml switch for each
GATED = {"llm": "allow_llm_calls", "local-logs": "allow_local_logs"}


def profile_for(needs: list[str] | None) -> str:
    needs = {n.strip().lower() for n in needs or []}
    if needs & {"torch", "transformers"}:
        return "torch"
    if needs & {"sklearn", "scikit-learn", "rdkit"}:
        return "ml"
    return "base"


def python_for(profile: str, envs: Path = ENVS) -> tuple[str, bool]:
    """(interpreter path, True if it is the real env rather than the fallback)."""
    candidate = envs / profile / "bin" / "python"
    if candidate.is_file():
        return str(candidate), True
    return sys.executable, False


def unmet_gates(needs: list[str] | None, allowed: dict[str, bool]) -> list[str]:
    """Project settings that must be switched on before this question may run."""
    return [GATED[n] for n in (x.strip().lower() for x in needs or []) if n in GATED and not allowed.get(GATED[n])]
