"""False-discovery control across a project's questions.

Default: Benjamini-Hochberg per import batch at batch closure (bh_adjusted). Optional: online LORD++
(Ramdas et al. 2017) for projects whose questions stream in without batches.

LORD++:

Each compute/ML question that reaches a verdict is one test, in decision order. Test t gets a
level alpha_t that depends only on earlier tests, so a decision is final when made (no later
question can flip an earlier pass). The stream lives in qbank/fdr.jsonl.

  alpha_t = gamma_t * W0 + (alpha - W0) * gamma_{t - tau_1} + alpha * sum_{j >= 2} gamma_{t - tau_j}
with tau_j the times of earlier discoveries and gamma a fixed decreasing sequence summing to 1.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

HORIZON = 100_000


def _gamma_raw(j: int) -> float:
    return math.log(max(j, 2)) / (j * math.exp(math.sqrt(math.log(j)))) if j >= 1 else 0.0


_NORM = sum(_gamma_raw(j) for j in range(1, HORIZON + 1))


def gamma(j: int) -> float:
    return _gamma_raw(j) / _NORM if j >= 1 else 0.0


def level(t: int, discoveries: list[int], alpha: float, w0: float | None = None) -> float:
    """Level for test t (1-based) given the 1-based times of earlier discoveries."""
    w0 = alpha / 2 if w0 is None else w0
    a = gamma(t) * w0
    for i, tau in enumerate(sorted(discoveries)):
        if tau < t:
            a += (alpha - w0 if i == 0 else alpha) * gamma(t - tau)
    return a


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def test(path: Path, qid: str, p: float, alpha: float) -> dict:
    """Record one test and return {'t', 'p', 'alpha_t', 'reject'}; idempotent per question."""
    stream = read(path)
    for e in stream:
        if e["qid"] == qid:
            return e
    t = len(stream) + 1
    a = level(t, [e["t"] for e in stream if e["reject"]], alpha)
    entry = {"t": t, "qid": qid, "p": p, "alpha_t": a, "reject": p <= a}
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def bh_adjusted(ps: list[float]) -> list[float]:
    """Benjamini-Hochberg adjusted p-values (q-values), in input order; reject where q <= alpha."""
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    adj = [1.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, ps[i] * m / rank)
        adj[i] = min(1.0, running)
    return adj
