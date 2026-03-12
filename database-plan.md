# Database Architecture Plan: AI Job Search Application

## Executive Summary

The current system crawls job boards on every run, processes serially, and has no persistent storage. This plan introduces a **SQLite-backed architecture** with concurrent ingestion and independent analysis pipelines. Jobs flow through: **(Scrape → Upsert to DB) ∥ (Analyze DB → Write Scores) → Report from DB**.

---

## 1. Technology Choice: SQLite

### Decision: **SQLite with WAL Mode**

**Why SQLite (not PostgreSQL or DuckDB):**

| Criterion                    | SQLite        | PostgreSQL | DuckDB |
|------------------------------|---------------|------------|--------|
| **Setup Complexity**          | 0 (file-based) | Medium (server) | Low |
| **Single Developer Tool**     | ✅ Perfect     | Overkill   | ❌ Analytics DB |
| **Concurrent R/W**           | ✅ WAL mode   | ✅ Native  | Limited |
| **Persistence**              | ✅ Single file | ✅ Yes     | ✅ Yes |
| **Portability**              | ✅ Move one file | ❌ Network | ✅ One file |
| **Query Speed**              | Good (< 50ms)  | ✅ Excellent | ✅ Analytics |
| **Local Dev Use**            | ✅ Best       | Unnecessary | Better for analytics |

**Why NOT PostgreSQL:** Requires running a server. For a personal tool triggered by cron or manual CLI, this introduces operational overhead. SQLite is simpler.

**Why NOT DuckDB:** Optimized for analytical queries (OLAP), not frequent upserts and reads (OLTP). Multiple 2025 benchmarks confirm DuckDB is 5–20× faster for analytics but slower for OLTP point lookups and upserts. A "SQLite OLTP + DuckDB analytics bridge" pattern exists but is overkill for this project's scale.

**Recommendation:** **SQLite with WAL (Write-Ahead Logging)** mode.
- WAL enables safe concurrent reads + writes: Multiple scraping threads can upsert while the analyzer reads.
- Single file (`.db`): Easy to backup, move, or commit (if anonymized).
- Proven at scale: Chromium, Firefox, Slack all use SQLite for local data.

> **⚠️ Critical WAL caveat (confirmed 2025):** WAL mode does NOT mean multiple threads write simultaneously. SQLite still has a **single-writer lock**. WAL only means readers don't block writers (and vice versa). If two threads try to write concurrently, one queues behind the other. `BEGIN CONCURRENT` (true multi-writer) exists only in an experimental SQLite branch — not in mainline. See Section 7 for the recommended write-queue pattern that handles this correctly.

---

## 2. Schema Design

### Core Tables

