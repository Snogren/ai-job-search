# Milestone 7 — UI Job Scoring Manager

**Status:** Planning  
**Depends on:** M1–M6 complete  
**Goal:** Replace the config.yaml + CLI workflow with a browser-based control plane:
criteria management, job search launch, live log streaming, and real-time board updates as jobs are scored.

---

## Context

The pipeline currently runs from the command line. Config lives in `config.yaml`. There is no way to manage criteria, launch a search, or watch progress without touching files and a terminal. The web UI (`viewer.py`) already exists for reviewing scored jobs — this milestone extends it into a full control plane.

---

## Architecture Notes

### How the pipeline currently works

```
main.py --mode pipeline
  └─ ingest_parallel_keywords()   ← ThreadPoolExecutor, one thread per (keyword × location)
  └─ run_analysis()               ← ThreadPoolExecutor, concurrent LLM calls (parallel_workers)
  └─ generate_report_from_db()    ← sequential, writes .md file
```

- Scraping and scoring are **sequential phases** today. All scraping completes before scoring starts.
- Within each phase, parallelism already exists (thread pool).
- Phases **can** be overlapped in a future optimization: score batch 1 while scraper fetches batch 2. Not in scope for M7.

### Real-time updates — why they work even with sequential phases

Scoring writes each job result to the DB via `SQLiteWriteQueue` as it finishes (not all at once at the end). An SSE endpoint can poll for `job_scores WHERE scored_at > ?` on a 1-second interval and push delta rows to connected clients. The job board page subscribes and renders new cards as they arrive — the user sees results flowing in during the scoring phase.

### Background task strategy

Run the pipeline as a **subprocess** (`asyncio.create_subprocess_exec` calling `python src/main.py`). This:
- Keeps the pipeline code unchanged
- Lets stdout/stderr be captured line-by-line and streamed to the browser via SSE
- Isolates the long-running work from the FastAPI event loop
- Lets us kill the process cleanly

Alternative (in-process thread) was considered but rejected: `run_analysis()` uses `ThreadPoolExecutor` with blocking `litellm.completion()` calls. Running it directly in FastAPI's async loop requires `asyncio.to_thread()` wrappers at every blocking point and makes log capture messy.

### SSE approach

Two SSE feeds:

| Endpoint | Purpose |
|---|---|
| `GET /run/{run_id}/log` | Stream subprocess stdout/stderr lines as they appear |
| `GET /jobs/live` | Push new `job_scores` rows as they land in the DB |

Both use `text/event-stream` (native Starlette `StreamingResponse`). The `sse-starlette` package handles keepalive and disconnect cleanly.

---

## Features

### Feature 1 — Criteria Manager (`/criteria`)

Full CRUD for scoring criteria sets, replacing manual `config.yaml` edits.

**Pages:**
- `GET /criteria` — list all criteria sets, which is default, created date
- `GET /criteria/new` — form to create a new criteria set
- `GET /criteria/{id}/edit` — form to edit an existing criteria set
- `POST /criteria` — create
- `PUT /criteria/{id}` — update
- `DELETE /criteria/{id}` — soft-disable (set `is_enabled = 0`)
- `POST /criteria/{id}/set-default` — make this the active default

**Form fields:**
- `name` — short slug (e.g. `senior-qa-remote-2026`)
- `description` — free text, used as context for the LLM scorer
- `qualifiers` — multi-line textarea, one qualifier per line  
  _(ALL must be true for a job to be scored)_
- `disqualifiers` — multi-line textarea, one disqualifier per line  
  _(ANY match skips the job)_
- `weights` — three numeric inputs: Relevance / Duties / Income  
  Auto-normalize to sum to 1.0 on save. Show live preview of normalized values.

**Data mapping to DB `criteria` table:**
- `qualifiers` and `disqualifiers` stored as JSON arrays
- `weights` stored as JSON object
- `criteria_hash` computed from the above on save — unchanged criteria reuse the same row

### Feature 2 — Search Launch UI (`/search`)

A form that configures and fires a pipeline run from the browser.

**Form fields:**
- `criteria_id` — dropdown: which criteria set to score against
- `sites` — checkboxes: `indeed`, `linkedin`, `glassdoor`, `zip_recruiter`, `google`
- `keywords` — textarea, one keyword per line (pre-populated from config.yaml defaults)
- `location` — text input (default: `remote`)
- `results_per_site` — number input (default: 25)
- `hours_old` — number input (default: 72)
- `mode` — radio: `scrape only` / `analyze only` / `full pipeline`

**Behavior:**
- `POST /search/start` → validates form, starts subprocess, creates a `scrape_runs` row, redirects to `/run/{run_id}`

### Feature 3 — Live Log Window (`/run/{run_id}`)

A real-time log viewer for a running or completed pipeline run.

**Page elements:**
- Status badge: `running` / `done` / `error` (updates live)
- Auto-scrolling log window: monospace, shows each stdout/stderr line as it arrives
- Progress summary (scraped / scored / qualified) — updated from DB every 5 seconds
- "Stop run" button → sends `DELETE /run/{run_id}` → kills subprocess
- When run completes, "View Results" button → navigates to `/` with `criteria_id` pre-filtered

