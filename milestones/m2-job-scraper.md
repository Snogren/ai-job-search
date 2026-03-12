# Milestone 2 — Job Scraper

## Tasks

- [x] Write `src/scraper.py`: accepts `search_term`, `location`, `hours_old`, `site_name` as CLI args or function params
- [x] Deduplicate results by `(company, title)` pair — no duplicate listings
- [x] Export results to `output/jobs_raw_YYYY-MM-DD.csv`
- [x] Handle empty results gracefully (log a warning, exit 0)

## Result — 5/5 PASS (2026-03-11)

```
[PASS] CLI runs end-to-end  (19 jobs, exit 0)
[PASS] Output file has all required columns (site, title, company, location, job_type, job_url, description)
[PASS] Deduplication works  (20 raw → 19 deduped, 0 duplicate pairs in CSV)
[PASS] Empty result is safe  (nonsense query → WARNING + exit 0, no exception)
[PASS] Scrapes ≥2 boards    (indeed + zip_recruiter default; glassdoor/google available via --sites)
```

Sample output (`output/jobs_raw_2026-03-11.csv`, 19 rows):
- Full Stack Developer @ LMI [indeed]
- Sr. Full Stack Software Engineer @ Ursa Space Systems [indeed]
- Entry-Level Developer @ Green Line Digital [indeed]

## Quality Gate

All of the following must be true before handoff:

1. **CLI runs end-to-end** — `python src/scraper.py --search "python developer" --location "remote" --hours-old 72` completes without exception
2. **Output file exists** — `output/jobs_raw_YYYY-MM-DD.csv` is created with at least the columns: `site`, `title`, `company`, `location`, `job_type`, `job_url`, `description`
3. **Deduplication works** — no two rows share the same `(company, title)` pair
4. **Empty result is safe** — a search term that returns 0 results exits cleanly with a log message, not a crash
5. **Scrapes ≥2 boards** — at least Indeed and one other site return rows in a standard run

## Handoff Artifact

- `src/scraper.py` source
- Sample `output/jobs_raw_YYYY-MM-DD.csv` with ≥10 real rows (committed without secrets)
