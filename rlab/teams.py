"""Stage teams: each worker stage is a lead with specialist subagents (claude -p --agents).

The stage worker is the team lead. It must consult each specialist through the Agent tool and record what each
found under `team` in its output; the kernel checks every required role is there. Specialists run inside the
same sandbox and permission rules as the lead (their tools are a subset of the lead's).
"""

from __future__ import annotations

TEAMS: dict[str, dict[str, dict]] = {
    "lead": {
        "analyst": {"description": "Ranks waiting questions and diagnoses blocked ones for the project lead",
                    "prompt": "You are the lead's analyst. From packet.json, rank the waiting questions by value, "
                              "feasibility with open data and how clearly they fill a gap, and for each blocked "
                              "question say whether it is transient, a worker mistake, an integrity block or a "
                              "needs-the-owner problem.",
                    "tools": ["Read", "Grep", "Glob"]},
    },
    "triage": {
        "scout": {"description": "Searches the literature for the closest prior work on the question",
                  "prompt": "You are the literature scout. Run 3-6 targeted searches through the scholarly APIs you are "
                            "given and return the closest hits with exact ids and titles, newest first, and one line on "
                            "what each did and did not do.",
                  "tools": ["WebFetch", "Read"]},
        "skeptic": {"description": "Argues the question is already answered, infeasible or not worth testing",
                    "prompt": "You are the skeptic. Make the strongest case that this question is already answered, "
                              "cannot be tested with available data, or would not change any decision. Cite ids.",
                    "tools": ["WebFetch", "Read"]},
    },
    "lit": {
        "contrarian": {"description": "Finds evidence against the emerging answer",
                       "prompt": "You are the contrarian searcher. Find the strongest published evidence AGAINST the "
                                 "answer the lead is converging on. Exact ids and titles only.",
                       "tools": ["WebFetch", "Read"]},
    },
    "protocol": {
        "statistician": {"description": "Attacks the draft rule: power, controls, leakage, multiplicity",
                         "prompt": "You are the statistician. Attack the draft preregistration: is n enough for the "
                                   "threshold, do the negative controls replay the whole pipeline, can outcome "
                                   "information leak, is there more than one comparison, are covariates measured before "
                                   "the exposure? Return concrete fixes.",
                         "tools": ["WebFetch"]},
    },
    "script": {
        "tester": {"description": "Runs the script on a small slice and checks it implements the frozen rule",
                   "prompt": "You are the tester. Run the analysis on a small slice, check every key in results matches "
                             "the frozen prereg.json names, that units go through rlab_split, that controls carry "
                             "intervals, and that nothing outside the analysis_plan is computed. Report failures exactly.",
                   "tools": ["Read", "Grep", "Glob", "Bash"]},
    },
    "review": {
        "code_reviewer": {"description": "Checks the code for leakage, bugs and deviations from the rule",
                          "prompt": "You are the code reviewer. Read the code against rule.json. Look for leakage, wrong "
                                    "metric, fake or partial controls, selection among analyses, unsplit data. Re-run it "
                                    "if useful. List issues with file and line.",
                          "tools": ["Read", "Grep", "Glob", "Bash"]},
        "stats_reviewer": {"description": "Checks the statistics: intervals, n, controls, multiplicity",
                           "prompt": "You are the statistics reviewer. Check that intervals are computed as claimed, n "
                                     "matches the data, controls are real and null-centred, and multiplicity is "
                                     "accounted for. List issues.",
                           "tools": ["Read", "Grep", "Glob"]},
    },
    "writeup": {
        "fact_checker": {"description": "Checks every number and claim in the draft prose against the records",
                         "prompt": "You are the fact-checker. Check every number, interval, n and verdict word in the "
                                   "draft against question.json and kernel/*.json. List each mismatch; nothing else.",
                         "tools": ["Read", "Grep"]},
    },
}


def agents_for(stage: str, model: str = "inherit") -> dict:
    return {name: {**spec, "model": model} for name, spec in TEAMS.get(stage, {}).items()}


def instructions(stage: str) -> str:
    roles = TEAMS.get(stage)
    if not roles:
        return ""
    names = ", ".join(roles)
    return (f"\nYou lead a team for this stage: {names}. Consult each of them through the Agent tool before you answer, "
            "act on what they find, and record one entry per specialist under `team` (role and what it found).")


def missing_roles(stage: str, out: dict) -> list[str]:
    got = {str(t.get("role", "")).strip() for t in out.get("team") or [] if isinstance(t, dict)}
    return [r for r in TEAMS.get(stage, {}) if r not in got]


TEAM_SCHEMA = {"type": "array", "items": {"type": "object", "required": ["role", "finding"],
                                          "properties": {"role": {"type": "string"}, "finding": {"type": "string"}}}}