```sql
-- ============================================================================
-- Database: ai_job_search.db
-- Tables:
--   jobs         — core job data, deduplicated by URL
--   job_scores   — analysis results, separate from raw data
--   job_insights — global findings (trends, skills advice)
--   scrape_runs  — audit trail of when scraping happened
-- ============================================================================

-- Table: jobs
-- Dedup key: URL (more reliable than company+title which can change).
-- Timestamps: first_seen_at (first scrape), refreshed_at (last confirmed active).
CREATE TABLE jobs (
    job_id          INTEGER  PRIMARY KEY AUTOINCREMENT,
    job_url         TEXT     NOT NULL UNIQUE,
    site            TEXT     NOT NULL,  -- 'indeed', 'linkedin', 'remotive', etc.
    title           TEXT     NOT NULL,
    company         TEXT     NOT NULL,
    location        TEXT,
    job_type        TEXT,               -- 'fulltime', 'contract', 'parttime', etc.
    description     TEXT,
    first_seen_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    refreshed_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    scrape_run_id   INTEGER,            -- FK to scrape_runs (nullable if imported)
    FOREIGN KEY (scrape_run_id) REFERENCES scrape_runs(run_id) ON DELETE SET NULL
);

CREATE INDEX idx_jobs_url         ON jobs(job_url);
CREATE INDEX idx_jobs_refreshed   ON jobs(refreshed_at DESC);
CREATE INDEX idx_jobs_site        ON jobs(site, company);
CREATE INDEX idx_jobs_first_seen  ON jobs(first_seen_at DESC);


-- Table: criteria
-- Stores named, versioned scoring criteria configurations.
-- A criteria row captures exactly what the LLM was asked to evaluate against,
-- so any score can be fully reproduced or compared across criteria versions.
CREATE TABLE criteria (
    criteria_id   INTEGER  PRIMARY KEY AUTOINCREMENT,
    name          TEXT     NOT NULL,             -- e.g. 'senior-eng-remote-2026-q1'
    description   TEXT     NOT NULL,             -- the full criteria prompt/text
    weights       TEXT,                          -- JSON: {"relevance": 0.4, "duties": 0.4, "income": 0.2}
    qualifiers    TEXT,                          -- JSON array of hard-pass rules
    disqualifiers TEXT,                          -- JSON array of hard-fail rules
    criteria_hash TEXT     NOT NULL UNIQUE,      -- SHA256(description+weights+qualifiers+disqualifiers)
    is_active     BOOLEAN  NOT NULL DEFAULT 0,   -- marks the currently default criteria
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_criteria_active ON criteria(is_active);
CREATE INDEX idx_criteria_hash   ON criteria(criteria_hash);


-- Table: job_scores
-- One row per (job, criteria) pair — multiple scores per job are allowed and expected.
-- Separate from jobs because:
--   1. Criteria evolve; old scores remain valid for their criteria version.
--   2. A/B testing different criteria sets is first-class.
--   3. Re-scoring with new criteria does not touch the jobs table.
CREATE TABLE job_scores (
    score_id            INTEGER  PRIMARY KEY AUTOINCREMENT,
    job_id              INTEGER  NOT NULL,
    criteria_id         INTEGER  NOT NULL,
    is_qualified        BOOLEAN  NOT NULL DEFAULT 1,
    disqualified_reason TEXT,
    score_overall       REAL,
    score_relevance     REAL,
    score_duties        REAL,
    score_income        REAL,
    reasoning           TEXT,
    model_name          TEXT     NOT NULL DEFAULT 'claude-3.5-sonnet',
    scored_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (job_id, criteria_id),                -- one score per (job, criteria) pair
    FOREIGN KEY (job_id)      REFERENCES jobs(job_id)         ON DELETE CASCADE,
    FOREIGN KEY (criteria_id) REFERENCES criteria(criteria_id) ON DELETE CASCADE
);

CREATE INDEX idx_job_scores_qualified   ON job_scores(criteria_id, is_qualified, score_overall DESC);
CREATE INDEX idx_job_scores_scored_at   ON job_scores(scored_at DESC);
CREATE INDEX idx_job_scores_job         ON job_scores(job_id);


-- Table: job_insights
-- Global insights extracted from top-scoring jobs per analysis run.
CREATE TABLE job_insights (
    insight_id           INTEGER  PRIMARY KEY AUTOINCREMENT,
    skills_advice        TEXT,
    top_skills           TEXT,    -- JSON array of skill recommendations
    market_trends        TEXT,
    scrape_run_id        INTEGER,
    analysis_date        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    jobs_analyzed_count  INTEGER,
    model_name           TEXT     NOT NULL DEFAULT 'claude-3.5-sonnet',
    FOREIGN KEY (scrape_run_id) REFERENCES scrape_runs(run_id) ON DELETE SET NULL
);


-- Table: scrape_runs
-- Audit trail of scraping sessions.
CREATE TABLE scrape_runs (
    run_id                   INTEGER  PRIMARY KEY AUTOINCREMENT,
    started_at               TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at             TIMESTAMP,
    keywords                 TEXT     NOT NULL,  -- JSON array
    locations                TEXT     NOT NULL,  -- JSON array
    sites                    TEXT     NOT NULL,  -- JSON array
    raw_results_count        INTEGER,
    deduplicated_count       INTEGER,
    new_jobs_inserted        INTEGER,
    existing_jobs_updated    INTEGER,
    hours_old                INTEGER,
    results_per_site         INTEGER,
    status                   TEXT     NOT NULL DEFAULT 'pending',  -- pending|in_progress|completed|failed
    error_message            TEXT
);

CREATE INDEX idx_scrape_runs_completed ON scrape_runs(completed_at DESC);
CREATE INDEX idx_scrape_runs_status    ON scrape_runs(status);


-- View: jobs_unscored
-- Jobs not yet scored under the currently active criteria.
-- Filter by criteria_id in application code for other criteria versions:
--   SELECT * FROM jobs_unscored_for_criteria(?) -- see query in database.py
CREATE VIEW jobs_unscored AS
SELECT j.job_id, j.job_url, j.title, j.company, j.location, j.description, j.site
FROM jobs j
LEFT JOIN job_scores js
       ON j.job_id = js.job_id
      AND js.criteria_id = (SELECT criteria_id FROM criteria WHERE is_active = 1 LIMIT 1)
WHERE js.score_id IS NULL
ORDER BY j.refreshed_at DESC;


-- View: jobs_qualified
-- Jobs that passed qualification under the active criteria, ranked by score.
CREATE VIEW jobs_qualified AS
SELECT
    j.job_id,
    j.job_url,
    j.title,
    j.company,
    j.location,
    j.job_type,
    j.site,
    j.first_seen_at,
    j.refreshed_at,
    js.score_overall,
    js.score_relevance,
    js.score_duties,
    js.score_income,
    js.reasoning,
    js.scored_at,
    c.name  AS criteria_name
FROM jobs j
INNER JOIN job_scores js ON j.job_id = js.job_id
INNER JOIN criteria c    ON js.criteria_id = c.criteria_id
WHERE js.is_qualified = 1
  AND c.is_active = 1
ORDER BY js.score_overall DESC NULLS LAST;


-- Recommended PRAGMA settings (set on each connection open)
-- PRAGMA journal_mode = WAL;       -- write-ahead logging for concurrent R/W
-- PRAGMA synchronous   = NORMAL;   -- balance of safety + speed
-- PRAGMA cache_size    = -64000;   -- 64 MB cache
-- PRAGMA temp_store    = MEMORY;
-- PRAGMA busy_timeout  = 5000;     -- 5s wait on lock contention
```

