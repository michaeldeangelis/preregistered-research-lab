"""Kernel-rendered outputs: prereg.md, card.md, registry claims, the lab ledger and the digest.

Cards are built only from recorded fields; worker prose appears only in the
labelled interpretation/summary lines.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

from . import questions as Q
from .project import Project
from .util import append_jsonl, atomic_write_text, now_iso, read_json

CLOSED = {"duplicate", "parked"}


def default_registry() -> Path:
    home = Path(os.environ.get("RLAB_HOME", Path.home() / ".dev" / "research-lab"))
    return home / "registry" / "claims.jsonl"


def _fmt_effect(effect) -> str:
    if not isinstance(effect, dict):
        return "-"
    parts = [f"{effect.get('point'):.4g}" if isinstance(effect.get("point"), (int, float)) else "?"]
    if isinstance(effect.get("lo"), (int, float)) and isinstance(effect.get("hi"), (int, float)):
        parts.append(f"[{effect['lo']:.4g}, {effect['hi']:.4g}]")
    if effect.get("n") is not None:
        parts.append(f"n={effect['n']}")
    return " ".join(parts)


def write_prereg_md(qd: Path, q: dict, prereg: dict) -> None:
    rule = prereg["decision_rule"]
    atomic_write_text(qd / "prereg.md", "\n".join([
        f"# Preregistration {q['id']}", "", f"Question: {q['text']}", f"Frozen: {now_iso()}", "",
        f"Hypothesis: {prereg['hypothesis']}", f"Family: {prereg['family']} (alpha {prereg['alpha']})",
        f"Decision rule: {rule['metric']} {'>' if rule['comparator'] == 'gt' else '<'} {rule['threshold']}"
        f"{' with the 95% CI excluding it' if rule['ci_must_exclude'] else ''}; min_n {rule['min_n']}",
        "Baselines: " + ", ".join(prereg["baselines"]),
        "Negative controls (must read null): " + ", ".join(prereg["negative_controls"]),
        "Data: " + ", ".join(prereg["data"]), f"Seeds: {prereg['seeds']}", "", f"Notes: {prereg['notes']}", "",
    ]))


def write_card(project: Project, q: dict) -> None:
    qd = Q.qdir(project, q["id"])
    lines = [f"# {q['id']}: {q['text']}", "",
             f"**Verdict: {q.get('verdict')}**" + (f" (evidence {q['evidence_grade']})" if q.get("evidence_grade") else ""),
             f"Reason (kernel): {q.get('verdict_reason', '')}", "",
             f"Type: {q.get('type')} · family: {q.get('family', '-')} · test family: {q.get('test_family', '-')}",
             f"Spend: ${q.get('spend_usd', 0):.2f}"]
    if q.get("metric"):
        lines.append(f"Effect ({q['metric']}): {_fmt_effect(q.get('effect'))}")
    if q.get("opposite_direction"):
        lines.append("Note: the effect is significant in the OPPOSITE direction to the hypothesis.")
    if (q.get("n_comparisons") or 1) > 1:
        lines.append(f"Comparisons accounted for (Bonferroni): {q['n_comparisons']}")
    unverified = [s.get("id") for s in (q.get("sources") or []) if s.get("verified") is None and "verified" in s]
    if unverified:
        lines.append("Unverified citations (resolver unreachable): " + ", ".join(map(str, unverified)))
    if q.get("rejected_sources"):
        lines.append("Rejected citations: " + "; ".join(f"{r.get('id')} ({r.get('reject_reason')})" for r in q["rejected_sources"]))
    if q.get("reproduced") is not None:
        lines.append(f"Rerun reproduced: {q['reproduced']}")
    if "confirmed" in q:
        lines.append(f"Confirmed on held-out units: {q['confirmed']} (holdout verdict {q.get('holdout_verdict')}, "
                     f"effect {_fmt_effect(q.get('holdout_effect'))})")
    if q.get("fdr_method") == "BH":
        lines.append(f"Multiple testing: Benjamini-Hochberg across {q['fdr_m']} test(s) of batch "
                     f"{q.get('import_batch')}, p={q['fdr_p']:.3g}, q={q['fdr_q']:.3g}")
    if q.get("fdr_t"):
        lines.append(f"Online FDR (LORD++): test {q['fdr_t']}, p={q['fdr_p']:.3g}, level {q['fdr_alpha_t']:.3g}")
    if q.get("review_severity"):
        review = read_json(qd / "review" / "review.json", {})
        lines += ["", f"Review ({q['review_severity']}):"] + [f"- {i}" for i in review.get("issues", [])]
    if q.get("sources"):
        lines += ["", "Sources:"] + [f"- {s.get('id')}: {s.get('title', '')}" for s in q["sources"]]
    if q.get("links", {}).get("duplicate_of"):
        lines.append(f"Duplicate of: {q['links']['duplicate_of']}")
    if q.get("repo"):
        lines += ["", f"GitHub-ready repo: {q['repo']}/ (README with plain-English abstract; publish with "
                      f"`lab publish --root <project> {q['id']}`)"]
    elif q.get("writeup_error"):
        lines += ["", f"Write-up failed: {q['writeup_error'][:200]}"]
    lines += ["", "Files: " + ", ".join(p for p in ("triage.json", "lit/evidence.json", "prereg.md", "code/analysis.py",
                                                    "kernel/results.json", "review/review.json") if (qd / p).exists()), ""]
    atomic_write_text(qd / "card.md", "\n".join(lines))


def register(project: Project, q: dict, registry: Path) -> None:
    if q.get("verdict") in CLOSED:
        return
    append_jsonl(registry, {
        "claim_id": f"{project.name}:{q['id']}", "project": project.name, "qid": q["id"],
        "statement": q["text"], "verdict": q.get("verdict"), "reason": q.get("verdict_reason"),
        "metric": q.get("metric"), "effect": q.get("effect"), "evidence_grade": q.get("evidence_grade"),
        "family": q.get("family"), "prereg_sha": q.get("prereg_sha"), "results_sha": q.get("results_sha"),
        "confirmed": q.get("confirmed"), "holdout_effect": q.get("holdout_effect"),
        "fdr": {k: q[k] for k in ("fdr_method", "fdr_t", "fdr_p", "fdr_q", "fdr_m", "fdr_alpha_t") if k in q} or None,
        "opposite_direction": q.get("opposite_direction"),
        "at": now_iso(), "status": "active", "supersedes": None, "retracted_by": None,
    })


def append_ledger(project: Project, q: dict) -> None:
    """Append-only lab ledger inside the qbank (not the project's own experiments.md)."""
    path = project.qbank / "ledger.md"
    entry = [f"\n## {now_iso()} {q['id']} {q.get('verdict')}", f"Question: {q['text']}",
             f"Reason: {q.get('verdict_reason', '')}"]
    if q.get("metric"):
        entry.append(f"Effect ({q['metric']}): {_fmt_effect(q.get('effect'))}")
    with path.open("a") as f:
        f.write("\n".join(entry) + "\n")


def write_digest(project: Project) -> None:
    qs = Q.all_questions(project)
    states = Counter(q["state"] for q in qs)
    verdicts = Counter(q.get("verdict") for q in qs if q.get("verdict"))
    spend = sum(q.get("spend_usd", 0.0) for q in qs)
    lines = [f"# {project.name} lab digest", f"Updated {now_iso()} · {len(qs)} questions · spend ${spend:.2f}", "",
             "## Funnel", *[f"- {s}: {n}" for s, n in sorted(states.items())], "",
             "## Verdicts", *([f"- {v}: {n}" for v, n in sorted(verdicts.items())] or ["- none yet"]), ""]
    blocked = [q for q in qs if q["state"] == "blocked"]
    if blocked:
        lines += ["## Blocked (needs attention)", *[f"- {q['id']}: {q.get('blocked_reason')}" for q in blocked], ""]
    recent = sorted((q for q in qs if q["state"] == "reported"), key=lambda q: q.get("updated_at", ""), reverse=True)[:15]
    if recent:
        lines += ["## Latest results",
                  *[f"- {q['id']} **{q.get('verdict')}**: {q['text'][:120]}" for q in recent], ""]
    atomic_write_text(project.qbank / "digest.md", "\n".join(lines))
