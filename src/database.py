"""
src/database.py — SQLite storage layer for the AI Job Search pipeline.

Provides:
  - init_db(db_path)             — create tables, indexes, views (idempotent)
  - SQLiteWriteQueue             — single-writer thread for safe concurrent writes
  - get_or_create_criteria(...)  — resolve active criteria_id from config, insert if new
  - start_scrape_run(...)        — open a scrape_runs audit row, return run_id
  - finish_scrape_run(...)       — close the scrape_runs audit row
  - get_unscored_jobs(...)       — SELECT from jobs_unscored view
  - migrate_from_csv_json(...)   — import legacy output/ CSV+JSON on first run
  - db_status(...)               — summary counts for --status flag
"""

import csv
import hashlib
import json
import logging
import queue
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── DDL ──────────────────────────────────────────────────────────────────────
# Each statement is separated by a line containing only "---" so we can split
# cleanly without worrying about semicolons inside view bodies.

_DDL_STATEMENTS = [
    # scrape_runs
    """
CREATE TABLE IF NOT EXISTS scrape_runs (
    run_id                   INTEGER  PRIMARY KEY AUTOINCREMENT,
    started_at               TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at             TIMESTAMP,
    keywords                 TEXT     NOT NULL,
    locations                TEXT     NOT NULL,
    sites                    TEXT     NOT NULL,
    raw_results_count        INTEGER,
    deduplicated_count       INTEGER,
    new_jobs_inserted        INTEGER,
    existing_jobs_updated    INTEGER,
    hours_old                INTEGER,
    results_per_site         INTEGER,
    status                   TEXT     NOT NULL DEFAULT 'pending',
    error_message            TEXT
)
""",
    "CREATE INDEX IF NOT EXISTS idx_scrape_runs_completed ON scrape_runs(completed_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_scrape_runs_status    ON scrape_runs(status)",

    # jobs
    """
CREATE TABLE IF NOT EXISTS jobs (
    job_id          INTEGER  PRIMARY KEY AUTOINCREMENT,
    job_url         TEXT     NOT NULL UNIQUE,
    site            TEXT     NOT NULL,
    title           TEXT     NOT NULL,
    company         TEXT     NOT NULL,
    location        TEXT,
    job_type        TEXT,
    description     TEXT,
    canonical_key   TEXT,
    first_seen_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    refreshed_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    scrape_run_id   INTEGER,
    FOREIGN KEY (scrape_run_id) REFERENCES scrape_runs(run_id) ON DELETE SET NULL
)
""",
    "CREATE INDEX IF NOT EXISTS idx_jobs_url        ON jobs(job_url)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_refreshed  ON jobs(refreshed_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_site       ON jobs(site, company)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen_at DESC)",

    # criteria
    """
CREATE TABLE IF NOT EXISTS criteria (
    criteria_id   INTEGER  PRIMARY KEY AUTOINCREMENT,
    name          TEXT     NOT NULL,
    description   TEXT     NOT NULL,
    weights       TEXT,
    qualifiers    TEXT,
    disqualifiers TEXT,
    criteria_hash TEXT     NOT NULL UNIQUE,
    is_active     BOOLEAN  NOT NULL DEFAULT 0,
    is_enabled    BOOLEAN  NOT NULL DEFAULT 0,
    is_default    BOOLEAN  NOT NULL DEFAULT 0,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
""",
    "CREATE INDEX IF NOT EXISTS idx_criteria_active  ON criteria(is_active)",
    "CREATE INDEX IF NOT EXISTS idx_criteria_hash    ON criteria(criteria_hash)",
    # Note: idx_criteria_enabled and idx_criteria_default are created in init_db()
    # after the ALTER TABLE migration adds those columns to existing databases.

    # job_scores
    """
CREATE TABLE IF NOT EXISTS job_scores (
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
    UNIQUE (job_id, criteria_id),
    FOREIGN KEY (job_id)      REFERENCES jobs(job_id)           ON DELETE CASCADE,
    FOREIGN KEY (criteria_id) REFERENCES criteria(criteria_id)  ON DELETE CASCADE
)
""",
    "CREATE INDEX IF NOT EXISTS idx_job_scores_qualified ON job_scores(criteria_id, is_qualified, score_overall DESC)",
    "CREATE INDEX IF NOT EXISTS idx_job_scores_scored_at ON job_scores(scored_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_job_scores_job        ON job_scores(job_id)",

    # job_actions
    """
CREATE TABLE IF NOT EXISTS job_actions (
    action_id   INTEGER   PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER   NOT NULL,
    action_type TEXT      NOT NULL CHECK(action_type IN ('dismissed','saved','applied','archived','active')),
    note        TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
)
""",
    "CREATE INDEX IF NOT EXISTS idx_job_actions_job       ON job_actions(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_job_actions_type_time ON job_actions(action_type, created_at DESC)",

    # job_insights
    """
CREATE TABLE IF NOT EXISTS job_insights (
    insight_id           INTEGER  PRIMARY KEY AUTOINCREMENT,
    skills_advice        TEXT,
    top_skills           TEXT,
    market_trends        TEXT,
    scrape_run_id        INTEGER,
    analysis_date        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    jobs_analyzed_count  INTEGER,
    model_name           TEXT     NOT NULL DEFAULT 'claude-3.5-sonnet',
    FOREIGN KEY (scrape_run_id) REFERENCES scrape_runs(run_id) ON DELETE SET NULL
)
""",
]

