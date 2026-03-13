"""
src/reporter.py — Results Reporter (Milestone 5)

Reads a scored JSON file from pipeline.py and writes a human-readable
output/results_YYYY-MM-DD.md sorted by score descending.

Usage (standalone):
    python src/reporter.py --input output/scored_2026-03-11.json
"""

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from config import load_config

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"


def score_bar(score: float | None, width: int = 10) -> str:
    """Visual score bar, e.g. ████████░░ 8/10"""
    if score is None:
        return "?/10"
    filled = round(score)
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar} {score:.0f}/10"


def render_report(scored_json_path: Path, top_n: int) -> str:
    with open(scored_json_path, encoding="utf-8") as f:
        data = json.load(f)

    jobs = data.get("scored_jobs", [])
    criteria = data.get("criteria", "")
    skills_advice = data.get("skills_advice", "").strip()
    run_date = data.get("date", str(date.today()))
    evaluated = data.get("jobs_evaluated", len(jobs))
    disqualified = data.get("jobs_disqualified", 0)

    # Sort by score descending (unscored last), then take top_n
    jobs_sorted = sorted(jobs, key=lambda j: (j.get("score") is None, -(j.get("score") or 0)))
    top_jobs = jobs_sorted[:top_n]

    lines = []
    lines.append(f"# Job Search Results — {run_date}")
    lines.append("")
    lines.append(f"> **Criteria:** {criteria}")
    dq_note = f" | **Disqualified (pre-filter):** {disqualified}" if disqualified else ""
    lines.append(f"> **Jobs evaluated:** {evaluated}{dq_note} | **Showing top {len(top_jobs)}**")
    lines.append("")
    lines.append("---")
    lines.append("")

    for i, job in enumerate(top_jobs, 1):
        score = job.get("score")
        title = job.get("title") or "Unknown Title"
        company = job.get("company") or "Unknown Company"
        location = job.get("location") or "—"
        job_type = job.get("job_type") or "—"
        url = job.get("job_url") or ""
        reasoning = (job.get("reasoning") or "").strip()

        lines.append(f"## {i}. {title}")
        lines.append(f"**{company}** · {location} · {job_type}")
        lines.append("")
        lines.append(f"**Score:** {score_bar(score)}")
        # Per-category breakdown (present when scored with the new weighted system)
        cat_parts = []
        for cat_label, cat_key in (
            ("Relevance", "score_relevance"),
            ("Duties", "score_duties"),
            ("Income", "score_income"),
        ):
            v = job.get(cat_key)
            if v is not None:
                cat_parts.append(f"{cat_label} {int(v)}")
        if cat_parts:
            lines.append(f"**Breakdown:** {' · '.join(cat_parts)}")
        if reasoning:
            lines.append(f"**Why:** {reasoning}")
        if url:
            lines.append(f"**Apply:** [{url}]({url})")
        lines.append("")
        lines.append("---")
        lines.append("")

    if skills_advice:
        lines.append("## Skills Advice")
        lines.append("")
        lines.append(skills_advice)
        lines.append("")

    return "\n".join(lines)


def write_report(scored_json_path: Path, output_path: Path | None, top_n: int) -> Path:
    out_path = output_path or OUTPUT_DIR / f"results_{date.today()}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    content = render_report(scored_json_path, top_n=top_n)
    out_path.write_text(content, encoding="utf-8")
    log.info(f"Wrote report → {out_path}")
    return out_path


def main() -> None:
    cfg = load_config()
    top_n = cfg.get("criteria", {}).get("top_n", 10)

    parser = argparse.ArgumentParser(description="Format scored jobs into a Markdown report.")
    parser.add_argument("--input", required=True, help="Path to scored JSON from pipeline.py")
    parser.add_argument("--top", type=int, default=None, help="Override number of results to show")
    parser.add_argument("--output", default=None, help="Override output .md path")
    args = parser.parse_args()

    json_path = Path(args.input)
    if not json_path.exists():
        log.error(f"Input file not found: {json_path}")
        sys.exit(1)

    effective_top = args.top if args.top is not None else top_n
    out_path = write_report(json_path, Path(args.output) if args.output else None, top_n=effective_top)
    print(f"\nDone. Report: {out_path}")


if __name__ == "__main__":
    main()


# ── DB-backed report ──────────────────────────────────────────────────────────

def generate_report_from_db(db_path: "Path", output_path: "Path", top_n: int) -> "Path":  # type: ignore[name-defined]
    """Generate a Markdown report directly from the DB (no intermediate JSON).

    Reads from jobs_qualified view and job_insights. Called by main.py --mode report.
    """
    from database import _open_conn  # local import avoids circular dependency

    conn = _open_conn(db_path)
    top_jobs = conn.execute("SELECT * FROM jobs_qualified LIMIT ?", (top_n,)).fetchall()
    latest_insight = conn.execute(
        "SELECT skills_advice FROM job_insights ORDER BY analysis_date DESC LIMIT 1"
    ).fetchone()
    stats = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM jobs)                              AS total_jobs,
            (SELECT COUNT(*) FROM jobs_unscored)                     AS unscored_count,
            (SELECT COUNT(*) FROM job_scores
             WHERE is_qualified = 1
               AND criteria_id = (SELECT criteria_id FROM criteria WHERE is_active = 1 LIMIT 1)
            )                                                        AS qualified_count
        """
    ).fetchone()
    conn.close()

    from datetime import datetime

    lines = [
        f"# Job Search Results — {datetime.now():%Y-%m-%d}",
        "",
        f"> **Total in DB:** {stats['total_jobs']}  |  "
        f"**Qualified:** {stats['qualified_count']}  |  "
        f"**Unscored:** {stats['unscored_count']}",
        "",
        "---",
        "",
    ]
    for i, job in enumerate(top_jobs, 1):
        score = job["score_overall"]
        lines.append(f"## {i}. {job['title']}")
        lines.append(f"**{job['company']}** · {job['location'] or '—'} · {job['job_type'] or '—'}")
        lines.append("")
        lines.append(f"**Score:** {score_bar(score)}")
        cat_parts = []
        for label, key in (("Relevance", "score_relevance"), ("Duties", "score_duties"), ("Income", "score_income")):
            v = job[key]
            if v is not None:
                cat_parts.append(f"{label} {int(v)}")
        if cat_parts:
            lines.append(f"**Breakdown:** {' · '.join(cat_parts)}")
        reasoning = (job["reasoning"] or "").strip()
        if reasoning:
            lines.append(f"**Why:** {reasoning}")
        url = job["job_url"] or ""
        if url:
            lines.append(f"**Apply:** [{url}]({url})")
        lines.append("")
        lines.append("---")
        lines.append("")

    if latest_insight and latest_insight["skills_advice"]:
        lines.append("## Skills Advice")
        lines.append("")
        lines.append(latest_insight["skills_advice"])
        lines.append("")

    content = "\n".join(lines)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    log.info(f"Wrote report → {output_path}")
    return output_path
