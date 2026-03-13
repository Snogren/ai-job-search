# Milestone 6 — Feature Set Design

**Status:** Planning — dedup/canonical_key shipped (see Completed Prerequisite below)  
**Depends on:** M1–M5 complete  
**Research companion:** `m6-dedup-report.md`

---

## Overview

Five interrelated improvements that clean up the architecture and improve daily usability:

0. **Dedup / canonical_key** ✅ *SHIPPED* — cross-site alias grouping stored in `jobs.canonical_key`; aliases shown as "also on X (score)" badges in the viewer
1. **Job dismissal** — hide reviewed-and-rejected jobs without deleting them  
2. **Multiple concurrent criteria** — score the same job against several angles at once  
3. **Criteria = scoring only** — extract search params so `criteria` in config describes *what makes a good match*, not *where to look*  
4. **Model config in config.yaml** — stop using env vars for non-secret model settings

Each feature is independent enough to ship separately, but Features 3 and 4 share a config.yaml restructure that should land together.

---

## Completed Prerequisite — Dedup / canonical_key

**Shipped.** A `canonical_key TEXT` column was added to `jobs` (non-unique, additive `ALTER TABLE`). Value is computed by `make_canonical_key(title, company)` in `database.py`, which strips common seniority prefixes (`sr`, `senior`, `staff`, `principal`, `lead`, `associate`) and corporate suffixes (`inc`, `llc`, `ltd`, `corp`, `co`) before building the `lower(company)|lower(title)` key.

`UPSERT_JOB_SQL` now sets `canonical_key` on every write (9 positional params). `scraper.py` passes it at both enqueue sites. `init_db()` runs an additive migration and backfills existing rows.

`viewer.py` builds a `canonical_sites` dict (secondary query, keyed by `canonical_key`) and an `alias_groups` dict (`{canonical_key: {site: count}}`) and passes both to the template. After fetching rows (ordered best-score-first), a Python dedup loop keeps **one card per `canonical_key`** — the highest-scored representative. `jobs.html` renders compact purple badges: "also on X" (1 alias) or "N more on X" (multiple), derived from `alias_groups`.

**Design choice:** `canonical_key` is intentionally non-unique. The row-per-URL audit trail is preserved in the DB. Duplicates are **collapsed to one card** in the viewer (not silently deleted), with alias counts surfaced via badges. The false-positive merge problem (e.g., two distinct "Senior Software Engineer" openings at Google) is avoided entirely.

**Constraint to carry forward:** The `canonical_sites` secondary query in `viewer.py` currently uses `ORDER BY scored_at DESC LIMIT 1` — it fetches the most-recent score regardless of criteria. When Feature 2 (multi-criteria) lands, the query must become criteria-aware. See Feature 2 below.

---

## Feature 1 — Job Dismissal / Deactivation

### Problem
Once a user has reviewed a job and decided it's not worth applying to, it keeps reappearing on every page load. There's no way to suppress it without deleting the row, which would destroy the audit trail and cause re-scoring on the next scrape.

### DB Design — Recommended: `job_actions` table

Rejected option: `jobs.status TEXT NOT NULL DEFAULT 'active'`  
A status column on `jobs` is simple but:
- Mixes UX state (a user decision) into the raw data table
- Only supports one state per job — can't record "I applied AND later withdrew"  
- Has no timestamp or note field

**Recommended design:**

```sql
CREATE TABLE job_actions (
    action_id   INTEGER   PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER   NOT NULL,
    action_type TEXT      NOT NULL CHECK(action_type IN ('dismissed','saved','applied','archived')),
    note        TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

CREATE INDEX idx_job_actions_job        ON job_actions(job_id);
CREATE INDEX idx_job_actions_type_time  ON job_actions(action_type, created_at DESC);
```

Why `job_actions` wins:
- One job can have multiple action events (dismissed → reconsidered → applied)
- Adds a `note` field for free ("waiting on referral", "salary too low")
- The latest action per job determines the effective state — use a window query or a simple `max(action_id)` subquery
- Extensible: future actions (interview_scheduled, offer_received) drop in without schema changes
- No column changes to the `jobs` table — zero migration risk

**Effective state query:**

```sql
-- Latest action per job
SELECT j.*, COALESCE(a.action_type, 'active') AS status
FROM jobs j
LEFT JOIN job_actions a
       ON a.action_id = (
           SELECT action_id FROM job_actions
           WHERE job_id = j.job_id
           ORDER BY created_at DESC LIMIT 1
       )
```

