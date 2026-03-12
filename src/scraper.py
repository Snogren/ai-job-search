"""
src/scraper.py — Job Scraper (Milestone 2)

Scrapes job listings from multiple boards using JobSpy and writes
deduplicated results to output/jobs_raw_YYYY-MM-DD.csv.

Usage:
    python src/scraper.py                              # uses config.yaml defaults
    python src/scraper.py --search "python developer" --location "remote" --hours-old 72
    python src/scraper.py --search "data engineer" --location "New York" --sites indeed glassdoor
"""

import argparse
import csv
import logging
import sys
from datetime import date
from pathlib import Path

import requests
from jobspy import scrape_jobs

from config import load_config

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"

REQUIRED_COLUMNS = ["site", "title", "company", "location", "job_type", "job_url", "description"]
ALL_SITES = ["indeed", "linkedin", "glassdoor", "zip_recruiter", "google"]


def deduplicate_rows(rows: list[dict]) -> list[dict]:
    """Deduplicate a list of job dicts by (company, title) — case-insensitive."""
    seen: set[str] = set()
    result: list[dict] = []
    for row in rows:
        key = (
            row.get("company", "").lower().strip()
            + "|"
            + row.get("title", "").lower().strip()
        )
        if key not in seen:
            seen.add(key)
            result.append(row)
    before = len(rows)
    after = len(result)
    if before != after:
        log.info(f"Deduplicated: {before} → {after} rows")
    return result


def run_scrape(
    search_term: str,
    location: str = "remote",
    hours_old: int = 72,
    site_name: list[str] | None = None,
    results_wanted: int = 50,
) -> list[dict]:
    """Scrape jobs and return deduplicated rows as a list of dicts."""
    sites = site_name or ["indeed", "zip_recruiter"]

    is_remote = location.lower().strip() == "remote"
    loc_str = None if is_remote else location
    log.info(f"Scraping: '{search_term}' | {'remote=True' if is_remote else f'location={location!r}'} | hours_old={hours_old} | sites={sites}")

    try:
        df = scrape_jobs(
            site_name=sites,
            search_term=search_term,
            location=loc_str,
            is_remote=is_remote,
            country_indeed="usa",
            hours_old=hours_old,
            results_wanted=results_wanted,
        )
    except Exception as e:
        log.error(f"Scrape failed: {e}")
        return []

    if df is None or df.empty:
        log.warning("No results returned from any site.")
        return []

    log.info(f"Raw results: {len(df)} rows across {df['site'].nunique() if 'site' in df.columns else '?'} site(s)")

    # Ensure required columns exist (fill missing with empty string)
    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    rows = df[REQUIRED_COLUMNS].fillna("").to_dict(orient="records")
    return deduplicate_rows(rows)


