"""Kernel-side verdicts. Agents write the decision rule before the run; this module applies it.

Fail-closed: anything missing or malformed can only yield `provisional` or `invalid`, never `pass`.

Rule shape (from prereg.json):
  {"metric": "delta_auroc", "comparator": "gt" | "lt", "threshold": 0.0,
   "ci_must_exclude": true, "min_n": 100, "null_value": 0.0}
Results shape (from run/results.json):
  {"effects": {"delta_auroc": {"point": .., "lo": .., "hi": .., "n": ..}},
   "controls": {"shuffled_labels": {"point": .., "lo": .., "hi": ..}}}

Multiplicity: before a pass the kernel applies a Bonferroni correction over
n = max(declared n_comparisons, distinct effect blocks reported). The 95% CI is widened to
the 1 - alpha/n level (normal approximation from the CI itself, on the log scale for ratio
metrics with null_value 1) and the pass must survive at the preregistered threshold.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

Z95 = 1.959964
MAX_COMPARISONS = 10_000


@dataclass(frozen=True)
class Verdict:
    verdict: str  # pass | kill | provisional | invalid
    reason: str
    opposite: bool = False  # CI excludes the null on the side opposite to the hypothesis
    n_comparisons: int = 1  # comparisons accounted for by the multiplicity guard


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def p_from_ci(point: float, lo: float, hi: float, null: float = 0.0) -> float | None:
    """Two-sided normal-approximation p-value from a 95% CI; None if the CI is degenerate."""
    se = (hi - lo) / (2 * Z95)
    if not se > 0:
        return None
    return math.erfc(abs(point - null) / se / math.sqrt(2))


def distinct_effects(results: dict) -> int:
    """Number of distinct effect blocks reported. Exact copies (a "best_*" alias of another
    entry) collapse to one, so selecting the best of many is counted as many."""
    seen = set()
    effects = results.get("effects")
    for block in (effects.values() if isinstance(effects, dict) else []):
        if isinstance(block, dict) and _num(block.get("point")):
            seen.add(tuple(block.get(k) if _num(block.get(k)) else None for k in ("point", "lo", "hi", "n")))
    return len(seen)


def control_is_null(block: dict, null_value: float, margin: float = 0.0) -> bool:
    """A control reads null when its CI reaches the null value, or comes within the preregistered margin of it
    (out-of-sample metrics sit slightly below null under a shuffled target: a noise predictor costs a little)."""
    lo, hi = block.get("lo"), block.get("hi")
    return _num(lo) and _num(hi) and lo <= null_value + margin and hi >= null_value - margin


def _count(n_declared) -> int:
    try:
        n = int(n_declared or 1)
    except (TypeError, ValueError, OverflowError):
        n = 1
    return min(max(n, 1), MAX_COMPARISONS)


def adjusted_interval(lo: float, hi: float, n: int, alpha: float, null_value: float, threshold: float):
    """(lo, hi, threshold, mid, se) of the interval widened to the Bonferroni level, on the scale
    the rule lives on (log for ratio metrics with null_value 1)."""
    if null_value == 1 and lo > 0 and threshold > 0:
        lo, hi, threshold = math.log(lo), math.log(hi), math.log(threshold)
    se = (hi - lo) / (2 * Z95)
    mid = (lo + hi) / 2
    z = NormalDist().inv_cdf(1 - alpha / (2 * n))
    return mid - z * se, mid + z * se, threshold, mid, se


def evaluate(rule: dict, results: dict, required_controls: list[str] | None = None,
             n_declared: int = 1, alpha: float = 0.05) -> Verdict:
    if not isinstance(rule, dict) or not isinstance(results, dict):
        return Verdict("invalid", "rule or results missing")
    metric, comparator, threshold = rule.get("metric"), rule.get("comparator"), rule.get("threshold")
    if not metric or comparator not in ("gt", "lt") or not _num(threshold):
        return Verdict("invalid", "decision rule incomplete (metric, comparator gt|lt, numeric threshold)")
    effects = results.get("effects")
    block = effects.get(metric) if isinstance(effects, dict) else None
    if not isinstance(block, dict) or not _num(block.get("point")):
        return Verdict("provisional", f"metric '{metric}' missing from results")
    null_value = rule.get("null_value", 0.0)
    if not _num(null_value):
        return Verdict("invalid", "decision rule null_value is not a finite number")
    controls = results.get("controls") if isinstance(results.get("controls"), dict) else {}
    for name in required_controls or []:
        control = controls.get(name)
        if not isinstance(control, dict):
            return Verdict("invalid", f"required control '{name}' missing")
        margin = rule.get("control_margin", 0.0)
        margin = margin if _num(margin) and margin >= 0 else 0.0
        # the agent cannot switch the control off: at most a quarter of the effect scale (distance from null to
        # threshold, or the primary CI half-width when the threshold is the null)
        half = (block.get("hi") - block.get("lo")) / 2 if _num(block.get("hi")) and _num(block.get("lo")) else 0.0
        margin = min(margin, 0.25 * max(abs(threshold - null_value), abs(half)))
        if _num(control.get("lo")) and control.get("lo") == control.get("hi"):
            return Verdict("invalid", f"control '{name}' was reported without an interval (lo == hi), so it cannot "
                           "be shown to be null; bootstrap or permute it like the primary effect")
        if not control_is_null(control, null_value, margin):
            return Verdict("invalid", f"control '{name}' is not null (possible leakage or bug)")
    min_n = rule.get("min_n", 0)
    n = block.get("n")
    if min_n and (not _num(n) or n < min_n):
        return Verdict("provisional", f"n={n} below preregistered min_n={min_n}")

    alpha = alpha if _num(alpha) and 0 < alpha <= 0.05 else 0.05  # the agent cannot loosen the guard
    m = max(_count(n_declared), distinct_effects(results))
    m = min(m, MAX_COMPARISONS)
    point, lo, hi = block["point"], block.get("lo"), block.get("hi")
    beyond = (lambda v: v > threshold) if comparator == "gt" else (lambda v: v < threshold)
    if rule.get("ci_must_exclude", True):
        if not (_num(lo) and _num(hi)):
            return Verdict("provisional", "CI required by the rule but missing")
        if not lo < hi:
            return Verdict("provisional", f"CI [{lo}, {hi}] is degenerate or inverted")
        near, far = (lo, hi) if comparator == "gt" else (hi, lo)
        if beyond(near):
            if m > 1 or alpha < 0.05:
                alo, ahi, thr, mid, se = adjusted_interval(lo, hi, m, alpha, null_value, threshold)
                survives = alo > thr if comparator == "gt" else ahi < thr
                if not survives:
                    p_adj = min(1.0, m * math.erfc(abs(mid - thr) / se / math.sqrt(2)))
                    return Verdict("provisional", f"{metric} CI [{lo:.4g}, {hi:.4g}] is beyond {threshold} but does not "
                                   f"survive Bonferroni over {m} comparisons (adjusted p {p_adj:.4g})", n_comparisons=m)
            return Verdict("pass", f"{metric} CI [{lo:.4g}, {hi:.4g}] entirely beyond {threshold}", n_comparisons=m)
        if not beyond(far):
            opposite = (hi < null_value) if comparator == "gt" else (lo > null_value)
            note = " (significant in the OPPOSITE direction to the hypothesis)" if opposite else ""
            return Verdict("kill", f"{metric} CI [{lo:.4g}, {hi:.4g}] entirely on the null side of {threshold}{note}",
                           opposite=opposite)
        return Verdict("provisional", f"{metric} CI [{lo:.4g}, {hi:.4g}] crosses {threshold}")
    if beyond(point):
        if m > 1:
            return Verdict("provisional", f"{m} comparisons reported or declared but the rule does not require a CI, "
                           "so multiplicity cannot be applied", n_comparisons=m)
        return Verdict("pass", f"{metric}={point:.4g} beyond {threshold} (no CI required)")
    return Verdict("kill", f"{metric}={point:.4g} not beyond {threshold}")


def one_sided_p(rule: dict, block: dict, n_comparisons: int = 1) -> float | None:
    """One-sided p-value against the rule's threshold (normal approximation from the 95% CI, log
    scale for ratio metrics), Bonferroni-adjusted for the question's own comparisons. None when the
    block has no usable CI."""
    if not isinstance(rule, dict) or not isinstance(block, dict):
        return None
    lo, hi, threshold = block.get("lo"), block.get("hi"), rule.get("threshold")
    if not (_num(lo) and _num(hi) and _num(threshold)) or not lo < hi:
        return None
    if rule.get("null_value") == 1 and lo > 0 and threshold > 0:
        lo, hi, threshold = math.log(lo), math.log(hi), math.log(threshold)
    se, mid = (hi - lo) / (2 * Z95), (lo + hi) / 2
    z = (mid - threshold) / se if rule.get("comparator") == "gt" else (threshold - mid) / se
    return min(1.0, 0.5 * math.erfc(z / math.sqrt(2)) * max(1, int(n_comparisons or 1)))
