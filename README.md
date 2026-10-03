# Preregistered research lab

I built a lab that takes a list of research questions and tests each one without me in the loop. Language-model workers do the judgement work: they check the literature, write the test, write the analysis and review it. A plain Python kernel runs the code and decides every verdict from a rule written before any data was touched. In six live runs on small bundled datasets it passed one real effect after checking it on held-out data, and it refused five analyses that were weaker or broken. It runs on macOS only.

## Abstract

Automated research has an obvious failure: a model that writes an analysis and then reports on it can, without meaning to, tune the test until it passes. I wanted a lab where that cannot happen, so that a verdict means the same thing whether a person or a model did the work.

Each question moves through fixed stages. A first worker decides whether the question is open and worth testing, using literature searches whose citations the kernel checks against PubMed, Crossref and arXiv. A second worker writes the rule that will decide the answer, with no access to data or a shell, and the kernel freezes it by hash. A third worker writes the analysis, which cannot change the rule. The kernel runs the analysis twice and requires identical results. A separate reviewer, sealed off from the author's notes and transcripts, checks the code. The kernel then applies the rule. A pass is re-run on 30% of the units that the analysis never saw, using a split key only the kernel holds, and must pass again. Finally, every confirmed test in an approved batch goes through a Benjamini-Hochberg correction for testing many questions at once.

Every worker and every analysis runs inside a macOS sandbox. It can write only to its own question folder. It cannot read credentials, the split key, held-out data or other questions. I had an independent reviewer attack the design three times. It found 31 defects, including ways to rename hidden files into view, plant symlinks, fake the held-out run and read another process's environment. Each is fixed and kept as a test.

This does not show that the lab gives right answers on hard questions. The live runs used two small teaching datasets. A script written on purpose to cheat could still nudge its numbers to slip past the identical-results check, and analysis scripts have open network access. The verdicts are only as good as the preregistered rules the workers write.

## Six live runs

All runs used real workers inside the sandbox, on scikit-learn's bundled diabetes data (442 patients) and breast cancer data (569 tumours). Each run exposed something, and I fixed it before the next.

- Run 1 asked whether BMI improves a model of disease progression that already has age and sex. The kernel ruled the analysis invalid. Its shuffled-outcome control came out slightly negative (-0.006, interval -0.012 to -0.001), as out-of-sample scores do when a noise predictor is added. Rules can now declare a small control margin in advance, capped by the kernel.
- Run 2 passed the BMI question. The gain in cross-validated R squared was confirmed on held-out patients (interval 0.19 to 0.47 against a threshold of 0.05).
- Run 3 ran two questions in parallel. One rule named its control with a sentence instead of an identifier, and the other set the minimum sample size for all units when each split sees only part of them. Both rules are now checked and scaled by the kernel.
- In run 4 both questions confirmed on held-out data. The online correction I used then (LORD++) still made both provisional, purely because of the order in which they finished. I replaced it with Benjamini-Hochberg across each approved batch.
- Run 5 passed BMI again (q = 7.8e-6). The breast cancer analysis was ruled invalid because its control had no interval.
- Run 6 added the write-up stage below and passed the house-style checks on the first attempt.

The breast cancer question (does a 30-feature model beat mean radius alone by more than 0.03 AUROC) came out provisional in run 3b, with an interval of 0.023 to 0.067 crossing the threshold. That is a correct inconclusive result, not a failure. Notional usage was about $0.25 to $0.90 per question.

## Three review rounds

An independent reviewer read the code and wrote attacks against it in three rounds, finding 13, 10 and 8 defects. The first round found that the sandbox allowed writes anywhere outside the project, that renaming a hidden file made it readable, that the reviewer could read the author's transcripts, and that the online correction admitted only questions that had already passed. The second found writable shell caches and Claude settings, symlinks that redirected the sandbox rules, and predictable temporary file names. The third found credentials still reachable through the environment and the SSH agent, and that one process could read another's environment, which is why worker stages and analysis runs never overlap. Every attack is in `tests/test_attacks.py`.

## Write-ups

Every finished question gets a repository folder in the style of my published research repos: a lead paragraph, a plain-English abstract, a result section, reproduce steps, `hypothesis.md`, an `experiments.md` ledger, the result records, the frozen analysis code with pinned requirements, `CITATION.cff` and an MIT license. A worker writes the prose; the kernel checks the style and builds the folder from its own records, so the numbers and the verdict cannot drift. `lab publish` creates the GitHub repository, private unless asked otherwise.

## Reproduce

The kernel uses only the Python standard library (3.11 or newer). The tests need pytest and macOS.

```bash
git clone https://github.com/michaeldeangelis/preregistered-research-lab
cd preregistered-research-lab
python3 -m pip install pytest
python3 -m pytest
```

A live run needs the Claude Code CLI signed in. `claude setup-token` writes a long-lived token; save it to `~/.config/rlab/oauth_token` so headless workers never prompt for the keychain. Workers refuse to start unless `~/.claude/settings.json` has a PreToolUse hook whose command contains `spend_guard` (a guard against paid compute); set `RLAB_SPEND_GUARD=off` to run without one. Analyses run in shared virtual environments under `RLAB_ENVS` (`base`, `ml`, `torch`, chosen from each question's needs), for example `python3 -m venv envs/base && envs/base/bin/pip install numpy pandas scipy statsmodels`.

```bash
bin/lab init --root ~/lab/demo --name demo --description "what this project studies"
bin/lab import --root ~/lab/demo questions.md      # one "- question [type:compute]" per line
bin/lab approve --root ~/lab/demo --ids Q-0001     # or --batch <name>
bin/lab run --root ~/lab/demo
bin/lab show --root ~/lab/demo Q-0001
```

## Layout

| Path | Contents |
|---|---|
| `rlab/kernel.py` | The state machine: stages, caps, retries, verdicts, confirmation, batch correction |
| `rlab/sandbox.py` | The macOS sandbox profiles for workers and analysis runs |
| `rlab/rules.py` | Applies a preregistered decision rule to results, failing closed |
| `rlab/fdr.py` | Benjamini-Hochberg and LORD++ |
| `rlab/cite.py` | Resolves cited PMIDs, DOIs and arXiv ids and checks titles |
| `rlab/repo.py` | Builds and publishes the per-question repository |
| `runtime/rlab_split.py` | The unit splitter analysis scripts import |
| `prompts/`, `schemas/` | Instructions and output contracts for each worker stage |
| `docs/design.md` | Each design decision, why, and how to reverse it |
| `tests/` | 89 tests, including every attack from the review rounds |

## Cite

Use the Cite this repository button. It reads `CITATION.cff`.

## License

[MIT](LICENSE). Copyright 2026 Michael DeAngelis. Built by Michael DeAngelis with Claude (Anthropic).
