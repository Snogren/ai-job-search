"""
src/main.py — Full Pipeline Entry Point (Milestone 5)

Chains: scrape → score → report in a single command.
All settings come from config.yaml; individual steps can be overridden via flags.

Usage:
    python src/main.py
    python src/main.py --search "data engineer" --location "New York"
    python src/main.py --skip-scrape --input output/jobs_raw_2026-03-11.csv
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from config import build_criteria_string, get_criteria_weights, get_disqualifiers, get_qualifiers, load_config
from scraper import deduplicate_rows, fetch_adzuna, fetch_himalayas, fetch_remotive, run_scrape, write_csv
from pipeline import run_pipeline
from reporter import write_report

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"


def main() -> None:
    cfg = load_config()
    search_cfg = cfg.get("search", {})
    criteria_cfg = cfg.get("criteria", {})

    parser = argparse.ArgumentParser(description="AI Job Search — full pipeline (scrape → score → report).")
    parser.add_argument("--search", default=None, help="Override search term from config.yaml")
    parser.add_argument("--location", default=None, help="Override location from config.yaml")
    parser.add_argument("--top", type=int, default=None, help="Override top_n from config.yaml")
    parser.add_argument(
        "--skip-scrape",
        action="store_true",
        help="Skip scraping and use an existing CSV (requires --input)",
    )
    parser.add_argument("--input", default=None, help="Path to existing jobs CSV (used with --skip-scrape)")
    args = parser.parse_args()

    today = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    raw_csv = OUTPUT_DIR / f"jobs_raw_{today}.csv"
    scored_json = OUTPUT_DIR / f"scored_{today}.json"
    report_md = OUTPUT_DIR / f"results_{today}.md"
    top_n = args.top if args.top is not None else criteria_cfg.get("top_n", 10)

    # ── Step 1: Scrape ────────────────────────────────────────────────────────
    if args.skip_scrape:
        if args.input:
            raw_csv = Path(args.input)
        if not raw_csv.exists():
            log.error(f"--skip-scrape set but no CSV found at {raw_csv}. Pass --input or run without --skip-scrape.")
            sys.exit(1)
        log.info(f"Skipping scrape, using existing: {raw_csv}")
    else:
        search_term = args.search or search_cfg.get("term")
        location = args.location or search_cfg.get("location", "remote")
        hours_old = search_cfg.get("hours_old", 72)
        sites = search_cfg.get("sites", ["indeed", "zip_recruiter"])
        results_per_site = search_cfg.get("results_per_site", 50)

        if not search_term:
            log.error("No search term. Set 'search.term' in config.yaml or pass --search.")
            sys.exit(1)

        log.info("=== Step 1/3: Scraping jobs ===")
        rows = run_scrape(
            search_term=search_term,
            location=location,
            hours_old=hours_old,
            site_name=sites,
            results_wanted=results_per_site,
        )

        # ── API sources (no scraping, no ToS risk) ────────────────────────────
        api_cfg = cfg.get("api_sources", {})
        api_rows: list[dict] = []

        if api_cfg.get("remotive", False):
            api_rows.extend(fetch_remotive(search_term))

        if api_cfg.get("himalayas", False):
            api_rows.extend(fetch_himalayas(search_term))

        adzuna_cfg = api_cfg.get("adzuna", {})
        if isinstance(adzuna_cfg, dict):
            adzuna_id = adzuna_cfg.get("app_id", "")
            adzuna_key = adzuna_cfg.get("app_key", "")
            if adzuna_id and adzuna_key:
                api_rows.extend(fetch_adzuna(search_term, adzuna_id, adzuna_key))

        if api_rows:
            log.info(f"API sources added {len(api_rows)} rows; deduplicating combined set")
            rows = deduplicate_rows(rows + api_rows)

        if not rows:
            log.warning("No jobs found. Exiting.")
            sys.exit(0)
        write_csv(rows, raw_csv)

    # ── Step 2: Score ─────────────────────────────────────────────────────────
    log.info("=== Step 2/3: Scoring with CrewAI ===")
    criteria = build_criteria_string(cfg)
    weights = get_criteria_weights(cfg)
    qualifiers = get_qualifiers(cfg)
    disqualifiers = get_disqualifiers(cfg)
    parallel_workers = cfg.get("scoring", {}).get("parallel_workers", 10)
    run_pipeline(
        csv_path=raw_csv,
        criteria=criteria,
        top_n=top_n,
        output_path=scored_json,
        parallel_workers=parallel_workers,
        weights=weights,
        qualifiers=qualifiers,
        disqualifiers=disqualifiers,
    )

    # ── Step 3: Report ────────────────────────────────────────────────────────
    log.info("=== Step 3/3: Generating report ===")
    write_report(
        scored_json_path=scored_json,
        output_path=report_md,
        top_n=top_n,
    )

    print(f"\n{'='*50}")
    print(f"  Job search complete!")
    print(f"  Raw listings:   {raw_csv}")
    print(f"  Scored output:  {scored_json}")
    print(f"  Report:         {report_md}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()
