# Milestone 5 — Output & Scheduling

## Tasks

- [x] Write a `src/reporter.py` that formats the top-N scored jobs into a readable `output/results_YYYY-MM-DD.md`
- [x] Each result entry includes: job title, company, location, score, reasoning, job URL, and skills advice
- [x] Create a single entry point `run.sh` (or `src/main.py`) that chains scrape → score → report in one command
- [x] Optionally add a shell alias or cron entry for daily runs — see scheduling note below
- [x] Add `output/` to `.gitignore` (results contain scraped third-party data)

## Quality Gate

All of the following must be true before handoff:

1. **Single command works** — `./run.sh` (or `python src/main.py`) completes the full pipeline: scrape → score → report, without manual intervention
2. **Output file is readable** — `output/results_YYYY-MM-DD.md` renders correctly in a Markdown viewer; includes all required fields per job entry
3. **Top-N is configurable** — the number of results surfaced is controlled by a value in `config.yaml`, not hardcoded
4. **`output/` is gitignored** — `git status` does not show any files under `output/` as tracked
5. **Full run is repeatable** — running the command twice on the same day produces a valid (possibly un-updated) output file without errors

## Gate Results — 2026-03-11

| Gate | Result |
|------|--------|
| `./run.sh` completes full pipeline without intervention | PASS |
| `output/results_YYYY-MM-DD.md` renders with all required fields | PASS |
| `top_n` controlled by `config.yaml criteria.top_n` | PASS |
| `output/` in `.gitignore` | PASS |
| Second run same day produces valid output without errors | PASS |

**5/5 PASS**

## Handoff Artifact

- `run.sh` — thin venv-activating wrapper; `./run.sh --search "qa lead"` etc.
- `src/main.py` — full orchestrator (scrape → score → report)
- `src/reporter.py` — formats scored JSON to ranked markdown
- `output/results_2026-03-11.md` — sample report (gitignored, view locally)

## Scheduling a Daily Run

To run every morning at 7am, add one line to your crontab (`crontab -e`):

```
0 7 * * * /home/nick/code/staraligner/projects/ai-job-search/run.sh >> /home/nick/code/staraligner/projects/ai-job-search/output/cron.log 2>&1
```

Or create a shell alias for manual use:

```bash
# In ~/.bashrc or ~/.zshrc
alias jobsearch="/home/nick/code/staraligner/projects/ai-job-search/run.sh"
```