**DB migration strategy:**  
No changes to existing tables. `CREATE TABLE IF NOT EXISTS job_actions ...` is fully additive. Safe to run on any existing database without data loss or column backfills.

### UX Design (FastAPI viewer)

- Add a **Dismiss** button per row in the job list (inline, small, muted color)  
- New `POST /job/{job_id}/action` endpoint: accepts `{"action_type": "dismissed", "note": ""}`  
- Default view filters out jobs where latest action is `dismissed`  
- Add a `?show_dismissed=1` query param (or checkbox toggle) to reveal dismissed jobs  
- No confirmation dialog needed — personal tool, low stakes — but a visual indicator (strikethrough or greyed row) confirms the action  
- Undismiss: clicking "Restore" sends `POST /job/{job_id}/action` with `action_type = "active"` (add to the CHECK constraint or use a sentinel)

**Alternative UX:** Use a `DELETE /job/{job_id}/dismiss` that just sets a status flag. Simpler to implement but loses extensibility. Not recommended.

### Canonical Aliases and Dismissal

Since `canonical_key` is non-unique, the same real-world job may appear as multiple rows (one per site). The viewer already surfaces these as "also on X" badges.

**Per-job-id dismiss (recommended default):** The `job_actions` table is keyed by `job_id`. Dismissing one URL does not auto-dismiss its aliases. This is correct: the user may have seen the job on Indeed but want to apply via LinkedIn. Independently tracking each listing preserves that flexibility.

**Stretch: Dismiss by canonical_key** — for a future iteration, add a `POST /jobs/dismiss-by-canonical/{canonical_key}` endpoint that inserts a `dismissed` action for every `job_id` sharing that `canonical_key`. This is a one-liner bulk-insert. Because `canonical_key` is URL-safe (lowercase letters, `|` separator), it can be used directly as a path segment. Not needed for the initial implementation but costs nothing to note here.

### Open Questions
- Should "dismissed" persist across re-scrapes? **Yes** — the DB key is `job_id` which survives re-scrapes (URL-based dedup). If the job is re-fetched, it gets `refreshed_at` updated but keeps its actions.
- What if a dismissed job comes back with new data (salary added)? No auto-un-dismiss. User chooses when to review dismissed jobs via the `?show_dismissed` toggle.

---

## Feature 2 — Multiple Concurrent Criteria Sets

### Problem
`is_active = 1` is a single-boolean toggle that forces only one criteria to be "active" at a time. The user wants to score each job through multiple lenses simultaneously (e.g., "am I qualified?" plus "is this a good fit?").

### DB Design

The `UNIQUE(job_id, criteria_id)` in `job_scores` already supports multiple scores per job — this is correct and stays unchanged.

The problem is the concept of "active" criteria:

**Rejected option:** Keep `is_active` on criteria, allow multiple rows with `is_active = 1`.  
The current code has `UPDATE criteria SET is_active = 0` (deactivates all) before activating the new one. Flipping to multi-active just means removing that bulk-deactivate — but it makes "which criteria is shown in the default view?" ambiguous.

**Recommended: Split `is_active` into two separate concepts:**

```sql
ALTER TABLE criteria ADD COLUMN is_enabled  BOOLEAN NOT NULL DEFAULT 0;
-- is_active:   controls whether new scrapes are auto-scored against this criteria
-- is_enabled:  controls whether this criteria participates in ongoing scoring
-- is_default:  one row marked as the default for the viewer's primary sort
```

Actually simpler — replace `is_active` with a bitmask-friendly `flags`:

**Simplest clean design:**

```sql
-- Drop if possible, or treat as legacy:
-- is_active was: "the one criteria in use"

-- New columns (additive ALTER TABLE):
ALTER TABLE criteria ADD COLUMN is_enabled  BOOLEAN NOT NULL DEFAULT 0;
-- is_enabled = 1 → this criteria participates in scoring new jobs
-- (multiple criteria can be enabled simultaneously)

ALTER TABLE criteria ADD COLUMN is_default  BOOLEAN NOT NULL DEFAULT 0;
-- is_default = 1 → used for the viewer's primary-sort column and the 
--                  jobs_unscored view
-- CONSTRAINT: exactly one row should have is_default = 1 (enforced in Python)
```

