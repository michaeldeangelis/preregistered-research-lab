# Design decisions

Each decision, why it was made, and how to reverse it. Numbers refer to the order they were made.

## Kernel

1. Standard library only (json, sqlite3, tomllib, subprocess) and a small in-house JSON-schema subset validator. No install step. Reverse: add jsonschema if contracts grow.
2. Files are the source of truth, one folder per question, written atomically. A killed run resumes from the last finished stage.
3. Workers are `claude -p` calls with `--output-format json --json-schema --max-budget-usd --permission-mode dontAsk`, stdin closed. `--max-budget-usd` can overshoot by one turn, so the kernel also sums reported cost per question and refuses a stage that would exceed the per-question cap.
4. The kernel, never a worker, runs analysis code and decides the verdict from the preregistered rule. Fail closed: anything missing or malformed gives `provisional` or `invalid`, never `pass`. Reverse: never.
5. Usage on a Claude subscription is notional; dollar caps act as usage guards. Paid compute is out of scope and blocked by a spend guard hook.
6. Question banks are imported in batches and approved by a person, per batch or per question.

## Preregistration

7. The protocol is two stages. The rule stage writes the hypothesis, the exact analysis plan and the decision rule with web access only: no shell, no local files. The kernel hashes it. The script stage implements it and blocks the question if it changes the rule. Before this split, "rule before results" rested on the worker's honesty.
8. Control and baseline names must be short identifiers. `min_n` is set for all units and scaled by the kernel to each split. A negative control may sit within a preregistered margin of null, capped at a quarter of the effect scale, because out-of-sample scores drift slightly below null under a shuffled outcome. A control reported without an interval is invalid.
9. Within one question, Bonferroni over the larger of the declared comparisons and the distinct effects reported. Kills significant in the opposite direction are flagged.
10. Cited identifiers must resolve (PubMed, PMC, Crossref, doi.org, arXiv) and the title must match; unreachable resolvers do not count as evidence.

## Verification

11. The kernel reruns every analysis and requires identical endpoint, effects, baselines, controls, seeds and split. A pass that was not rerun is provisional.
12. Review is independent and sealed: the reviewer works in a copy of the rule (without hypothesis or notes), the code and the results, and the sandbox denies it the rest of the question folder, other questions and the author's transcripts.
13. Held-out confirmation: analysis scripts filter units through `rlab_split` with a per-question salt derived from a kernel-only master key. Discovery runs on about 70%; a discovery pass is rerun on the other 30% with the same rule. The kernel checks that the two runs saw the same units, that the splits are complementary, and that the held-out numbers are not identical to discovery (a decoy split). An analysis that does not split cannot pass. Reverse: `[checks] require_confirmation = false`.
14. Multiple testing across questions: Benjamini-Hochberg across every confirmatory test of an approved batch, once nothing in the batch is still running. Each verdict is still made once. Online LORD++ is available (`[checks] fdr = "lord"`) for projects that stream questions without batches. LORD++ was the first default and was replaced after a live run in which two confirmed effects were made provisional purely by the order they finished.

## Confinement

15. Every worker and analysis run executes under macOS `sandbox-exec`. Writes are default-deny: only the question's working folders and, for Claude workers, their own per-question Claude config folder. Every read-denied path is also write-denied, because a rename is a write. Credentials (cloud, ssh, gh, keychains, Claude config and transcripts, shell rc and history files) are unreadable. Deny rules cover both the named path and its resolved target. Reverse: Linux would need bubblewrap or landlock; without the sandbox the kernel refuses to run unless `RLAB_ALLOW_UNSANDBOXED=1`.
16. Environment is an allowlist: no API keys, tokens or SSH agent socket reach a worker or a script; outbound unix sockets are denied except DNS.
17. Workers run with their own `CLAUDE_CONFIG_DIR` per question and role, carrying only the user's spend guard hook. The real `~/.claude` is never writable.
18. The kernel refuses symlinks where it owns folders, rejects symlinks and bytecode in frozen code, copies results it relies on into a folder analyses cannot write, hash-checks them before every decision, and writes its own files through random temporary names.
19. A sandboxed process can read another process's environment, and live workers carry their OAuth token, so worker stages and analysis runs never overlap within a tick.
20. General web search is off by default; literature search goes through PubMed, Europe PMC, OpenAlex, arXiv, Crossref and Semantic Scholar by per-domain fetch. Reverse: `[web] search = true`.

## Throughput

21. Up to three worker stages and two analysis runs at a time; kernel stages (decide, gate, batch closure, report) run serially in one order. A crash in one question blocks that question, not the run.

## Write-ups

22. After the verdict, a read-only worker writes the prose for a public repository (title, lead, plain-English abstract, result section, description, topics). The kernel enforces the house style and assembles the folder from its own records in a fixed section order. A failed write-up never blocks the verdict. Publishing is a separate command, private by default.

## Agent teams

23. Three tiers of agents above the kernel: a lab director on a loop across projects, a project lead per project with an analyst, and a team for every worker stage (scout and skeptic for triage, contrarian for literature, statistician for the rule, tester for the script, code and statistics reviewers, fact-checker for the write-up). Specialists are `claude -p --agents` subagents inside their lead's sandbox; a missing finding fails the attempt.
24. Leads act only by returning actions. The kernel applies them within the project's `[lead]` charter: approval caps and work-in-flight caps, no question whose needs wait on a project setting, one retry per question and never for an integrity block, private publishing only if allowed. The owner keeps paid compute, credentials and data accounts, project settings and charters, and anything public.

## Known limits

- Analysis scripts have open network access.
- A script written to cheat can nudge its numbers to evade the identical-results check; the reviewer is told to look for this.
- The worker's own OAuth token is in its environment.
- macOS only.
