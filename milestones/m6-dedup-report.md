# Deduplication Research Report

**Status:** Research Only — no code changes yet  
**Related milestone:** `m6-feature-set.md`

---

## Summary

The codebase has **two deduplication layers** that are inconsistently applied. The in-memory layer works correctly on the legacy CSV path but is bypassed in the DB-backed pipeline. The result: **cross-site duplicate jobs regularly enter the database**, and the current in-memory key has inherent correctness tradeoffs worth understanding before deciding whether to tighten or relax it.

---

## Current State: Two Independent Layers

### Layer 1 — In-memory: `(company, title)` key

**Where:** `deduplicate_rows()` in `src/scraper.py`  
**Key:** `lower(company) + "|" + lower(title)` (exact string match, case-insensitive)  
**When it runs:** Only in `run_scrape()` — the legacy CSV path  
**When it does NOT run:** `scrape_one_combo()` / `ingest_parallel_keywords()` — the DB-backed path

```python
def deduplicate_rows(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    for row in rows:
        key = row.get("company", "").lower().strip() + "|" + row.get("title", "").lower().strip()
        ...
```

**Coverage:** Within a single `run_scrape()` call — handles the case where Indeed and LinkedIn return the same job in the same batch. Does nothing for separate calls (parallel threads, subsequent runs, API sources).

### Layer 2 — Database: `job_url` UNIQUE constraint

**Where:** `UPSERT_JOB_SQL` — `INSERT ... ON CONFLICT(job_url) DO UPDATE SET ...`  
**Key:** `job_url` (exact URL match)  
**What it does on collision:** Updates `title`, `refreshed_at`, `scrape_run_id` for the matching row  
**What it doesn't do:** Detect the same real-world job appearing at different URLs on different platforms

---

## The Gap: DB-Backed Path Bypasses Layer 1

`ingest_parallel_keywords()` dispatches one thread per `(keyword, location, site)` combo. Each thread calls `scrape_one_combo()`, which:
1. Calls `scrape_jobs()` for one site only
2. Iterates the rows and enqueues each URL directly to the `SQLiteWriteQueue`
3. **Does not call `deduplicate_rows()`**

Then, after the parallel scrape, `fetch_remotive()` and `fetch_himalayas()` are called sequentially and also **enqueue without dedup**.

The result:

| Scenario | Expected | Actual |
|----------|----------|--------|
| Same URL scraped twice from Indeed | 1 DB row | ✅ 1 row (URL UNIQUE) |
| Same job on Indeed + LinkedIn (different URLs) | 1 DB row | ❌ 2 rows |
| Same job on LinkedIn + Remotive (different URLs) | 1 DB row | ❌ 2 rows |
| Same job reposted with new URL (same site) | User sees it fresh | ❌ 2 rows |

---

## The `(company, title)` Key: Correctness Analysis

### False Negatives — Same job missed by the key

| Root cause | Example |
|------------|---------|
| Title abbreviation | "Sr. Software Engineer" vs "Senior Software Engineer" |
| Title normalization differences | "Software Engineer III" vs "Software Engineer - Senior" |
| Different company name formats | "Google" vs "Google LLC" vs "Alphabet" |
| Staffing agency re-listings | Agency re-posts under their company name, not the actual employer |

Industry data (Salesforce dedup, DataLadder) suggests 10–15% of duplicates slip past exact title matching due to normalization gaps alone.

### False Positives — Different jobs incorrectly merged

| Root cause | Example |
|------------|---------|
| Same company, same-named role, different teams | "Senior Software Engineer" (Platform) vs "Senior Software Engineer" (Mobile) at the same company |
| Generic role titles at large companies | "Software Engineer" at Google — hundreds of open headcount, all with the same title |
| Consecutive postings | Last quarter's posting expired; new posting with same title is a genuinely new opening |

For a personal job tool with ~50–200 jobs per run, false positives are the bigger practical concern. Merging two distinct openings at the same company is worse than seeing the same posting twice.

---

## What Industry Does

From research across job data vendors (JobsPikr, JobSync, RecruiterBox):

1. **URL-based dedup** (per-site, exact) — the universal baseline. Every major job aggregator does this. This is what the DB layer does, and it's correct.

2. **Composite key dedup** (title + company + location) — used for cross-site deduplication by most industry tools. JobSync: "boards deduplicate listings by checking key fields like job title and location, or a unique job ID." Weakness: still misses title abbreviation variants.

3. **Fuzzy matching** (edit distance, phonetic, token sort) — used for higher-quality dedup at scale. Requires choosing a threshold; stricter = more false negatives, looser = more false positives. Overkill for a personal tool at <5k jobs.

4. **Canonical job ID** — most authoritative dedup. Some ATS systems emit a canonical job ID that aggregators can use. JobSpy does not expose this; sites don't share IDs across platforms.

5. **Keep duplicates, flag them** — the pragmatic approach for aggregators that can't reliably deduplicate. Show the same job twice with different site badges; let the user decide. This actually has merit here: seeing "also on LinkedIn" is useful signal.