# ── Canonical key ────────────────────────────────────────────────────────────

_TITLE_PREFIXES = re.compile(r"\b(sr|jr|senior|staff|principal|lead|associate)\.?\s*", re.I)
_COMPANY_SUFFIXES = re.compile(r"\b(inc|llc|ltd|corp|co)\.?\s*$", re.I)


def _normalize_title(title: str) -> str:
    t = _TITLE_PREFIXES.sub("", title.lower()).strip()
    return re.sub(r"\s+", " ", t)


def _normalize_company(company: str) -> str:
    c = _COMPANY_SUFFIXES.sub("", company.lower()).strip()
    return re.sub(r"\s+", " ", c)


def make_canonical_key(title: str, company: str) -> str:
    """Normalized dedup key for grouping the same job listing across sites."""
    return _normalize_company(company) + "|" + _normalize_title(title)


# SQL used by multiple callers
UPSERT_JOB_SQL = """
INSERT INTO jobs (job_url, site, title, company, location, job_type, description, canonical_key, scrape_run_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(job_url) DO UPDATE SET
    title         = excluded.title,
    refreshed_at  = CURRENT_TIMESTAMP,
    canonical_key = excluded.canonical_key,
    scrape_run_id = excluded.scrape_run_id
"""

INSERT_SCORE_SQL = """
INSERT OR IGNORE INTO job_scores
    (job_id, criteria_id, is_qualified, disqualified_reason,
     score_overall, score_relevance, score_duties, score_income, reasoning, model_name)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

INSERT_INSIGHT_SQL = """
INSERT INTO job_insights (skills_advice, jobs_analyzed_count, model_name)
VALUES (?, ?, ?)
"""


# ── View definitions (always recreated to pick up definition changes) ─────────

_VIEW_JOBS_UNSCORED = """
CREATE VIEW jobs_unscored AS
SELECT j.job_id, j.job_url, j.title, j.company, j.location, j.description, j.site, j.job_type
FROM jobs j
LEFT JOIN job_scores js
       ON j.job_id = js.job_id
      AND js.criteria_id = (SELECT criteria_id FROM criteria WHERE is_default = 1 LIMIT 1)
WHERE js.score_id IS NULL
ORDER BY j.refreshed_at DESC
"""

_VIEW_JOBS_QUALIFIED = """
CREATE VIEW jobs_qualified AS
SELECT
    j.job_id, j.job_url, j.title, j.company, j.location, j.job_type, j.site,
    j.first_seen_at, j.refreshed_at,
    js.score_overall, js.score_relevance, js.score_duties, js.score_income,
    js.reasoning, js.scored_at,
    c.name AS criteria_name
FROM jobs j
INNER JOIN job_scores js ON j.job_id = js.job_id
INNER JOIN criteria c    ON js.criteria_id = c.criteria_id
WHERE js.is_qualified = 1
  AND c.is_default = 1