---

## 3. Ingestion Pipeline Design

### JobSpy Status (2025–2026 Research)

JobSpy (`python-jobspy`) now supports **8 job boards**: LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google Jobs, Bayt, Naukri, BDJobs.

**Known reliability issues (confirmed from GitHub issues and community reports):**

| Site | Status | Notes |
|------|--------|-------|
| **LinkedIn** | ✅ Working | May have `search_term` quirks; use exact quotes |
| **Indeed** | ✅ Working | Most reliable |
| **Glassdoor** | ✅ Working | Generally stable |
| **ZipRecruiter** | ⚠️ Intermittent | 403/Cloudflare WAF errors reported in 2025 |
| **Google Jobs** | ⚠️ Intermittent | "initial cursor not found" — 0 results common |
| **Bayt/Naukri/BDJobs** | ❓ Regional | Not relevant for US searches |

**Implication for the scraper:** Wrap each `scrape_jobs()` call in a try/except and log failures per-site per-keyword. Don't let one blocked site (e.g. ZipRecruiter 403) cancel an entire keyword run. The DB architecture is what makes retries safe — re-run just the failed combinations without duplicating results.

```python
def scrape_one_combo(keyword: str, location: str, sites: list[str], run_id: int, writer: SQLiteWriteQueue):
    """Scrape one (keyword, location) pair, upsert per-site, tolerate per-site failures."""
    for site in sites:
        try:
            jobs_df = scrape_jobs(
                site_name=[site],
                search_term=keyword,
                location=location,
                results_wanted=50,
                hours_old=cfg['search']['hours_old'],
                linkedin_fetch_description=True,  # required for full description
            )
            for _, row in jobs_df.iterrows():
                writer.enqueue(UPSERT_SQL, (
                    row.get('job_url'), site, row['title'], row['company'],
                    row.get('location'), row.get('job_type'), row.get('description'), run_id
                ))
        except Exception as e:
            logging.warning(f"[{site}] failed for '{keyword}' @ {location}: {e}")
            # Continue to the next site; don't abort the whole keyword run
```

### Architecture

```
config.yaml: keywords[], locations[], sites[]
                     │
            ┌────────▼────────┐
            │ Parse multi-    │
            │ keyword combos  │
            └────────┬────────┘
                     │
     ┌───────────────┼───────────────┐
     │               │               │
┌────▼────┐    ┌────▼────┐    ┌────▼────┐
│Keyword A│    │Keyword B│    │Keyword C│
│Location1│    │Location2│    │Location1│
└────┬────┘    └────┬────┘    └────┬────┘
     │               │               │
     └───────────────┴───────────────┘
                     │
     ┌───────────────▼───────────────────────┐
     │  ThreadPoolExecutor(max_workers=3)    │
     │  Each thread:                         │
     │    1. run_scrape(keyword, location)   │
     │    2. Upsert to DB (ON CONFLICT...)   │
     │       – new URL → INSERT             │
     │       – known URL → UPDATE refreshed_at│
     └───────────────┬───────────────────────┘
                     │
     ┌───────────────▼───────────────────────┐
     │ Update scrape_run: inserted/refreshed │
     └───────────────────────────────────────┘
```

