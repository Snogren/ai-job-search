---
name: AI Job Search Agent
status: active
---

## Problem

Job searching is tedious and time-consuming. Discovering relevant listings across multiple boards, evaluating fit, and tracking applications is manual work that an AI agent is well-suited to automate. The user wants a local Python tool that uses an LLM agent SDK to research jobs on their behalf, with credentials and API keys managed via `.env`.

## Goal

A working personal job search agent that: (1) pulls fresh job listings from major boards, (2) scores them against the user's profile or criteria, and (3) surfaces the best matches — all triggered from the command line with no manual browsing. Credentials and API keys live in `.env` and never touch version control.

## Approach

**JobSpy + CrewAI** (Option 1). JobSpy (`python-jobspy`) scrapes LinkedIn, Indeed, Glassdoor, ZipRecruiter, and Google Jobs concurrently into a Pandas DataFrame — no auth needed for basic searches. A CrewAI pipeline processes the results through specialized agents: a Job Scorer that matches listings to user criteria, a Skills Gap Advisor that identifies what skills to highlight, and optionally an Interview Coach. The LLM API key (OpenAI, Anthropic, etc.) lives in `.env`.

Chosen over Adzuna API (less board coverage) and browser automation (TOS/ban risk). The JobSpy MCP Server is a useful fallback if the user wants zero-code research inside Claude Desktop.

LLM provider is **OpenRouter**. CrewAI uses LiteLLM internally, which routes to OpenRouter via the `openrouter/` model prefix (e.g. `openrouter/anthropic/claude-3.5-sonnet`). Set `OPENROUTER_API_KEY` and `CREWAI_MODEL` in `.env`.

## Plan

### Milestone 1 — Environment Setup ✓
- [x] Create project directory and `venv`
- [x] `pip install python-jobspy crewai python-dotenv`
- [x] Create `.env` with `OPENAI_API_KEY` (or Anthropic key)
- [x] Add `.env` to `.gitignore`
- [x] Verify JobSpy works: run a test scrape (`scrape_jobs(site_name=["indeed"], search_term="software engineer", results_wanted=10)`)

### Milestone 2 — Job Scraper ✓
- [x] Write `scraper.py`: accepts `search_term`, `location`, `hours_old`, `site_name` as args
- [x] Return results as a filtered DataFrame (no duplicates by company+title)
- [x] Export to CSV for inspection

### Milestone 3 — CrewAI Pipeline ✓
- [x] Define agent: **Job Scorer** — reads job description + user criteria from `.env` or a `config.yaml`; rates relevance 1-10 with reasoning
- [x] Define agent: **Skills Advisor** — identifies skills the user should highlight or acquire per top listings
- [x] Wire agents into a sequential CrewAI Crew
- [x] Pipe JobSpy DataFrame output as task context into CrewAI

### Milestone 4 — User Profile Config ✓
- [x] Create `config.yaml`: desired roles, required skills, preferred locations, salary range, remote preference
- [x] Load config at runtime; pass as context to agents
- [x] Store only secrets in `.env` (API keys); non-secret config in `config.yaml`

### Milestone 5 — Output & Scheduling ✓
- [x] Write top-N results (scored + annotated) to `output/results_YYYY-MM-DD.md`
- [x] Optionally add a cron job or shell alias (`jobsearch`) to run daily

### Milestone 6 — DB + Viewer ✓
- [x] SQLite schema: `jobs`, `criteria`, `job_scores`, `job_actions`, `scrape_runs`, `job_insights`
- [x] `SQLiteWriteQueue` single-writer thread for safe concurrent scraper writes
- [x] Multiple criteria sets: score the same job against several criteria independently
- [x] `canonical_key` dedup: cross-site alias grouping; "also on X" badges in viewer
- [x] Job dismissal: hide reviewed-and-rejected jobs without deleting them
- [x] FastAPI web viewer (`viewer.py`): filter by site / location / job type / score / criteria