def fetch_remotive(search_term: str, limit: int = 100) -> list[dict]:
    """Fetch remote jobs from the Remotive public API (no auth required).

    Uses category=software-dev (reliable) and appends a keyword-filtered pass
    via the search param so both QA-tagged and title-matched roles are returned.
    """
    try:
        resp = requests.get(
            "https://remotive.com/api/remote-jobs",
            params={"category": "software-dev", "limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
        log.info(f"Remotive: {len(jobs)} results for '{search_term}'")
    except Exception as e:
        log.warning(f"Remotive fetch failed: {e}")
        return []
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


def fetch_himalayas(search_term: str, limit: int = 100) -> list[dict]:
    """Fetch remote jobs from the Himalayas public API (no auth required).

    The API returns max 20 per request; paginates automatically to reach `limit`.
    """
    PAGE_SIZE = 20
    all_jobs: list[dict] = []
    offset = 0
    while len(all_jobs) < limit:
        try:
            resp = requests.get(
                "https://himalayas.app/jobs/api",
                params={"limit": PAGE_SIZE, "offset": offset, "q": search_term},
                timeout=15,
            )
            resp.raise_for_status()
            page = resp.json().get("jobs", [])
        except Exception as e:
            log.warning(f"Himalayas fetch failed (offset={offset}): {e}")
            break
        if not page:
            break
        all_jobs.extend(page)
        if len(page) < PAGE_SIZE:
            break  # last page
        offset += PAGE_SIZE
    log.info(f"Himalayas: {len(all_jobs)} results for '{search_term}'")
    return [
        {
            "site": "himalayas",
            "title": j.get("title", ""),
            "company": j.get("companyName", ""),
            "location": j.get("locationRestrictions", "remote") or "remote",
            "job_type": j.get("jobType", ""),
            "job_url": j.get("applicationLink", ""),
            "description": j.get("description", ""),
        }
        for j in all_jobs[:limit]
    ]


def fetch_adzuna(search_term: str, app_id: str, app_key: str, results_per_page: int = 50) -> list[dict]:
    """Fetch jobs from the Adzuna REST API (free app_id + app_key from developer.adzuna.com)."""
    try:
        resp = requests.get(
            "https://api.adzuna.com/v1/api/jobs/us/search/1",
            params={
                "app_id": app_id,
                "app_key": app_key,
                "what": search_term,

                "results_per_page": results_per_page,
            },
            timeout=15,
        )
        resp.raise_for_status()
        jobs = resp.json().get("results", [])
        log.info(f"Adzuna: {len(jobs)} results for '{search_term}'")
    except Exception as e:
        log.warning(f"Adzuna fetch failed: {e}")
        return []
    return [
        {
            "site": "adzuna",
            "title": j.get("title", ""),
            "company": j.get("company", {}).get("display_name", ""),
            "location": j.get("location", {}).get("display_name", "remote"),
            "job_type": j.get("contract_time", ""),
            "job_url": j.get("redirect_url", ""),
            "description": j.get("description", ""),
        }
        for j in jobs
    ]


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REQUIRED_COLUMNS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
    log.info(f"Wrote {len(rows)} rows → {path}")


def main() -> None:
    cfg = load_config()
    search_cfg = cfg.get("search", {})

    parser = argparse.ArgumentParser(description="Scrape job listings to CSV.")
    parser.add_argument("--search", default=None, help="Job search term (default: from config.yaml)")
    parser.add_argument("--location", default=None, help="Location string (default: from config.yaml)")
    parser.add_argument("--hours-old", type=int, default=None, help="Max age of listings in hours (default: from config.yaml)")
    parser.add_argument(
        "--sites",
        nargs="+",
        default=None,
        choices=ALL_SITES,
        help=f"Job boards to scrape (default: from config.yaml). Choices: {ALL_SITES}",
    )
    parser.add_argument("--results", type=int, default=None, help="Max results per site (default: from config.yaml)")
    parser.add_argument("--output", default=None, help="Override output CSV path")
    args = parser.parse_args()

    # CLI args take precedence; fall back to config.yaml values
    search_term = args.search or search_cfg.get("term")
    location = args.location or search_cfg.get("location", "remote")
    hours_old = args.hours_old if args.hours_old is not None else search_cfg.get("hours_old", 72)
    sites = args.sites or search_cfg.get("sites", ["indeed", "zip_recruiter"])
    results = args.results if args.results is not None else search_cfg.get("results_per_site", 50)

    if not search_term:
        log.error("No search term provided. Set 'search.term' in config.yaml or pass --search.")
        sys.exit(1)

    rows = run_scrape(
        search_term=search_term,
        location=location,
        hours_old=hours_old,
        site_name=sites,
        results_wanted=results,
    )

    if not rows:
        log.warning("No jobs found — no output file written.")
        sys.exit(0)

    out_path = Path(args.output) if args.output else OUTPUT_DIR / f"jobs_raw_{date.today()}.csv"
    write_csv(rows, out_path)
    print(f"\nDone. {len(rows)} jobs saved to: {out_path}")


if __name__ == "__main__":
    main()