### Upsert Pattern

```python
conn.execute(
    """
    INSERT INTO jobs (job_url, site, title, company, location, job_type, description, scrape_run_id)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(job_url) DO UPDATE SET
        refreshed_at  = CURRENT_TIMESTAMP,
        scrape_run_id = excluded.scrape_run_id
    """,
    (job['job_url'], job['site'], job['title'], job['company'],
     job.get('location'), job.get('job_type'), job.get('description'), run_id)
)
```

**Key points:**
- `ON CONFLICT` handles dedup at the DB layer — no application-side checking needed.
- `first_seen_at` is never updated (set once at INSERT via DEFAULT).
- `refreshed_at` updates on every re-scrape, proving the posting is still live.
- **All writes go through `SQLiteWriteQueue`** (see Section 7) — scraper threads never hold a direct SQLite write lock.

---

## 4. Analysis Pipeline Design

### CrewAI Status (v1.1.0, October 2025)

Key capabilities relevant to this project:
- **`async_execution=True`** on a Task: runs that task in parallel with other async tasks in the same crew
- **`kickoff_async()` / `kickoff_for_each_async()`**: kick off an entire crew without blocking
- **CrewAI Flows**: event-driven orchestration layer above crews; use `@listen` to fan multiple crews out in parallel
- **Important:** CrewAI's parallelism dispatches threads internally — any SQLite writes from within a crew's task must go through the write queue to be safe

**Recommended approach for this project:** Keep the analysis pipeline as a plain Python `ThreadPoolExecutor` (not CrewAI tasks) for the per-job scoring loop, since that's I/O-bound (LLM API calls). Use CrewAI's Crew/Agent abstraction only for the LLM calls themselves (the `score_single_job` function), not for the parallelism. This gives you full control over the write queue and avoids CrewAI's threading conflicting with your DB writes.

```python
# CrewAI handles the LLM reasoning; Python handles parallelism and DB writes
def score_single_job(job: dict, config: dict) -> dict:
    """Call CrewAI crew for one job. Returns scoring result dict."""
    scoring_crew = Crew(
        agents=[qualifier_agent, scorer_agent],
        tasks=[qualify_task, score_task],
        process=Process.sequential,
        verbose=False,
    )
    result = scoring_crew.kickoff(inputs={"job": job, "criteria": config["criteria"]})
    return parse_crew_output(result)  # -> {"score": 8.2, "reasoning": "...", ...}
```

Then the parallel driving loop owns DB interactions:
```python
with ThreadPoolExecutor(max_workers=10) as pool:
    futures = {pool.submit(score_single_job, job, cfg): job for job in unscored}
    for future in as_completed(futures):
        result = future.result()  # blocks on LLM response, not on DB
        writer.enqueue(INSERT_SCORE_SQL, (result["job_id"], result["score"], ...))
        # writer is non-blocking: returns immediately, DB write serialized in background
```

### Independent from Scraping

```
Load config (criteria, weights, qualifiers)
                │
  ┌─────────────▼─────────────┐
  │ SELECT * FROM jobs_unscored│
  └─────────────┬─────────────┘
                │
  ┌─────────────▼──────────────────────────────┐
  │ ThreadPoolExecutor(max_workers=10)         │
  │ For each job:                              │
  │   1. LLM evaluate qualifiers/disqualifiers │
  │   2. If qualified: score (relevance, etc.) │
  │   3. INSERT into job_scores               │
  └─────────────┬──────────────────────────────┘
                │
  ┌─────────────▼─────────────────────────────┐
  │ Skills Advisor: top N from jobs_qualified  │
  │ → INSERT into job_insights                │
  └───────────────────────────────────────────┘
```

**Why this works concurrently:**
- Scraper inserts into `jobs`; analyzer reads from `jobs_unscored` (which LEFT JOINs `job_scores`).
- They touch different tables and WAL prevents conflicts.
- Scoring threads only write to `job_scores` — no lock on `jobs`.