Keep `is_active` as a deprecated alias for `is_default` during transition, or rename via a migration.

**Updated views:**

The current `jobs_unscored` and `jobs_qualified` views hardcode `WHERE is_active = 1`. SQLite views don't accept parameters, so these views become **limited views** that only serve the default criteria:

```sql
-- Rename existing views as _default variants:
DROP VIEW IF EXISTS jobs_unscored;
CREATE VIEW jobs_unscored AS
SELECT j.job_id, j.job_url, j.title, j.company, j.location, j.description, j.site, j.job_type
FROM jobs j
LEFT JOIN job_scores js
       ON j.job_id = js.job_id
      AND js.criteria_id = (SELECT criteria_id FROM criteria WHERE is_default = 1 LIMIT 1)
WHERE js.score_id IS NULL
ORDER BY j.refreshed_at DESC;
```

For non-default criteria, move to **parameterized Python queries** in `database.py`:

```python
def get_unscored_jobs(db_path: Path, criteria_id: int, limit: int = 2000) -> list[dict]:
    conn = _open_conn(db_path)
    rows = conn.execute("""
        SELECT j.job_id, j.job_url, j.title, j.company, j.location, j.description, j.site, j.job_type
        FROM jobs j
        LEFT JOIN job_scores js ON j.job_id = js.job_id AND js.criteria_id = ?
        WHERE js.score_id IS NULL
        ORDER BY j.refreshed_at DESC LIMIT ?
    """, (criteria_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_all_enabled_criteria(db_path: Path) -> list[int]:
    conn = _open_conn(db_path)
    rows = conn.execute("SELECT criteria_id FROM criteria WHERE is_enabled = 1").fetchall()
    conn.close()
    return [r[0] for r in rows]
```

**Pipeline change:** `run_analysis()` currently takes a single `criteria_id`. Change to accept `list[int]`:

```python
def run_analysis(db_path, cfg, criteria_ids: list[int], top_n, parallel_workers):
    for criteria_id in criteria_ids:
        _score_unscored_jobs(db_path, cfg, criteria_id, top_n, parallel_workers)
```

### UX Design (viewer)

- Add a **Criteria** dropdown filter to the top of the job list  
- Default selection: the `is_default` criteria  
- When a criteria is selected, the score columns (and score bars) reflect that criteria's scores  
- Jobs scored against multiple criteria show a small indicator tag ("also scored: startup-lens")  
- Jobs unscored for the selected criteria are grayed out or tagged "not scored"

**Criteria-aware canonical_sites (required).** The current `canonical_sites` secondary query in `viewer.py` uses `ORDER BY scored_at DESC LIMIT 1` — it returns the most-recent score regardless of which criteria produced it. When the user selects a criteria via the dropdown, both queries must be filtered by that `criteria_id`:

```python
# Updated secondary query — criteria_id comes from the selected filter
alias_rows = conn.execute(
    f"""
    SELECT j.canonical_key, j.site, j.job_url, js.score_overall
    FROM jobs j
    LEFT JOIN job_scores js ON js.score_id = (
        SELECT score_id FROM job_scores
        WHERE job_id = j.job_id
          AND criteria_id = ?
        ORDER BY scored_at DESC LIMIT 1
    )
    WHERE j.canonical_key IN ({placeholders})
    """,
    [criteria_id] + canonical_keys,
).fetchall()
```

This ensures the score shown in the "also on LinkedIn (5.2)" badge reflects the currently-selected criteria, not whichever criteria last wrote a score for that job. If `criteria_id` is `None` or not yet selected, fall back to the current behaviour (most-recent score) to preserve backward compatibility.

### Migration Strategy
`ALTER TABLE criteria ADD COLUMN is_enabled BOOLEAN NOT NULL DEFAULT 0` and `ADD COLUMN is_default BOOLEAN NOT NULL DEFAULT 0` are safe additive migrations in SQLite. After migration, run a one-time update:  
```sql
UPDATE criteria SET is_enabled = 1, is_default = 1 WHERE is_active = 1;
```

---

## Feature 3 — Separating Search Params from Criteria

### Problem
`criteria` in `config.yaml` currently contains `top_n` (a display preference) and implicitly depends on `search.term` as the thing being matched against. The file has no clean boundary between "what to scrape" and "how to score what we scrape."