---

## Recommended Fix: Two-Phase Dedup

### Phase 1 — Fix the immediate gap (low risk, drop-in)

**Re-enable `deduplicate_rows()` in the DB-backed path** by buffering all rows from all parallel threads before writing to the DB.

Change `ingest_parallel_keywords()` to collect results first:

```python
# In ingest_parallel_keywords():
all_rows = []
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    futures = {pool.submit(scrape_one_combo_return_rows, kw, loc, site, cfg): ...}
    for future in ...:
        all_rows.extend(future.result())

# Add API source rows
for keyword in keywords:
    if api_cfg.get("remotive"):
        all_rows.extend(fetch_remotive(keyword))
    ...

# Deduplicate combined set before writing
deduped = deduplicate_rows(all_rows)
for row in deduped:
    writer.enqueue(UPSERT_JOB_SQL, (...))
```

This requires splitting `scrape_one_combo()` into a version that returns rows instead of writing directly — a clean change.

**Tradeoff:** Buffering all rows before writing means the DB write is deferred and slightly less "streaming." For ~500–2000 rows, this is negligible.

### Phase 2 — Improve the key (optional, medium risk)

Add light normalization to the title in the dedup key:

```python
import re

_TITLE_PREFIXES = re.compile(r"\b(sr|jr|senior|staff|principal|lead|associate)\b\.?\s*", re.I)
_COMPANY_SUFFIXES = re.compile(r"\b(inc|llc|ltd|corp|co)\.?\s*$", re.I)

def _normalize_title(title: str) -> str:
    t = _TITLE_PREFIXES.sub("", title.lower()).strip()
    return re.sub(r"\s+", " ", t)

def _normalize_company(company: str) -> str:
    c = _COMPANY_SUFFIXES.sub("", company.lower()).strip()
    return re.sub(r"\s+", " ", c)

def deduplicate_rows(rows):
    seen = set()
    for row in rows:
        key = _normalize_company(row.get("company","")) + "|" + _normalize_title(row.get("title",""))
        ...
```

**Risk:** The false-positive problem still exists for large companies with many identical-title roles. Normalize carefully and test on real scraped data before enabling. If uncertain, add a `--strict-dedup` flag to opt in.

### Phase 2 Alternative — Retain duplicates, surface them in the viewer

Instead of merging duplicates, **add a `canonical_key` column** (non-unique) and display a badge in the viewer:

```sql
-- Additive migration:
ALTER TABLE jobs ADD COLUMN canonical_key TEXT;
CREATE INDEX idx_jobs_canonical ON jobs(canonical_key);

-- Populate at insertion time:
-- canonical_key = lower(company) + "|" + lower(title) (normalized)
```

Then in the viewer, group jobs by `canonical_key` and show:
> "SDET @ Acme Corp — also on LinkedIn (5.7), Indeed (5.3)"

This approach:
- Preserves all job URL records for the audit trail
- Surfaces site coverage as useful signal ("applied via LinkedIn, saw it on Indeed too")
- Eliminates the false-positive merge problem entirely
- Does not break any existing queries

This is the **recommended long-term design**.

---

## DB Architecture Options Summary

| Option | Dedup guarantee | False positive risk | Migration cost | Complexity |
|--------|----------------|---------------------|----------------|------------|
| Status quo (URL only) | Per-site exact | None | None | Low |
| Fix Phase 1 (buffer+dedup in Python) | Cross-site (company+title exact) | Medium (same title at big company) | None | Low |
| Phase 2 (normalize title/company) | Cross-site (title/company normalized) | Low-Medium | None | Medium |
| `canonical_key` non-unique column + grouping | Display grouping only | N/A | Additive `ALTER TABLE` | Medium |
| `UNIQUE(canonical_key)` constraint | Merge by normalized key | High for large employers | Requires data migration | High |

**Recommendation:** Do Phase 1 first (fix the gap — it's a bug). Then add `canonical_key` as a non-unique column and use it for grouping in the viewer. Do not add a UNIQUE constraint on `canonical_key` — the false positive risk is not worth it for a personal tool.

---

## Open Questions

1. **Remotive/Himalayas cross-dedup**: A job from Himalayas and LinkedIn with the same title+company — should both appear? Currently yes (different URLs). With Phase 1, no (first URL wins in dedup). Is this desirable? The Himalayas URL often links to the company's own ATS, which is better than the LinkedIn apply-easy flow. Keeping both may be preferable.

2. **Repost freshness**: A job reposted with a new URL at the same company is genuinely "new" for freshness purposes but redundant for review purposes. The current behavior (show it again as unscored) is arguably correct — the user may have dismissed the old posting prematurely.

3. **`scrape_one_combo` return-value refactor**: Phase 1 requires changing `scrape_one_combo()` from "enqueue directly" to "return rows." This changes the function contract. The parallel writer optimization (streaming to DB as results arrive) would be partially lost, though batch writes of 500 rows are still fast.
