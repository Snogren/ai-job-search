# Job Sites Research Report
**Date:** March 12, 2026  
**Purpose:** Evaluate additional job sites to expand the ai-job-search pipeline  
**Scope:** QA Lead / SDET / Quality Engineer, remote positions

---

## Executive Summary — Top 5 Recommended Additions

| # | Site | Rationale |
|---|------|-----------|
| 1 | **Google Jobs (JobSpy)** | Zero-code addition; aggregates listings from hundreds of company career pages and boards — highest coverage gain per effort |
| 2 | **LinkedIn (JobSpy)** | Highest volume of QA/SDET roles; already in library, needs proxy to avoid rate limits past page 10 |
| 3 | **Remotive API** | Free, no-auth REST API returning remote tech jobs filtered by category; requires 3–5 lines of code |
| 4 | **Adzuna API** | Official REST API with generous free tier; covers US + UK + 16 countries; only requires registering for an app key |
| 5 | **Himalayas API** | No-auth public JSON API dedicated to remote jobs; returns salary data more often than most boards |

---

## Category 1: JobSpy Built-ins (Already Supported, Not Yet Configured)

These require only adding a string to `config.yaml → search.sites`. No code changes needed.

| Site | Job Volume (QA/SDET) | QA/Tech Relevance | Rate Limit Behavior | ToS Risk | Recommendation |
|------|---------------------|-------------------|---------------------|----------|----------------|
| **LinkedIn** | Very High | Excellent — large recruiter presence | Blocks around page 10 per IP; proxies needed for high volume | High (ToS prohibits scraping) | **Add with caution** — use `results_per_site: 25` to stay under the rate limit threshold without proxies |
| **Glassdoor** | Medium | Good — employer reviews + jobs | Moderate rate limiting; less reliable than Indeed | High (ToS prohibits scraping) | **Add** — good deduplication complement to Indeed; keep results low |
| **Google Jobs** | Very High | Excellent — aggregates from company ATS, LinkedIn, Indeed, and niche boards | Minimal rate limiting | Low (scrapes public search results) | **Add immediately** — single highest-leverage change; surfaces jobs from boards not scraped directly |

> **Note on JobSpy sites added in newer versions:** The library also now supports `bayt` (Middle East), `naukri` (India), and `bdjobs` (Bangladesh). These are not relevant for remote US QA roles.

> **Usage tip:** LinkedIn has a separate `linkedin_fetch_description=True` option that fetches full job descriptions but is significantly slower. For the CrewAI scoring pipeline, full descriptions are critical — consider enabling it but reducing `results_per_site` to compensate.

---

## Category 2: Public APIs (No Scraping, Preferred for Reliability)

These require writing a custom adapter (a new function in `scraper.py` or a separate module) that fetches from the API, normalizes to the 7-column schema, and writes to CSV.

| Site | API Type | Auth Required | Free Tier Limits | QA Job Coverage | Integration Effort | Recommendation |
|------|----------|---------------|------------------|-----------------|--------------------|----------------|
| **Adzuna** | REST (JSON) | App ID + App Key (free registration) | Generous; ~250 req/month documented, practical daily use unrestricted | Good — US + UK large volume; QA roles present | Low — standard REST, paginated, `q=qa+lead&what=qa+sdet` | **Add** |
| **Remotive** | REST (JSON) | None — fully public | No documented rate limit; 24hr posting delay | Good — remote-only tech jobs, `software-dev` and `qa` categories | Very Low — single GET, no auth | **Add first** |
| **RemoteOK** | REST (JSON) | None — fully public | No documented limit; 24hr delay on free; requires attribution link | Medium — fewer QA-specific listings, more general dev | Very Low — single GET to `remoteok.com/api` | **Add** |
| **Himalayas** | REST (JSON) | None — fully public | Max **20 jobs per request** (reduced March 2025); paginate with offset | Good — curated remote tech jobs, salary data included | Low — paginate to desired count | **Add** |
| **Arbeitnow** | REST (JSON) | None — fully public | No documented limit | Low-Medium — European/global remote focus, fewer US QA roles | Very Low — single paginated GET | **Consider** (good for non-US remote roles) |
| **Jobicy** | REST (JSON) | None — fully public | No documented limit | Low-Medium — smaller board, general remote jobs | Very Low | **Consider** |
| **The Muse** | REST (JSON) | None for jobs endpoint | 500 req/day (generous) | Low — focuses on company culture profiles, fewer QA postings | Low | **Skip** — low QA signal |
| **jsearch (RapidAPI)** | REST (JSON) | RapidAPI key (freemium) | 200 req/month free tier | High — aggregates Indeed, LinkedIn, Glassdoor | Medium — RapidAPI dependency; limited free quota | **Skip** — same boards covered cheaper via JobSpy |
| **Reed.co.uk** | REST (JSON) | API key (free registration) | ~100 requests/hour | Low for US-based QA roles — UK-centric | Low | **Skip** unless targeting UK remote |

### API Endpoint Reference (for implementation)

```python
# Remotive — no auth, no setup
GET https://remotive.com/api/remote-jobs?category=software-dev&search=qa+lead&limit=100

# RemoteOK — no auth, requires User-Agent header
GET https://remoteok.com/api?tag=qa

# Himalayas — no auth, paginated
GET https://himalayas.app/jobs/api?limit=20&offset=0&q=qa+lead

# Adzuna — free app_id + app_key from developer.adzuna.com
GET https://api.adzuna.com/v1/api/jobs/us/search/1?app_id=ID&app_key=KEY&what=qa+lead&where=remote&results_per_page=50

# Arbeitnow — no auth
GET https://www.arbeitnow.com/api/job-board-api?remote=true&search=qa

# Jobicy — no auth
GET https://jobicy.com/api/v2/remote-jobs?count=50&keyword=qa+lead
```

