"""Kernel-controlled discovery/holdout split, imported by analysis scripts (on PYTHONPATH).

Every unit (patient, trial, user, gene, document...) is assigned to discovery or holdout by a
salted hash. Only the kernel knows the salt, so a worker can develop and test on a dev split but
cannot tell which units the kernel will hold out.

    import rlab_split
    df = df[rlab_split.keep(df["trial_id"])]          # rows of the current split
    ...
    results["split"] = rlab_split.summary()

RLAB_SPLIT: dev (default, worker testing) | discovery | holdout | all   (set by the kernel;
"all" keeps every unit, used when the project does not require confirmation)
"""

from __future__ import annotations

import hashlib
import os

MODE = os.environ.get("RLAB_SPLIT", "dev")
_SALT = os.environ.get("RLAB_SPLIT_SALT", "")
FRAC = float(os.environ.get("RLAB_HOLDOUT_FRAC", "0.3"))
_seen: set = set()
_kept: set = set()


def _norm(unit) -> str:
    """5, 5.0 and numpy 5 are the same unit."""
    if isinstance(unit, float) and unit.is_integer():
        unit = int(unit)
    elif hasattr(unit, "item") and not isinstance(unit, (str, bytes)):
        unit = unit.item()
        if isinstance(unit, float) and unit.is_integer():
            unit = int(unit)
    return str(unit)


def is_holdout(unit) -> bool:
    salt = _SALT or "dev-only-salt"
    h = hashlib.sha256(f"{salt}:{_norm(unit)}".encode()).digest()
    return int.from_bytes(h[:8], "big") / 2 ** 64 < FRAC


def _keep_one(unit) -> bool:
    side = is_holdout(unit)
    keep = True if MODE == "all" else side if MODE == "holdout" else not side
    _seen.add(_norm(unit))
    if keep:
        _kept.add(_norm(unit))
    return keep


def keep(units):
    """Boolean mask (list) for an iterable of unit ids; works as a pandas row filter."""
    mask = [_keep_one(u) for u in units]
    try:
        import pandas as pd
    except ImportError:
        return mask
    if isinstance(units, pd.Series):  # keep the index so df[mask] aligns
        return pd.Series(mask, index=units.index)
    if isinstance(units, pd.Index):  # df.loc[keep(df.index)] needs a plain boolean array
        import numpy as np
        return np.array(mask, dtype=bool)
    return mask


def summary() -> dict:
    return {"mode": MODE, "units_seen": len(_seen), "units_kept": len(_kept), "holdout_frac": FRAC,
            "kernel_salt": bool(_SALT)}