**Re-scoring with new criteria:**
```bash
python main.py --mode analyze --rescore-all
# Deletes job_scores rows, re-runs analysis with current criteria_hash
```

**Detecting stale scores (criteria changed):**
```sql
SELECT count(*) FROM job_scores
WHERE criteria_hash != '<current_hash>';
```

---

## 5. Migration Strategy

Import existing CSV/JSON on first run (before DB exists):

```python
def migrate_from_csv_json(db_path, csv_path=None, json_path=None):
    conn = sqlite3.connect(db_path)

    if csv_path:
        # Create synthetic scrape_run for imported data
        cursor = conn.execute(
            "INSERT INTO scrape_runs (keywords, locations, sites, status, completed_at) "
            "VALUES (?, ?, ?, 'completed', CURRENT_TIMESTAMP)",
            ('["migrated"]', '["migrated"]', '["csv-import"]')
        )
        run_id = cursor.lastrowid

        for job in csv.DictReader(open(csv_path)):
            conn.execute(
                "INSERT OR IGNORE INTO jobs "
                "(job_url, site, title, company, location, job_type, description, scrape_run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job['job_url'], job['site'], job['title'], job['company'],
                 job.get('location'), job.get('job_type'), job.get('description'), run_id)
            )
        conn.commit()

    if json_path:
        data = json.load(open(json_path))
        for scored_job in data.get('scored_jobs', []):
            row = conn.execute(
                "SELECT job_id FROM jobs WHERE job_url = ?",
                (scored_job.get('job_url'),)
            ).fetchone()
            if row:
                conn.execute(
                    "INSERT OR IGNORE INTO job_scores "
                    "(job_id, is_qualified, score_overall, score_relevance, "
                    " score_duties, score_income, reasoning, model_name) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (row[0], not scored_job.get('disqualified', False),
                     scored_job.get('score'), scored_job.get('score_relevance'),
                     scored_job.get('score_duties'), scored_job.get('score_income'),
                     scored_job.get('reasoning'), 'claude-3.5-sonnet')
                )
        conn.commit()

    conn.close()
```

Called in `main.py` if the DB file does not yet exist.

---

## 6. CLI / Run Interface

```bash
# Scrape only (multi-keyword parallel ingestion)
python main.py --mode scrape

# Analyze only (score unscored jobs from DB)
python main.py --mode analyze

# Report only (Markdown from DB)
python main.py --mode report

# Full pipeline (default)
python main.py --mode pipeline
python main.py                    # same as above

# Overrides
python main.py --mode scrape --keywords "python engineer" "backend engineer" --locations "remote" "NYC"
python main.py --mode analyze --rescore-all
python main.py --mode report --top 20 --output /tmp/jobs.md
```

`main.py` argument structure:

```python
parser.add_argument('--mode', choices=['scrape', 'analyze', 'report', 'pipeline'], default='pipeline')
parser.add_argument('--keywords', nargs='+')
parser.add_argument('--locations', nargs='+')
parser.add_argument('--rescore-all', action='store_true')
parser.add_argument('--top', type=int)
parser.add_argument('--output')
```

And `config.yaml` should expand `search.term` into a `search.keywords` list:

```yaml
search:
  keywords:
    - "python engineer"
    - "backend engineer"
    - "staff engineer"
  locations:
    - "remote"
    - "New York"
  sites_list:
    - indeed
    - zip_recruiter
    - linkedin
  results_wanted: 50
  hours_old: 72
```

---

## 7. Concurrency Safety

### The Real SQLite Concurrency Story (2025 Research)

WAL mode is widely misunderstood. Here is what actually happens:

| Scenario | SQLite + WAL | Reality |
|----------|-------------|---------|
| Scraper writes, Analyzer reads | ✅ Safe | WAL snapshot isolates readers from in-flight writes |
| Two scrapers write simultaneously | ⚠️ Queued | **One writer holds the lock; others wait up to `busy_timeout`** |
| Analyzer + Reporter both read | ✅ Safe | Readers never block each other in WAL mode |
| Analyze writes scores while scraper inserts jobs | ⚠️ Queued | Different tables, same lock — still serialized |

**Key finding:** `BEGIN CONCURRENT` (true parallel multi-writer for SQLite) exists only in an experimental branch and is **not in mainline SQLite as of 2025**. Turso/LibSQL offer it as a commercial extension.