### Design Principle
> **Search** = what jobs to fetch and from where.  
> **Criteria** = what makes a fetched job a good match.

`qualifiers` and `disqualifiers` in the `criteria` DB table are **LLM-evaluated rules** — they are scoring, not pre-search filters. They stay in criteria.

Location filtering is a **hybrid**: it belongs in both `search` (don't waste scrape quota on non-remote roles) and in `criteria` as a qualifier (LLM confirms the job is actually remote vs just says "remote" in the title). Both are valid and independent.

### Proposed `config.yaml` Restructure

```yaml
# ── Search ────────────────────────────────────────────────────────────────────
# What to scrape and from where.
search:
  keywords:                        # list of search terms (was: search.term scalar)
    - "qa engineer"
    - "software engineer in test"
  locations:                       # list of locations (was: search.location scalar)
    - "remote"
  hours_old: 72
  results_per_site: 25
  sites:
    - indeed
    - zip_recruiter
    - google
    - linkedin

# ── API Sources ───────────────────────────────────────────────────────────────
api_sources:
  remotive: true
  himalayas: true
  adzuna:
    app_id: ""
    app_key: ""

# ── Model ─────────────────────────────────────────────────────────────────────
# Non-secret model settings. API key stays in .env.
model:
  name: "openrouter/anthropic/claude-3.5-sonnet"   # was: CREWAI_MODEL env var
  base_url: "https://openrouter.ai/api/v1"         # was: hardcoded in get_llm_config()
  parallel_workers: 10                             # was: scoring.parallel_workers

# ── Criteria ──────────────────────────────────────────────────────────────────
# Scoring instructions only. What makes a fetched job a good match.
criteria:
  name: "senior-qa-remote-2026"    # used as the DB row name

  disqualifiers:
    - "The role or industry is manufacturing or any physical/non-software domain"
    - "The role is unrelated to software quality, testing, or engineering"

  qualifiers:
    - "The job is fully remote, or located in my preferred city"

  weights:
    relevance: 0.34
    duties: 0.33
    income: 0.33

  relevance:
    ideal: "The job title and posting exactly match my target role and domain."
    worst: "The job is completely unrelated to my target role or field."
  duties:
    ideal: "The responsibilities match exactly what I want to spend my time doing."
    worst: "The duties are entirely different from what I'm looking for."
  income:
    ideal: "Salary is at or above my target, stated or strongly implied."
    worst: "Salary is well below my minimum, or the role is short-term contract."

# ── Display ───────────────────────────────────────────────────────────────────
# Report and viewer display settings.
display:
  top_n: 10                        # was: criteria.top_n (not a scoring concept)
```

### What Changes in Code

| Location | Change |
|----------|--------|
| `config.py` | `get_criteria_weights()`, `get_disqualifiers()`, `get_qualifiers()` read from `config["criteria"]` — unchanged |
| `config.py` | Add `get_model_config(cfg)` → returns `{"name": ..., "base_url": ..., "workers": ...}` |
| `config.py` | `top_n` reads from `config["display"]["top_n"]` instead of `config["criteria"]["top_n"]` |
| `scraper.py` | `search.term` → `search.keywords[0]` or list; `search.location` → `search.locations[0]` or list |
| `pipeline.py` | `get_llm_config()` reads from `cfg["model"]` instead of env vars |
| `main.py` | `criteria_cfg.get("top_n")` → `cfg.get("display", {}).get("top_n")` |
| `config.example.yaml` | Full restructure as shown above |

**Note:** Feature 3 does not touch `UPSERT_JOB_SQL` or `make_canonical_key`. The 9-param signature introduced by the dedup work is unaffected.

### Backward Compatibility Note
`config.py` should accept both the old scalar `search.term` and new list `search.keywords` — the `_get_keywords()` function in `scraper.py` already handles this. Apply the same pattern to `locations`.

---

## Feature 4 — Extracting AI/Model Setup to Config

### Problem
`CREWAI_MODEL` is not a secret — it's a model name string. It lives in `.env` because it was convenient, but it belongs in `config.yaml` alongside other non-secret preferences. The hardcoded `base_url = "https://openrouter.ai/api/v1"` in `pipeline.py` is similarly wrong.

### Security Boundary
**Stays in `.env`:** `OPENROUTER_API_KEY` (secret).  
**Moves to `config.yaml`:** `model.name`, `model.base_url`, `model.parallel_workers`.

This is consistent with industry practice: Rasa, LangChain, AutoGen, and similar frameworks all put model names and endpoints in config files, with API keys in env.

### DB Changes — Provenance

`job_scores.model_name` already captures the model used to produce each score. This is the correct level of provenance — you can see which model scored which job.

**No change needed to the schema.** Recording `model_name` in the score row is sufficient. We don't need a `model_config_hash` because:
1. Model name alone is the meaningful dimension ("claude-3.5-sonnet" vs "gpt-4o")
2. The criteria hash already captures the prompt content; the model name captures the engine
3. If the same job was re-scored with a different model, that's a new score row (if the `UNIQUE(job_id, criteria_id)` constraint is relaxed — currently it isn't, which is a minor tension)