---

## Category 3: Niche & Specialty Sites

| Site | Focus | Access Method | QA/SDET Coverage | Notes |
|------|-------|---------------|------------------|-------|
| **We Work Remotely** | Remote-only, all tech | HTML scraping (no API) | Low — mostly dev/design, few QA | Simple HTML; ToS prohibits scraping; low QA yield |
| **Dice.com** | US tech jobs | No public API; third-party scrapers only | **High** — tech-specialist board, excellent QA/SDET concentration | Had a public API (retired); now requires proprietary access; scraping feasible but fragile |
| **Wellfound (AngelList Talent)** | Startup jobs | No public API; very bot-resistant | Medium — startup QA roles exist | Login-walled; difficult to scrape reliably |
| **Builtin.com** | Tech companies by city | HTML scraping | Medium | City-focused (not remote-primary); moderate QA listings |
| **FlexJobs** | Remote/flexible roles | Paywalled — subscription required | Medium | Paid membership required to view listings; cannot scrape freely |
| **Authentic Jobs** | Creative/tech | HTML scraping | Low | Small board; minimal QA activity |
| **Stack Overflow Jobs** | Developer-focused | **Discontinued (2022)** | N/A | No longer operational |
| **Remote.co** | Remote-only | HTML scraping | Low | Small board; broader than QA-specific |
| **Hacker News (Who's Hiring)** | Startup hiring threads | HN Algolia API (free) | Medium | Monthly thread; QA roles occur but infrequently; unstructured text requires extra parsing |

---

## Integration Priority Matrix

| Site | Value/Coverage (1–5) | Integration Effort (1–5, lower=easier) | ToS/Legal Risk | Recommendation |
|------|---------------------|----------------------------------------|----------------|----------------|
| Google Jobs (JobSpy) | 5 | 1 | Low | **Add Now** |
| LinkedIn (JobSpy) | 5 | 2 | High | **Add Now** (low `results_per_site`) |
| Remotive API | 3 | 1 | Low | **Add Now** |
| Himalayas API | 3 | 1 | Low | **Add Now** |
| Adzuna API | 4 | 2 | Low | **Add Now** |
| Glassdoor (JobSpy) | 3 | 1 | High | **Consider** (ToS) |
| RemoteOK API | 2 | 1 | Low | **Consider** |
| Arbeitnow API | 2 | 1 | Low | **Consider** (EU/global roles) |
| Dice.com | 4 | 4 | Medium | **Consider** — high QA value but brittle scraper |
| Jobicy API | 2 | 1 | Low | **Consider** — minor extra coverage |
| Hacker News | 2 | 3 | Low | **Consider** — requires custom parser |
| We Work Remotely | 2 | 3 | Medium | **Skip** — low QA yield for effort |
| Wellfound | 2 | 5 | Medium | **Skip** — login wall, bot detection |
| FlexJobs | 3 | 5 | High | **Skip** — paywalled |
| The Muse | 1 | 2 | Low | **Skip** — low QA signal |
| Reed.co.uk | 2 | 2 | Low | **Skip** — UK-centric |
| jsearch (RapidAPI) | 3 | 2 | Low | **Skip** — paid quota, redundant coverage |

---

## Implementation Notes

### Approach A: Extend JobSpy config (zero code, immediate)

Add to `config.yaml`:
```yaml
search:
  sites:
    - indeed          # currently active
    - zip_recruiter   # currently active
    - google          # ADD: highest coverage gain, low risk
    - linkedin        # ADD: high volume, keep results_per_site ≤ 25
    # - glassdoor     # optional: ToS risk, moderate value
```

For LinkedIn descriptions, add `linkedin_fetch_description: True` as a config flag (requires scraper.py update to pass it through to `scrape_jobs()`).

### Approach B: Lightweight API adapters (low effort, no ToS risk)

Add a new `fetch_api_jobs()` function to `scraper.py` (or a separate `src/api_sources.py`) that queries free APIs and normalizes results to the existing 7-column schema before deduplication.

**Minimal adapter pattern:**
```python
import requests

def fetch_remotive(search_term: str, limit: int = 100) -> list[dict]:
    resp = requests.get(
        "https://remotive.com/api/remote-jobs",
        params={"search": search_term, "limit": limit},
        timeout=15
    )
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    return [
        {
            "site": "remotive",
            "title": j.get("title", ""),
            "company": j.get("company_name", ""),
            "location": j.get("candidate_required_location", "remote"),
            "job_type": j.get("job_type", ""),
            "job_url": j.get("url", ""),
            "description": j.get("description", ""),
        }
        for j in jobs
    ]
```

The same pattern applies to Himalayas, RemoteOK, and Adzuna — each needs a small normalization mapping to collapse their field names to the 7 required columns. Results can be merged with the JobSpy output before deduplication runs.

### Deduplication note

The existing `(company, title)` deduplication key in `scraper.py` will correctly handle cross-source duplicates once API results are merged with JobSpy results before the dedup step.

---

## Summary of Recommended Additions

**Phase 1 (< 1 hour, no new dependencies):**
1. Add `google` and `linkedin` to `config.yaml` sites list

**Phase 2 (1–2 hours, add `requests` calls):**
2. Add Remotive API adapter — no auth, no setup
3. Add Himalayas API adapter — no auth, no setup

**Phase 3 (30 min setup + implementation):**
4. Register for Adzuna API key at developer.adzuna.com → add Adzuna adapter