### Recommended Pattern: Single Write-Queue Thread

The correct architecture for multi-threaded SQLite writes is to **serialize writes at the application layer** before they reach the DB. Multiple producer threads feed a single `queue.Queue`; one dedicated writer thread drains it.

```
 ┌──────────────────────────────────────────────────────────┐
 │  SCRAPPER THREADS (3)         ANALYZER THREADS (10)      │
 │  ThreadPoolExecutor           ThreadPoolExecutor         │
 │        │                             │                   │
 │        │  upsert(job_dict)           │  write(score_dict)│
 │        └──────────────┬──────────────┘                   │
 │                       ▼                                  │
 │             ┌─────────────────┐                          │
 │             │  write_queue    │  queue.Queue()           │
 │             │  (unbounded)    │  Non-blocking enqueue    │
 │             └────────┬────────┘                          │
 │                      │                                   │
 │             ┌────────▼────────┐                          │
 │             │  Writer Thread  │  Single dedicated thread │
 │             │  (serialized)   │  One SQLite connection   │
 │             │  bulk commits   │  Batch every N ops       │
 │             └─────────────────┘                          │
 └──────────────────────────────────────────────────────────┘
```

```python
# src/database.py
import sqlite3
import queue
import threading
from dataclasses import dataclass, field
from typing import Optional, Callable

@dataclass
class WriteOp:
    sql: str
    params: tuple = ()
    result_future: Optional["Future"] = field(default=None, repr=False)

class SQLiteWriteQueue:
    """
    Dedicated writer thread that serializes all writes to SQLite.
    Scraper and analyzer threads call enqueue() and never hold a
    SQLite write lock themselves.
    """
    def __init__(self, db_path: str, batch_size: int = 50):
        self._q: queue.Queue[Optional[WriteOp]] = queue.Queue()
        self._db_path = db_path
        self._batch_size = batch_size
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def enqueue(self, sql: str, params: tuple = ()) -> None:
        """Non-blocking. Caller does not wait for the write to complete."""
        self._q.put(WriteOp(sql=sql, params=params))

    def flush(self) -> None:
        """Block until all queued writes are committed."""
        done = threading.Event()
        self._q.put(None)  # sentinel
        self._q.join()
        done.set()

    def close(self) -> None:
        self.flush()
        # Thread exits on sentinel
        self._thread.join()

    def _worker(self) -> None:
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA cache_size = -64000")
        conn.execute("PRAGMA foreign_keys = ON")
        batch = []

        def commit_batch():
            if batch:
                conn.executemany.__doc__  # noop to avoid import
                for op in batch:
                    conn.execute(op.sql, op.params)
                conn.commit()
                batch.clear()

        while True:
            try:
                op = self._q.get(timeout=0.1)
                if op is None:
                    commit_batch()
                    self._q.task_done()
                    break
                batch.append(op)
                self._q.task_done()
                if len(batch) >= self._batch_size:
                    commit_batch()
            except queue.Empty:
                commit_batch()  # flush any partial batch on idle

        conn.close()
```

**Read connections are separate and can be per-thread:**
```python
# Read-only queries don't go through the write queue
def get_unscored_jobs(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM jobs_unscored LIMIT 1000").fetchall()
    conn.close()
    return [dict(r) for r in rows]
```

**Best practices (informed by 2025 production experience):**
1. **Never hold a write lock during an LLM API call.** Score in memory, then write results via the queue.
2. Scraping threads: do the HTTP fetch, parse the response → enqueue the upsert. The DB call is microseconds, the HTTP fetch is seconds.
3. `busy_timeout = 30000` (30s) as a backstop on all connections, even though the queue should prevent contention.
4. WAL checkpoint periodically: `PRAGMA wal_checkpoint(TRUNCATE)` after a large batch scrape.

**Alternative: `sqlite-worker` library** (PyPI `sqlite-worker`): implements this same pattern with less boilerplate if you prefer not to roll your own.

---

## 8. Reporting from DB

