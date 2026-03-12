# Milestone 1 — Environment Setup

## Tasks

- [x] Create project directory and Python `venv`
- [x] `pip install python-jobspy crewai python-dotenv`
- [x] Create `.env` with `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`)
- [x] Add `.env` to `.gitignore`
- [x] Verify JobSpy works: run a test scrape and confirm real job rows are returned

## Result — 9/9 PASS (2026-03-11)

```
[PASS] venv is active        (python-jobspy: 1.1.82, crewai: 1.9.3, dotenv: 1.1.1)
[PASS] jobspy importable     (1.1.82)
[PASS] crewai importable     (1.9.3)
[PASS] dotenv importable     (1.1.1)
[PASS] .env exists on disk
[PASS] .gitignore contains .env
[PASS] .env is not git-tracked
[PASS] test scrape returns ≥1 row  (5 rows from Indeed)
[PASS] result has title/company/job_url columns
```

Sample jobs scraped from Indeed:
- Software Engineer, Marketing @ HarbourVest
- Embedded Software Engineer @ Emerson
- R&D Staff Software Engineer @ Broadcom

## Quality Gate

All of the following must be true before handoff:

1. **venv activates cleanly** — `source venv/bin/activate` succeeds with no errors
2. **All packages importable** — `python -c "import jobspy; import crewai; import dotenv"` exits 0
3. **`.env` exists, is not committed** — file present on disk; `.gitignore` contains `.env`; `git status` does not show `.env` as a tracked file
4. **Test scrape returns data** — `scrape_jobs(site_name=["indeed"], search_term="software engineer", results_wanted=5)` returns a DataFrame with ≥1 row containing title, company, and job_url columns
5. **No secrets in code** — `.env` content is never hardcoded in any `.py` file

## Handoff Artifact

A `verify_setup.py` script that prints:
- Package versions for `jobspy`, `crewai`, `dotenv`
- Row count and first 3 rows (title + company + job_url) of the test scrape
- "PASS" or "FAIL" for each quality gate check