### Milestone 7 — UI Control Plane (planning)

Full details: `milestones/m7-ui-job-scoring-manager.md`

#### M7.1 — Criteria Manager backend
- [ ] API endpoints in `viewer.py`: `GET/POST /criteria`, `GET/PUT /criteria/{id}`, `POST /criteria/{id}/set-default`, `DELETE /criteria/{id}`
- [ ] DB helpers: `list_criteria`, `get_criteria_by_id`, `create_criteria`, `update_criteria`, `set_default_criteria`, `disable_criteria`
- [ ] Weight normalization on save; recompute `criteria_hash`

#### M7.2 — Criteria Manager UI
- [ ] `templates/criteria.html` — list page (create/edit/delete/set-default)
- [ ] `templates/criteria_form.html` — form with live weight normalization preview
- [ ] Nav link in `jobs.html` header

#### M7.3 — Background pipeline runner
- [ ] `RunManager` (in `src/runner.py`): starts `python src/main.py` as subprocess, captures stdout/stderr, exposes log buffer + status
- [ ] `POST /search/start` — validates form params, invokes RunManager, redirects to `/run/{run_id}`
- [ ] `DELETE /run/{run_id}` — kills subprocess
- [ ] In-memory `run_registry: dict[int, RunManager]` keyed by `scrape_run_id`

#### M7.4 — Search Launch UI
- [ ] `templates/search.html` — launch form (criteria, boards, keywords, location, counts, mode)
- [ ] `GET /search` route; pre-populate defaults from `config.yaml`

#### M7.5 — Live Log Streaming
- [ ] `GET /run/{run_id}/log` SSE endpoint — streams RunManager log buffer line-by-line
- [ ] `templates/run.html` — log window (auto-scroll, status badge, stop button, "View Results" link)
- [ ] `GET /runs` + `templates/runs.html` — run history table

#### M7.6 — Real-time Job Board Updates
- [ ] `GET /jobs/live` SSE endpoint — polls DB for new `job_scores` and pushes as JSON events
- [ ] `GET /jobs/progress` endpoint — `{scored, total, running}`
- [ ] Update `jobs.html`: EventSource subscription, animated card append, progress bar, toast

## Notes

2026-03-11 — Research complete. Prior art found:
- `python-jobspy` (speedyapply/JobSpy): best scraping library, supports 8 boards, no auth required for basic use. Indeed has no rate limits; LinkedIn rate-limits around page 10 (use proxies for volume).
- `job-search-agent` (byrencheema/job-search-agent): CrewAI + Claude, Oct 2025 UCI workshop. Uses Adzuna API. Good reference implementation.
- `crewai-job` (drukpa1455/crewai-job): CrewAI + LangChain for CV/cover letter tailoring. Complementary to this project.
- `JobSpy MCP Server` (lobehub.com/mcp/yourorg-jobspy-mcp-server): Ready-made MCP server for Claude Desktop. Zero-code alternative if fully custom pipeline is not needed.
- `Auto_job_applier_linkedIn` (GodsScion): Selenium bot for LinkedIn Easy Apply. High TOS risk; not recommended for this project.
- Adzuna API (developer.adzuna.com): Free, legitimate API. Good fallback if JobSpy scraping becomes unreliable.

TOS note: LinkedIn, Indeed, and Glassdoor prohibit scraping in their ToS. hiQ v. LinkedIn (US) established that accessing public data is not illegal per se, but platforms can ban accounts/IPs. For personal research use (not commercial), risk is low but real. Use proxies if running at volume, or switch to Adzuna API for a clean alternative.

Security note: `.env` stores API keys only. Never commit to version control. Use `python-dotenv` and `load_dotenv()`. For LinkedIn session-based access, store `LINKEDIN_EMAIL` and `LINKEDIN_PASSWORD` in `.env` — but prefer cookie-based auth if available, as it's less fragile.