```python
def generate_report_from_db(db_path, output_path, top_n=10):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    top_jobs = conn.execute("SELECT * FROM jobs_qualified LIMIT ?", (top_n,)).fetchall()

    latest_insight = conn.execute(
        "SELECT skills_advice FROM job_insights ORDER BY analysis_date DESC LIMIT 1"
    ).fetchone()

    stats = conn.execute("""
        SELECT
            COUNT(*)                                                AS total_jobs,
            COUNT(CASE WHEN js.is_qualified THEN 1 END)            AS qualified_count,
            COUNT(CASE WHEN js.job_id IS NULL THEN 1 END)          AS unscored_count
        FROM jobs j
        LEFT JOIN job_scores js ON j.job_id = js.job_id
    """).fetchone()

    conn.close()

    lines = [
        f"# Job Search Results — {datetime.now():%Y-%m-%d}",
        f"> **Total in DB:** {stats['total_jobs']}  |  "
        f"**Qualified:** {stats['qualified_count']}  |  "
        f"**Unscored:** {stats['unscored_count']}",
        "",
    ]
    for i, job in enumerate(top_jobs, 1):
        lines += [
            f"## {i}. {job['title']}",
            f"**{job['company']}** · {job['location']} · {job['job_type']}",
            f"Score: **{job['score_overall']:.1f}** "
            f"(Relevance {job['score_relevance']:.0f} · Duties {job['score_duties']:.0f} · Income {job['score_income']:.0f})",
            f"**Why:** {job['reasoning']}",
            f"**Apply:** [{job['job_url']}]({job['job_url']})",
            "",
        ]
    if latest_insight:
        lines += ["## Skills Advice", latest_insight['skills_advice'], ""]

    output_path.write_text('\n'.join(lines), encoding='utf-8')
```

No intermediate JSON files. All data comes from the DB views.

---

## 9. Implementation Roadmap

### Phase 1 — Schema & Migration
- [ ] Create `src/database.py` with DDL, `init_db()`, and `migrate_from_csv_json()`
- [ ] Test migration on existing `output/` CSVs and JSONs
- [ ] Verify WAL pragmas are set correctly

### Phase 2 — Ingestion
- [ ] Expand `config.yaml` to support `search.keywords` list
- [ ] Refactor `scraper.py` to accept keyword + location args, return list of dicts
- [ ] Implement `ingest_parallel_keywords()` with upsert logic
- [ ] Update `main.py --mode scrape`

### Phase 3 — Analysis
- [ ] Refactor `pipeline.py` to read from `jobs_unscored` view
- [ ] Implement parallel scoring + `job_scores` insertion
- [ ] Write insights to `job_insights` table
- [ ] Update `main.py --mode analyze`, add `--rescore-all`

### Phase 4 — Reporting
- [ ] Implement `generate_report_from_db()` in `reporter.py`
- [ ] Remove dependency on intermediate JSON files
- [ ] Update `main.py --mode report`

### Phase 5 — Integration
- [ ] Test full pipeline end-to-end
- [ ] Test `--mode scrape` and `--mode analyze` running concurrently
- [ ] Add a lightweight status query: `python main.py --status`

---

## 10. Operational Notes

### Backup
```bash
# Snapshot before a run (safe with WAL — SQLite backup API handles in-flight writes)
sqlite3 ai_job_search.db ".backup ai_job_search.db.$(date +%Y%m%d).bak"
```

### Health Queries
```sql
-- How many jobs haven't been scored yet?
SELECT COUNT(*) FROM jobs_unscored;

-- Scores that are stale (criteria changed)?
SELECT COUNT(*) FROM job_scores WHERE criteria_hash != '<current_hash>';

-- Jobs no longer seen in last 30 days (probably expired)
SELECT COUNT(*) FROM jobs WHERE refreshed_at < datetime('now', '-30 days');
```

### Performance (at 10K+ jobs)
```sql
-- Partial index: only unscored jobs
CREATE INDEX idx_jobs_unscored_partial ON jobs(job_id)
WHERE job_id NOT IN (SELECT job_id FROM job_scores);
```

---

## Summary