**SSE feed:** `GET /run/{run_id}/log`
- Each event: `data: <log line>\n\n`
- On completion: `event: done\ndata: exit_code\n\n`
- On error: `event: error\ndata: <message>\n\n`
- Keepalive comment every 15 seconds: `: keepalive\n\n`

**Run history:** `GET /runs` — table of past runs: date, keywords, sites, status, jobs scraped/scored

### Feature 4 — Real-time Job Board Updates

The existing job board (`/`) subscribes to new scored jobs as they land.

**SSE feed:** `GET /jobs/live?since={iso_timestamp}&criteria_id={id}`
- Server polls `job_scores WHERE scored_at > since AND criteria_id = ?` every 1 second
- Pushes delta rows as JSON events
- Client appends new job cards to the board (or re-sorts if sorting by score)
- Client updates a progress bar: `scored / total_unscored` (fetched from `GET /jobs/progress?criteria_id=N`)

**UX notes:**
- Progress bar appears when a run is in progress (detected via `scrape_runs WHERE status = 'running'`)
- Cards animate in with a CSS fade when appended
- A "N new jobs scored" toast appears and clears after 3 seconds
- After run completes, SSE closes; board shows final count

---

## Implementation Plan

### M7.1 — Criteria Manager backend
- [ ] Add API endpoints to `viewer.py`: `GET/POST /criteria`, `GET/PUT /criteria/{id}`, `POST /criteria/{id}/set-default`, `DELETE /criteria/{id}`
- [ ] Add `list_criteria`, `get_criteria_by_id`, `create_criteria`, `update_criteria`, `set_default_criteria`, `disable_criteria` to `database.py`
- [ ] Wire weights JSON normalization on save; recompute `criteria_hash`

### M7.2 — Criteria Manager UI
- [ ] Create `templates/criteria.html` — list page with create/edit/delete/set-default actions
- [ ] Create `templates/criteria_form.html` — create/edit form with live weight normalization
- [ ] Add nav link in `jobs.html` header

### M7.3 — Background pipeline runner
- [ ] Add `RunManager` class to `viewer.py` (or extract to `src/runner.py`): starts subprocess, captures stdout/stderr line-by-line, exposes log buffer + status
- [ ] `POST /search/start` endpoint: validates params, constructs `python src/main.py` invocation, hands off to RunManager
- [ ] `DELETE /run/{run_id}` endpoint: kills running subprocess
- [ ] In-memory `run_registry: dict[int, RunManager]` keyed by `scrape_run_id`

### M7.4 — Search Launch UI
- [ ] Create `templates/search.html` — launch form
- [ ] Add `GET /search` route to `viewer.py`
- [ ] Pre-populate form defaults from config.yaml

### M7.5 — Live Log Streaming
- [ ] `GET /run/{run_id}/log` SSE endpoint — streams from RunManager log buffer
- [ ] Create `templates/run.html` — log window page with EventSource JS
- [ ] Add `GET /runs` route + `templates/runs.html` — run history table

### M7.6 — Real-time Job Board Updates
- [ ] `GET /jobs/live` SSE endpoint — polls DB for new scores and pushes as JSON events
- [ ] `GET /jobs/progress` endpoint — returns `{scored: N, total: N, running: bool}`
- [ ] Update `jobs.html`: add EventSource subscription, card append logic, progress bar, toast

---

## Out of Scope

- Parallel scrape + score phases (stream-process pipeline) — future optimization
- User accounts / auth — single-user local tool
- Email/notification on run completion — can be CLI `./run.sh` wrapper concern
- Cover letter / skills gap generator UI — M8+ territory

---

## Open Questions

1. **`sse-starlette` vs raw `StreamingResponse`?**  
   `sse-starlette` handles disconnect detection (`asyncio.CancelledError`) and keepalives cleanly. Prefer it to avoid rolling that logic by hand.

2. **Config.yaml sync when criteria is edited via UI?**  
   The DB is the source of truth for active criteria. `config.yaml` stays as the seed/initialization source. No need to write back to disk unless explicitly asked — the `criteria` table takes over.

3. **`scrape_run_id` as the run handle?**  
   Yes — `scrape_runs` already has a `run_id` PK and `status` column. The RunManager maps to it. This avoids a separate run tracking table.

4. **Subprocess argument construction — injection risk?**  
   CLI args are built from validated form fields (enum-restricted site names, numeric inputs, keywords sanitized as quoted strings). Never shell=True. Use `args: list[str]` form of `asyncio.create_subprocess_exec`.

---

## Key Dependencies

| Package | Purpose |
|---|---|
| `sse-starlette` | Clean SSE with keepalive + disconnect handling |
| `asyncio` (stdlib) | `create_subprocess_exec` for background pipeline |
| No new scraping deps | Reuses existing `python-jobspy`, `crewai`, `litellm` |