**Tension:** `UNIQUE(job_id, criteria_id)` means re-scoring with a different model will try to insert a duplicate and fail silently (INSERT OR IGNORE). If multi-model scoring is important, the unique key should become `(job_id, criteria_id, model_name)`. Else, leave it as-is — re-scoring updates via the conflict.

**Recommendation:** Keep `UNIQUE(job_id, criteria_id)` and document that changing model + re-running `--mode analyze` re-scores jobs with the new model (replacing old scores). If the user needs to compare model outputs, add `(job_id, criteria_id, model_name)` as the unique key in a future milestone.

### Code Changes

```python
# src/config.py — new function
def get_model_config(cfg: dict) -> dict:
    model = cfg.get("model", {})
    return {
        "name": model.get("name", os.getenv("CREWAI_MODEL", "openrouter/anthropic/claude-3.5-sonnet")),
        "base_url": model.get("base_url", "https://openrouter.ai/api/v1"),
        "parallel_workers": model.get("parallel_workers", 10),
    }

# src/pipeline.py — update get_llm_config()
def get_llm_config(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    mc = get_model_config(cfg)
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        log.error("OPENROUTER_API_KEY not set in .env")
        sys.exit(1)
    return {"model": mc["name"], "api_key": api_key, "base_url": mc["base_url"]}
```

---

## Implementation Order

0. **Dedup / canonical_key** ✅ *DONE* — `jobs.canonical_key`, `make_canonical_key`, UPSERT now 9 params, viewer badges showing. Existing DB backfilled.
1. **Feature 4 + Feature 3 together** — config.yaml restructure is one coherent change; both touch the same files. Low risk, no DB changes. Ship first.  
2. **Feature 1 (Job Dismissal)** — pure addition: new table, new endpoint, new button. Zero risk to existing data. Ship second.  
3. **Feature 2 (Multi-Criteria)** — requires schema migration (ALTER TABLE), view updates, and pipeline logic changes. When implementing, must also update `viewer.py`'s `canonical_sites` query to accept a `criteria_id` parameter. Ship last.

## Dependencies Between Features

- Feature 3 and 4 share a config.yaml rewrite — they must land in the same commit or be carefully sequenced.
- Feature 2 depends on Feature 3 being done first (so `get_all_enabled_criteria()` reads from the clean config structure).
- Feature 1 has no dependencies on any other feature.
- **Feature 2 has a dependency on the completed dedup work:** the `canonical_sites` secondary query in `viewer.py` must be updated to accept a `criteria_id` parameter when the criteria dropdown is implemented. The current query (criteria-unaware, `ORDER BY scored_at DESC`) is correct until Feature 2 ships — at that point it becomes inconsistent.

## Risk Flags

| Risk | Severity | Mitigation |
|------|----------|------------|
| `ALTER TABLE` on existing DB silently creates `is_enabled = 0` for all rows | Medium | Run a post-migration UPDATE to set `is_enabled = 1` for the previously-active criteria |
| Viewer SQL joins `job_actions` on every page load — could be slow with many jobs | Low | The `idx_job_actions_job` index makes this O(1) per job |
| `UNIQUE(job_id, criteria_id)` silently dropped scores when model changes | Medium | Document clearly; CLI `--reanalyze` flag can force re-scoring |
| `search.term` scalar → `search.keywords` list is a breaking config change | Low | Backward-compat shim in `_get_keywords()` already handles both |
| `criteria.is_default` enforcement (only one row) is done in Python, not DB | Low | A CHECK constraint or trigger could enforce it, but Python validation at startup is sufficient for a personal tool |