| Aspect | Decision |
|--------|----------|
| **Database** | SQLite + WAL mode |
| **Dedup key** | `job_url` (immutable, globally unique) |
| **Freshness signal** | `refreshed_at` updated on every re-scrape |
| **Write concurrency** | Single `SQLiteWriteQueue` thread (queue.Queue pattern) |
| **Read concurrency** | Per-thread read-only connections (WAL snapshot isolation) |
| **Scrape parallelism** | `ThreadPoolExecutor(3)` — one per `(keyword × location)` combo |
| **Analysis parallelism** | `ThreadPoolExecutor(10)` — LLM calls in parallel, writes serialized |
| **CrewAI role** | Per-job LLM calls only; Python owns threading and DB writes |
| **Pipeline independence** | Scrape ∥ Analyze ∥ Report via `--mode` flag |
| **Score versioning** | `criteria` table (FK from `job_scores`); one score per `(job_id, criteria_id)` pair |
| **Migration** | CSV/JSON → DB on first run |
| **Job boards (JobSpy)** | LinkedIn + Indeed (reliable); ZipRecruiter/Google (intermittent) |
| **Per-site error handling** | try/except per site; log, continue, don't abort keyword run |
| **CLI modes** | `--mode {scrape,analyze,report,pipeline}` |

---

## Architecture: Modularity Commentary

The three patterns you identified are the right decomposition and map cleanly to separate modules with a single shared dependency (the DB file):

```
┌────────────────┐     ┌────────────────┐     ┌─────────────────────┐
│  database.py   │     │  pipeline.py   │     │  maintenance.py     │
│  (Storage)     │     │  (Scoring      │     │  (DB Maintenance)   │
│                │     │   Engine)      │     │                     │
│ – DDL / init   │◄────│ – reads        │     │ – WAL checkpoint    │
│ – WriteQueue   │◄────│   unscored     │     │ – stale job cleanup │
│ – migration    │     │ – CrewAI calls │     │ – re-score trigger  │
│ – read helpers │     │ – enqueues     │     │ – criteria mgmt     │
└────────────────┘     │   scores       │     └─────────────────────┘
       ▲               └────────────────┘               ▲
       │                                                 │
       └──────────────── main.py (CLI) ─────────────────┘
                         scraper.py (ingestion)
                         reporter.py (output)
```

**Observations:**

1. **Storage layer** (`database.py`) is the only module the others depend on. It exposes `SQLiteWriteQueue`, `init_db()`, and read helpers. No other module opens raw connections. This makes the storage backend fully swappable.

2. **Scoring engine** (`pipeline.py`) has no knowledge of *how* jobs got into the DB or *how* scores get persisted — it reads a list of jobs, calls the LLM, and hands dicts to the write queue. Swapping CrewAI for another LLM client is a one-file change.

3. **DB maintenance** is the weakest boundary today — it currently lives scattered across `main.py` (the `--rescore-all` flag), per-run scrape_run bookkeeping, and pragma calls. Pulling it into its own module (or a `main.py --db` subcommand) would make the separation explicit and allow scheduled maintenance without triggering a full pipeline run.

**The scraper** (`scraper.py`) sits at the same level as the scoring engine — depends on storage, knows nothing about scoring. That's the right design.

**The reporter** (`reporter.py`) is pure read — no writes, no scoring logic. Already well-separated.

---

## Research Sources (March 2026)

Key findings from live Brave Search research that updated the initial plan:

- **SQLite single-writer reality:** WAL mode does not enable true concurrent writes. One writer holds the lock; others queue. `BEGIN CONCURRENT` is experimental-only (not in mainline SQLite). → Changed architecture to write-queue pattern. *(Source: Turso blog, SQLite HN thread, oneuptime.com 2026-02 article)*
- **DuckDB confirmed OLAP-only:** 5–20× slower than SQLite on OLTP upsert workloads in all 2025 benchmarks. A SQLite+DuckDB analytics bridge exists but unnecessary at this project's scale. *(Source: DataCamp, Orchestra, MotherDuck comparisons)*
- **JobSpy expanded to 8 sites:** LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google Jobs, Bayt, Naukri, BDJobs. ZipRecruiter and Google Jobs have active 403/WAF blocking. → Wrap each site call in try/except. *(Source: JobSpy GitHub issue #302, Reddit r/selfhosted)*
- **CrewAI v1.1.0 (Oct 2025):** Flows and `kickoff_async` are production-ready. Recommended to use CrewAI only for LLM reasoning per-job; use Python `ThreadPoolExecutor` for outer parallelism to retain DB write control. *(Source: CrewAI community, markaicode.com Flows 2026 tutorial)*
- **Write-queue pattern:** Production best practice for multi-threaded SQLite is a single dedicated writer thread draining a `queue.Queue`. The `sqlite-worker` PyPI package implements this. *(Source: oneuptime.com production guide, shivekkhurana.com SQLite benchmarks, Medium post by Roshan Lamichhane)*
