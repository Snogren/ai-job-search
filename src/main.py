"""
src/main.py — Full Pipeline Entry Point

DB-backed pipeline: scrape → analyze → report, each independently runnable.
All settings come from config.yaml; modes and individual overrides via flags.

Usage:
    python src/main.py                          # full pipeline (scrape+analyze+report)
    python src/main.py --mode scrape            # scrape only → upsert to DB
    python src/main.py --mode analyze           # score unscored jobs in DB
    python src/main.py --mode report            # generate Markdown from DB
    python src/main.py --mode pipeline          # all three (default)
    python src/main.py --status                 # print DB summary and exit
    python src/main.py --mode scrape --keywords "python engineer" "backend engineer"
    python src/main.py --mode report --top 20 --output /tmp/jobs.md
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from config import load_config
from database import (
    db_status,
    finish_scrape_run,
    get_or_create_criteria,
    init_db,
    migrate_from_csv_json,
    SQLiteWriteQueue,
    start_scrape_run,
)
from scraper import ingest_parallel_keywords
from pipeline import run_analysis
from reporter import generate_report_from_db

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
DB_PATH = PROJECT_ROOT / "ai_job_search.db"


def _ensure_db(cfg: dict) -> int:
    """Initialize DB and run one-time migration from output/ if DB is new.

    Returns the active criteria_id.
    """
    is_new = not DB_PATH.exists()
    init_db(DB_PATH)

    # Resolve / upsert criteria before migration so scores can be linked
    criteria_id = get_or_create_criteria(DB_PATH, cfg)

    if is_new:
        # Find most recent CSV and JSON in output/ for migration
        csvs = sorted(OUTPUT_DIR.glob("jobs_raw_*.csv"), reverse=True)
        jsons = sorted(OUTPUT_DIR.glob("scored_*.json"), reverse=True)
        csv_path = csvs[0] if csvs else None
        json_path = jsons[0] if jsons else None
        if csv_path or json_path:
            log.info("New DB detected — migrating legacy output/ files...")
            migrate_from_csv_json(DB_PATH, cfg, csv_path=csv_path, json_path=json_path)

    return criteria_id


def _print_status() -> None:
    if not DB_PATH.exists():
        print("No database found. Run 'python src/main.py --mode scrape' to create one.")
        return
    s = db_status(DB_PATH)
    print(f"\n{'='*50}")
    print(f"  Database: {DB_PATH.name}")
    print(f"  Total jobs:    {s['total_jobs']}")
    print(f"  Unscored:      {s['unscored']}")
    print(f"  Qualified:     {s['qualified']}")
    print(f"  Disqualified:  {s['disqualified']}")
    print(f"  Scrape runs:   {s['scrape_runs']}")
    print(f"{'='*50}\n")


def main() -> None:
    cfg = load_config()
    search_cfg = cfg.get("search", {})
    criteria_cfg = cfg.get("criteria", {})

    parser = argparse.ArgumentParser(
        description="AI Job Search — DB-backed pipeline (scrape → analyze → report)."
    )
    parser.add_argument(
        "--mode",
        choices=["scrape", "analyze", "report", "pipeline"],
        default="pipeline",
        help="Pipeline stage to run (default: pipeline = all three)",
    )
    parser.add_argument("--keywords", nargs="+", default=None, help="Override search keywords")
    parser.add_argument("--locations", nargs="+", default=None, help="Override search locations")
    parser.add_argument("--top", type=int, default=None, help="Override top_n for report/analyze")
    parser.add_argument("--output", default=None, help="Override report output path (.md)")
    parser.add_argument("--status", action="store_true", help="Print DB summary and exit")
    args = parser.parse_args()

    if args.status:
        _print_status()
        return

    top_n = args.top if args.top is not None else criteria_cfg.get("top_n", 50)
    parallel_workers = cfg.get("scoring", {}).get("parallel_workers", 10)
    today = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    report_path = Path(args.output) if args.output else OUTPUT_DIR / f"results_{today}.md"

    criteria_id = _ensure_db(cfg)

    # ── Scrape ────────────────────────────────────────────────────────────────
    if args.mode in ("scrape", "pipeline"):
        log.info("=== Scraping jobs ===")
        keywords = args.keywords or None  # None → read from config
        locations = args.locations or None

        hours_old = search_cfg.get("hours_old", 72)
        results_per_site = search_cfg.get("results_per_site", 50)
        sites = search_cfg.get("sites", ["indeed", "zip_recruiter"])
        kw_list = keywords or (
            search_cfg.get("keywords") or
            ([search_cfg["term"]] if search_cfg.get("term") else [])
        )
        loc_list = locations or [search_cfg.get("location", "remote")]

        run_id = start_scrape_run(
            DB_PATH,
            keywords=kw_list,
            locations=loc_list,
            sites=sites,
            hours_old=hours_old,
            results_per_site=results_per_site,
        )

        writer = SQLiteWriteQueue(DB_PATH)
        try:
            total_enqueued = ingest_parallel_keywords(
                cfg=cfg,
                db_path=DB_PATH,
                run_id=run_id,
                writer=writer,
                keyword_overrides=keywords,
                location_overrides=locations,
            )
        finally:
            writer.close()

        finish_scrape_run(
            DB_PATH,
            run_id=run_id,
            new_jobs=writer.new_count,
            updated_jobs=writer.update_count,
            raw_count=total_enqueued,
        )
        log.info(
            f"Scrape complete: {writer.new_count} new, {writer.update_count} refreshed"
            f" ({total_enqueued} total rows processed)"
        )

    # ── Analyze ───────────────────────────────────────────────────────────────
    if args.mode in ("analyze", "pipeline"):
        log.info("=== Analyzing jobs ===")
        run_analysis(
            db_path=DB_PATH,
            cfg=cfg,
            criteria_id=criteria_id,
            top_n=top_n,
            parallel_workers=parallel_workers,
        )

    # ── Report ────────────────────────────────────────────────────────────────
    if args.mode in ("report", "pipeline"):
        log.info("=== Generating report ===")
        generate_report_from_db(DB_PATH, output_path=report_path, top_n=top_n)

        print(f"\n{'='*50}")
        print(f"  Job search complete!")
        print(f"  Database:  {DB_PATH}")
        print(f"  Report:    {report_path}")
        print(f"{'='*50}\n")


if __name__ == "__main__":
    main()