ORDER BY js.score_overall DESC
"""

# ── Connection helper ─────────────────────────────────────────────────────────

def _open_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -64000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    return conn


# ── Schema init ───────────────────────────────────────────────────────────────

def init_db(db_path: Path) -> None:
    """Create schema (idempotent — safe to call on every startup)."""
    conn = _open_conn(db_path)
    for stmt in _DDL_STATEMENTS:
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    # Additive migration: add canonical_key column to existing DBs
    try:
        conn.execute("ALTER TABLE jobs ADD COLUMN canonical_key TEXT")
        log.info("Migration: added canonical_key column to jobs")
    except sqlite3.OperationalError:
        pass  # column already present on new or previously-migrated DBs
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_canonical ON jobs(canonical_key)")
    # Backfill rows that pre-date this column
    rows = conn.execute(
        "SELECT job_id, title, company FROM jobs WHERE canonical_key IS NULL"
    ).fetchall()
    if rows:
        updates = [
            (make_canonical_key(r["title"], r["company"]), r["job_id"]) for r in rows
        ]
        conn.executemany("UPDATE jobs SET canonical_key = ? WHERE job_id = ?", updates)
        log.info(f"Backfilled canonical_key for {len(rows)} existing jobs")

    # Additive migration: add is_enabled / is_default to criteria (Feature 2)
    for col, default in [("is_enabled", 0), ("is_default", 0)]:
        try:
            conn.execute(f"ALTER TABLE criteria ADD COLUMN {col} BOOLEAN NOT NULL DEFAULT {default}")
            log.info(f"Migration: added {col} column to criteria")
        except sqlite3.OperationalError:
            pass  # column already present
    conn.execute("CREATE INDEX IF NOT EXISTS idx_criteria_enabled ON criteria(is_enabled)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_criteria_default ON criteria(is_default)")
    # Backfill: the previously-active criteria becomes is_enabled=1, is_default=1
    conn.execute(
        "UPDATE criteria SET is_enabled = 1, is_default = 1 WHERE is_active = 1 AND is_default = 0"
    )

    # Additive migration: create job_actions table for dismissal (Feature 1)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_actions (
            action_id   INTEGER   PRIMARY KEY AUTOINCREMENT,
            job_id      INTEGER   NOT NULL,
            action_type TEXT      NOT NULL CHECK(action_type IN ('dismissed','saved','applied','archived','active')),
            note        TEXT,
            created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_actions_job       ON job_actions(job_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_actions_type_time ON job_actions(action_type, created_at DESC)")

    # Always recreate views so definition changes take effect on existing DBs
    conn.execute("DROP VIEW IF EXISTS jobs_unscored")
    conn.execute("DROP VIEW IF EXISTS jobs_qualified")
    conn.execute(_VIEW_JOBS_UNSCORED.strip())
    conn.execute(_VIEW_JOBS_QUALIFIED.strip())

    conn.commit()
    conn.close()
    log.debug(f"Database initialized: {db_path}")


# ── Criteria management ───────────────────────────────────────────────────────

def _criteria_hash(description: str, weights: dict, qualifiers: list, disqualifiers: list) -> str:
    payload = json.dumps(
        {"description": description, "weights": weights,
         "qualifiers": qualifiers, "disqualifiers": disqualifiers},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def get_or_create_criteria(db_path: Path, cfg: dict[str, Any]) -> int:
    """Return the criteria_id for the current config, inserting a new row if the
    criteria have changed since the last run. Exactly one row has is_active=1."""
    from config import build_criteria_string, get_criteria_weights, get_disqualifiers, get_qualifiers

    description = build_criteria_string(cfg)
    weights = get_criteria_weights(cfg)
    qualifiers = get_qualifiers(cfg)
    disqualifiers = get_disqualifiers(cfg)
    h = _criteria_hash(description, weights, qualifiers, disqualifiers)

    criteria_name = cfg.get("criteria", {}).get("name", "default")

    conn = _open_conn(db_path)
    row = conn.execute(
        "SELECT criteria_id, is_active FROM criteria WHERE criteria_hash = ?", (h,)
    ).fetchone()

    if row:
        criteria_id = row["criteria_id"]
        if not row["is_active"]:
            conn.execute("UPDATE criteria SET is_active = 0, is_default = 0")
            conn.execute(
                "UPDATE criteria SET is_active = 1, is_enabled = 1, is_default = 1 WHERE criteria_id = ?",
                (criteria_id,),
            )
            conn.commit()
            log.info(f"Reactivated existing criteria id={criteria_id}")
    else:
        conn.execute("UPDATE criteria SET is_active = 0, is_default = 0")
        cur = conn.execute(
            """
            INSERT INTO criteria (name, description, weights, qualifiers, disqualifiers, criteria_hash, is_active, is_enabled, is_default)
            VALUES (?, ?, ?, ?, ?, ?, 1, 1, 1)
            """,
            (
                criteria_name,
                description,
                json.dumps(weights),
                json.dumps(qualifiers),
                json.dumps(disqualifiers),
                h,
            ),
        )
        criteria_id = cur.lastrowid
        conn.commit()
        log.info(f"Inserted new criteria id={criteria_id} (hash={h[:12]}…)")

    conn.close()
    return criteria_id


# ── Scrape run audit ──────────────────────────────────────────────────────────

def start_scrape_run(
    db_path: Path,
    keywords: list[str],
    locations: list[str],
    sites: list[str],
    hours_old: int,
    results_per_site: int,
) -> int:
    """Insert a scrape_runs row with status='in_progress'. Returns run_id."""
    conn = _open_conn(db_path)
    cur = conn.execute(
        """
        INSERT INTO scrape_runs
            (keywords, locations, sites, hours_old, results_per_site, status)
        VALUES (?, ?, ?, ?, ?, 'in_progress')
        """,
        (json.dumps(keywords), json.dumps(locations), json.dumps(sites), hours_old, results_per_site),
    )
    run_id = cur.lastrowid
    conn.commit()
    conn.close()
    return run_id


def finish_scrape_run(
    db_path: Path,
    run_id: int,
    new_jobs: int,
    updated_jobs: int,
    raw_count: int,
    status: str = "completed",
    error: str | None = None,
) -> None:
    conn = _open_conn(db_path)
    conn.execute(
        """
        UPDATE scrape_runs SET
            completed_at          = CURRENT_TIMESTAMP,
            new_jobs_inserted     = ?,
            existing_jobs_updated = ?,
            raw_results_count     = ?,
            status                = ?,
            error_message         = ?
        WHERE run_id = ?
        """,
        (new_jobs, updated_jobs, raw_count, status, error, run_id),
    )
    conn.commit()
    conn.close()


# ── Read helpers ──────────────────────────────────────────────────────────────

def get_unscored_jobs(db_path: Path, criteria_id: int, limit: int = 2000) -> list[dict]:
    """Return jobs not yet scored under the given criteria_id."""
    conn = _open_conn(db_path)
    rows = conn.execute(
        """
        SELECT j.job_id, j.job_url, j.title, j.company, j.location, j.description, j.site, j.job_type
        FROM jobs j
        LEFT JOIN job_scores js ON j.job_id = js.job_id AND js.criteria_id = ?
        WHERE js.score_id IS NULL
        ORDER BY j.refreshed_at DESC LIMIT ?
        """,
        (criteria_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_qualified_jobs(db_path: Path, top_n: int = 50) -> list[dict]:
    """Return top-scoring qualified jobs under the active criteria."""
    conn = _open_conn(db_path)
    rows = conn.execute("SELECT * FROM jobs_qualified LIMIT ?", (top_n,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_job_id_by_url(conn: sqlite3.Connection, job_url: str) -> int | None:
    row = conn.execute("SELECT job_id FROM jobs WHERE job_url = ?", (job_url,)).fetchone()
    return row["job_id"] if row else None


def db_status(db_path: Path) -> dict[str, int]:
    """Return summary counts for the --status display."""
    conn = _open_conn(db_path)
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    unscored = conn.execute("SELECT COUNT(*) FROM jobs_unscored").fetchone()[0]
    qualified = conn.execute(
        "SELECT COUNT(*) FROM job_scores WHERE is_qualified = 1"
        " AND criteria_id = (SELECT criteria_id FROM criteria WHERE is_default = 1 LIMIT 1)"
    ).fetchone()[0]
    disqualified = conn.execute(
        "SELECT COUNT(*) FROM job_scores WHERE is_qualified = 0"
        " AND criteria_id = (SELECT criteria_id FROM criteria WHERE is_default = 1 LIMIT 1)"
    ).fetchone()[0]
    scrape_runs = conn.execute("SELECT COUNT(*) FROM scrape_runs").fetchone()[0]
    conn.close()
    return {
        "total_jobs": total,
        "unscored": unscored,
        "qualified": qualified,
        "disqualified": disqualified,
        "scrape_runs": scrape_runs,
    }


def get_all_enabled_criteria(db_path: Path) -> list[int]:
    """Return criteria_ids for all enabled criteria (those that score new jobs)."""
    conn = _open_conn(db_path)
    rows = conn.execute("SELECT criteria_id FROM criteria WHERE is_enabled = 1").fetchall()
    conn.close()
    return [r[0] for r in rows]


def record_job_action(db_path: Path, job_id: int, action_type: str, note: str | None = None) -> None:
    """Record a user action against a job (e.g. dismissed, saved, applied).

    dismissed and active (restore) propagate to all jobs sharing the same
    canonical_key so deduped siblings are hidden/restored together.
    """
    conn = _open_conn(db_path)
    # Propagate dismissed / restore to all canonical siblings
    if action_type in ("dismissed", "active"):
        row = conn.execute("SELECT canonical_key FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        canonical_key = row[0] if row else None
        if canonical_key:
            sibling_ids = [
                r[0]
                for r in conn.execute(
                    "SELECT job_id FROM jobs WHERE canonical_key = ?", (canonical_key,)
                ).fetchall()
            ]
            conn.executemany(
                "INSERT INTO job_actions (job_id, action_type, note) VALUES (?, ?, ?)",
                [(sid, action_type, note) for sid in sibling_ids],
            )
            conn.commit()
            conn.close()
            return
    conn.execute(
        "INSERT INTO job_actions (job_id, action_type, note) VALUES (?, ?, ?)",
        (job_id, action_type, note),
    )
    conn.commit()
    conn.close()


# ── Write queue ───────────────────────────────────────────────────────────────

@dataclass
class _WriteOp:
    sql: str
    params: tuple = field(default_factory=tuple)


class SQLiteWriteQueue:
    """Dedicated writer thread that serializes all writes to SQLite.

    Scraper and analyzer threads call enqueue() and never hold a SQLite write
    lock themselves. The writer batches commits for efficiency.

    Usage:
        writer = SQLiteWriteQueue(db_path)
        writer.enqueue(UPSERT_JOB_SQL, (url, site, title, ...))
        ...
        writer.close()  # flushes and joins the thread
    """

    def __init__(self, db_path: Path, batch_size: int = 50):
        self._q: queue.Queue[_WriteOp | None] = queue.Queue()
        self._db_path = db_path
        self._batch_size = batch_size
        self._new_count = 0      # rows affected by INSERT (not ON CONFLICT update)
        self._update_count = 0   # rows affected by ON CONFLICT update
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._worker, daemon=True, name="db-writer")
        self._thread.start()

    def enqueue(self, sql: str, params: tuple = ()) -> None:
        """Non-blocking. Returns immediately; write happens in the background."""
        self._q.put(_WriteOp(sql=sql, params=params))

    def flush(self) -> None:
        """Block until all currently-queued writes are committed."""
        self._q.join()

    def close(self) -> None:
        """Flush remaining writes and stop the writer thread."""
        self._q.put(None)  # sentinel
        self._thread.join()

    @property
    def new_count(self) -> int:
        with self._lock:
            return self._new_count

    @property
    def update_count(self) -> int:
        with self._lock:
            return self._update_count

    def _worker(self) -> None:
        conn = _open_conn(self._db_path)
        batch: list[_WriteOp] = []

        def commit_batch() -> None:
            if not batch:
                return
            try:
                for op in batch:
                    cur = conn.execute(op.sql, op.params)
                    # Heuristic: rowcount==1 + lastrowid changed → INSERT; else UPDATE
                    if cur.lastrowid and cur.rowcount == 1:
                        with self._lock:
                            self._new_count += 1
                    elif cur.rowcount > 0:
                        with self._lock:
                            self._update_count += 1
                conn.commit()
            except Exception as e:
                log.error(f"DB write batch failed ({len(batch)} ops), rolling back: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass
            finally:
                batch.clear()

        try:
            while True:
                try:
                    op = self._q.get(timeout=0.1)
                    if op is None:          # sentinel → flush and exit
                        commit_batch()
                        self._q.task_done()
                        break
                    batch.append(op)
                    self._q.task_done()
                    if len(batch) >= self._batch_size:
                        commit_batch()
                except queue.Empty:
                    commit_batch()          # flush partial batch on idle
        finally:
            conn.close()


# ── Migration ─────────────────────────────────────────────────────────────────

def migrate_from_csv_json(
    db_path: Path,
    cfg: dict[str, Any],
    csv_path: Path | None = None,
    json_path: Path | None = None,
) -> None:
    """Import legacy output/ CSV and JSON files into the DB.

    Called automatically from main.py when the DB file does not yet exist.
    Safe to call multiple times — uses INSERT OR IGNORE throughout.
    """
    if csv_path is None and json_path is None:
        return

    conn = _open_conn(db_path)

    # Ensure active criteria exist before importing scores
    criteria_id: int | None = None
    try:
        row = conn.execute(
            "SELECT criteria_id FROM criteria WHERE is_active = 1 LIMIT 1"
        ).fetchone()
        criteria_id = row["criteria_id"] if row else None
    except sqlite3.OperationalError:
        pass

    if csv_path and csv_path.exists():
        log.info(f"Migrating jobs from {csv_path}")
        cur = conn.execute(
            "INSERT INTO scrape_runs (keywords, locations, sites, status, completed_at)"
            " VALUES (?, ?, ?, 'completed', CURRENT_TIMESTAMP)",
            ('["migrated"]', '["migrated"]', '["csv-import"]'),
        )
        run_id = cur.lastrowid
        imported = 0
        with open(csv_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                url = row.get("job_url", "").strip()
                if not url:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO jobs"
                    " (job_url, site, title, company, location, job_type, description, canonical_key, scrape_run_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        url,
                        row.get("site", ""),
                        row.get("title", ""),
                        row.get("company", ""),
                        row.get("location"),
                        row.get("job_type"),
                        row.get("description"),
                        make_canonical_key(row.get("title", ""), row.get("company", "")),
                        run_id,
                    ),
                )
                imported += 1
        conn.commit()
        log.info(f"Migrated {imported} jobs from CSV")

    if json_path and json_path.exists() and criteria_id is not None:
        log.info(f"Migrating scores from {json_path}")
        data = json.loads(json_path.read_text(encoding="utf-8"))
        model = data.get("model_name", "claude-3.5-sonnet")
        migrated = 0
        for scored_job in data.get("scored_jobs", []) + data.get("disqualified_jobs", []):
            url = scored_job.get("job_url", "").strip()
            if not url:
                continue
            job_row = conn.execute(
                "SELECT job_id FROM jobs WHERE job_url = ?", (url,)
            ).fetchone()
            if not job_row:
                continue
            is_qualified = not scored_job.get("disqualified", False)
            conn.execute(
                "INSERT OR IGNORE INTO job_scores"
                " (job_id, criteria_id, is_qualified, disqualified_reason,"
                "  score_overall, score_relevance, score_duties, score_income, reasoning, model_name)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_row["job_id"],
                    criteria_id,
                    is_qualified,
                    scored_job.get("disqualified_by") if not is_qualified else None,
                    scored_job.get("score"),
                    scored_job.get("score_relevance"),
                    scored_job.get("score_duties"),
                    scored_job.get("score_income"),
                    scored_job.get("reasoning", ""),
                    model,
                ),
            )
            migrated += 1
        conn.commit()
        log.info(f"Migrated {migrated} scores from JSON")
    elif json_path and json_path.exists() and criteria_id is None:
        log.warning("No active criteria — skipping score migration. Run --mode analyze after setup.")

    conn.close()
