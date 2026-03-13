"""
src/pipeline.py — CrewAI Pipeline (Milestone 3 / Phase 3 DB)

Reads scraped job listings from a CSV (legacy) or the DB (new), scores each
with a Job Scorer agent, then passes the top results to a Skills Advisor agent
for targeted advice. Writes output to scored_*.json (legacy) or the DB (new).

Legacy CSV usage:
    python src/pipeline.py --input output/jobs_raw_2026-03-11.csv

DB-backed usage (called from main.py --mode analyze):
    run_analysis(db_path, cfg, criteria_id, top_n, parallel_workers)
"""

import argparse
import concurrent.futures
import csv
import json
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import litellm
from crewai import Agent, Crew, LLM, Task

from config import build_criteria_string, get_criteria_weights, get_disqualifiers, get_qualifiers, load_config

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"


# ── LLM ─────────────────────────────────────────────────────────────────────

def get_llm_config() -> dict:
    api_key = os.getenv("OPENROUTER_API_KEY")
    model = os.getenv("CREWAI_MODEL", "openrouter/anthropic/claude-3.5-sonnet")
    if not api_key:
        log.error("OPENROUTER_API_KEY not set in .env")
        sys.exit(1)
    return {"model": model, "api_key": api_key, "base_url": "https://openrouter.ai/api/v1"}


def get_llm(llm_config: dict | None = None) -> LLM:
    if llm_config is None:
        llm_config = get_llm_config()
    return LLM(
        model=llm_config["model"],
        api_key=llm_config["api_key"],
        base_url=llm_config["base_url"],
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def load_jobs(csv_path: Path) -> list[dict]:
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    log.info(f"Loaded {len(rows)} jobs from {csv_path}")
    return rows


def job_summary(job: dict, max_desc: int = 400) -> str:
    """Compact text representation of a job for agent context."""
    desc = (job.get("description") or "").strip()[:max_desc]
    return (
        f"Title: {job.get('title', '?')}\n"
        f"Company: {job.get('company', '?')}\n"
        f"Location: {job.get('location', '?')}\n"
        f"Type: {job.get('job_type', '?')}\n"
        f"URL: {job.get('job_url', '?')}\n"
        f"Description snippet: {desc}"
    )


def strip_thinking_tags(text: str) -> str:
    """
    Remove <think>...</think> blocks emitted by reasoning models (e.g. step-3.5-flash).
    Also strips orphaned </think> tags and any preceding text, which occurs when
    CrewAI captures only the tail of a thinking block.
    """
    # Remove complete <think>...</think> blocks
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # Remove orphaned </think> and everything preceding it in the captured text
    text = re.sub(r"^.*?</think>\s*", "", text, flags=re.DOTALL)
    return text.strip()


def score_single_job(
    job: dict,
    criteria: str,
    llm_config: dict,
    weights: dict[str, float] | None = None,
    qualifiers: list[str] | None = None,
    disqualifiers: list[str] | None = None,
    max_retries: int = 2,
) -> dict:
    """Qualify then score one job. Returns a disqualified result if the LLM
    determines the job fails any qualifier or matches any disqualifier.

    Scoring covers three categories: relevance, duties, income.
    Jobs are scored independently — no cross-job comparison.
    """
    if weights is None:
        weights = {"relevance": 1/3, "duties": 1/3, "income": 1/3}
    base = {
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "job_type": job.get("job_type", ""),
        "job_url": job.get("job_url", ""),
        "site": job.get("site", ""),
    }

    qual_lines = ""
    if qualifiers:
        items = "\n".join(f"  - {q}" for q in qualifiers)
        qual_lines = f"Qualifiers (ALL must be true — if any fails, the job is disqualified):\n{items}\n\n"
    dq_lines = ""
    if disqualifiers:
        items = "\n".join(f"  - {d}" for d in disqualifiers)
        dq_lines = f"Disqualifiers (NONE may be true — if any matches, the job is disqualified):\n{items}\n\n"

    prompt = (
        f"You are evaluating a job listing for a specific candidate.\n\n"
        f"STEP 1 — QUALIFY\n"
        f"Evaluate the job against the qualifiers and disqualifiers below.\n"
        f"{qual_lines}"
        f"{dq_lines}"
        f"If the job FAILS a qualifier or MATCHES a disqualifier, respond with exactly one line:\n"
        f"Disqualified: <brief reason>\n\n"
        f"STEP 2 — SCORE (only if the job passes Step 1)\n"
        f"Score across three categories. Each is rated 1–10 independently.\n"
        f"10 = matches the ideal description exactly. 1 = matches the worst description exactly.\n"
        f"If information is missing (e.g. no salary listed), score conservatively toward 1.\n"
        f"Do not compare this job to other jobs — score it on its own merits.\n\n"
        f"--- SCORING CRITERIA ---\n"
        f"{criteria}\n\n"
        f"--- JOB LISTING ---\n"
        f"{job_summary(job)}\n\n"
        f"Respond with exactly four lines:\n"
        f"Relevance: <integer 1-10>\n"
        f"Duties: <integer 1-10>\n"
        f"Income: <integer 1-10>\n"
        f"Reasoning: <one sentence citing the key factors across all categories>"
    )
    for attempt in range(max_retries + 1):
        try:
            resp = litellm.completion(
                model=llm_config["model"],
                messages=[{"role": "user", "content": prompt}],
                api_key=llm_config["api_key"],
                api_base=llm_config["base_url"],
                timeout=60,
                num_retries=3,  # built-in 429-aware exponential backoff
            )
            text = strip_thinking_tags(resp.choices[0].message.content or "")
            # Check for disqualification before parsing scores
            dq_match = re.search(r"(?i)^disqualified\s*[:\-]?\s*(.+)", text, re.MULTILINE)
            if dq_match:
                reason = dq_match.group(1).strip()
                return {
                    **base,
                    "score": None,
                    "score_relevance": None,
                    "score_duties": None,
                    "score_income": None,
                    "reasoning": reason,
                    "disqualified": True,
                    "disqualified_by": reason,
                    "error": None,
                }
            cat_scores: dict[str, float | None] = {}
            for cat in ("relevance", "duties", "income"):
                m = re.search(rf"(?i){cat}\s*[:\-]?\s*(\d+(?:\.\d+)?)", text)
                cat_scores[cat] = float(m.group(1)) if m else None
            reason_match = re.search(r"(?i)reasoning?\s*[:\-]?\s*(.+)", text, re.DOTALL)
            reasoning = reason_match.group(1).strip() if reason_match else text[:200]
            # Weighted final score (skip categories where score is None)
            scored = {k: v for k, v in cat_scores.items() if v is not None}
            if scored:
                total_w = sum(weights.get(k, 1/3) for k in scored)
                final = sum(scored[k] * weights.get(k, 1/3) for k in scored) / total_w
            else:
                final = None
            return {
                **base,
                "score": round(final, 1) if final is not None else None,
                "score_relevance": cat_scores.get("relevance"),
                "score_duties": cat_scores.get("duties"),
                "score_income": cat_scores.get("income"),
                "reasoning": reasoning,
                "disqualified": False,
                "error": None,
            }
        except Exception as e:
            if attempt < max_retries:
                log.warning(
                    f"Retry {attempt + 1}/{max_retries}: '{base['title']}' @ '{base['company']}': {e}"
                )
            else:
                log.error(f"Scoring failed: '{base['title']}' @ '{base['company']}': {e}")
                return {
                    **base,
                    "score": None,
                    "score_relevance": None,
                    "score_duties": None,
                    "score_income": None,
                    "reasoning": "",
                    "disqualified": False,
                    "error": str(e),
                }

# ── Pipeline ─────────────────────────────────────────────────────────────────

def run_pipeline(
    csv_path: Path,
    criteria: str,
    top_n: int = 10,
    output_path: Path | None = None,
    parallel_workers: int = 10,
    weights: dict[str, float] | None = None,
    qualifiers: list[str] | None = None,
    disqualifiers: list[str] | None = None,
) -> Path:
    llm_config = get_llm_config()
    jobs = load_jobs(csv_path)

    if not jobs:
        log.error("No jobs to process.")
        sys.exit(1)

    # ── Parallel scoring + qualification ───────────────────────────────────────
    if weights is None:
        weights = {"relevance": 1/3, "duties": 1/3, "income": 1/3}
    log.info(f"Qualifying and scoring {len(jobs)} jobs ({parallel_workers} parallel workers)...")
    scored_jobs: list[dict] = []
    disqualified_jobs: list[dict] = []
    errored = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel_workers) as executor:
        futures = {
            executor.submit(
                score_single_job, job, criteria, llm_config, weights,
                qualifiers or [], disqualifiers or []
            ): job
            for job in jobs
        }
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result.get("disqualified"):
                disqualified_jobs.append(result)
                log.info(f"  ✗ Disqualified: '{result['title']}' — {result.get('disqualified_by', '')}")
            elif result.get("error"):
                errored += 1
                log.error(f"  ✗ Failed: '{result['title']}' @ '{result['company']}'")
                scored_jobs.append(result)
            else:
                score_str = f"{result['score']:.1f}/10" if result["score"] is not None else "?/10"
                log.info(f"  ✓ {result['title']} @ {result['company']} → {score_str}")
                scored_jobs.append(result)

    if errored:
        log.warning(f"{errored} job(s) failed to score and will appear unranked.")

    # Sort by score descending (nulls last)
    scored_jobs.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0)))

    # ── Skills Advisor ────────────────────────────────────────────────────────
    llm = get_llm(llm_config)
    top_summary = "\n\n---\n\n".join(
        f"Title: {j['title']}\nCompany: {j['company']}\nLocation: {j['location']}\n"
        f"Score: {j['score']}/10\nReasoning: {j['reasoning']}"
        for j in scored_jobs[:top_n]
        if j.get("score") is not None
    )

    advisor_agent = Agent(
        role="Skills Advisor",
        goal=(
            "Identify the most important skills the user should highlight or develop "
            "based on the top-scoring job listings."
        ),
        backstory=(
            "You are a senior tech recruiter who has reviewed thousands of job descriptions. "
            "You can quickly identify patterns in what employers want and translate that "
            "into concrete, actionable advice for job seekers."
        ),
        llm=llm,
        verbose=False,
        allow_delegation=False,
    )

    advisor_task = Task(
        description=(
            f"The user's current criteria: {criteria}\n\n"
            f"Based on the following top-scoring job listings, identify:\n"
            f"1. The top 3–5 skills that appear most frequently in high-scoring listings.\n"
            f"2. Any skill gaps — skills that keep appearing but aren't in the user's criteria.\n"
            f"3. One concrete action the user should take this week to improve their candidacy.\n\n"
            f"Be specific and practical.\n\n"
            f"Top listings:\n\n{top_summary}"
        ),
        expected_output=(
            "A structured skills analysis with: top skills, skill gaps, and one recommended action."
        ),
        agent=advisor_agent,
    )

    crew = Crew(
        agents=[advisor_agent],
        tasks=[advisor_task],
        verbose=False,
    )

    log.info("Running Skills Advisor...")
    crew.kickoff()

    advisor_output = strip_thinking_tags(
        advisor_task.output.raw if advisor_task.output else ""
    )

    output = {
        "date": str(date.today()),
        "criteria": criteria,
        "jobs_evaluated": len(jobs),
        "jobs_disqualified": len(disqualified_jobs),
        "jobs_scored": len(scored_jobs),
        "scored_jobs": scored_jobs,
        "disqualified_jobs": disqualified_jobs,
        "skills_advice": advisor_output,
    }

    # ── Write ─────────────────────────────────────────────────────────────────
    out_path = output_path or OUTPUT_DIR / f"scored_{date.today()}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    log.info(f"Wrote scored output → {out_path}")
    return out_path


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    cfg = load_config()
    criteria_cfg = cfg.get("criteria", {})
    config_criteria = build_criteria_string(cfg)
    config_top_n = criteria_cfg.get("top_n", 10)

    parser = argparse.ArgumentParser(description="Score scraped job listings with CrewAI.")
    parser.add_argument(
        "--input",
        required=True,
        help="Path to jobs_raw CSV from scraper.py",
    )
    parser.add_argument(
        "--criteria",
        default=None,
        help="Override scoring criteria (default: built from config.yaml)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=None,
        help="Override number of jobs to score (default: criteria.top_n in config.yaml)",
    )
    parser.add_argument("--output", default=None, help="Override output JSON path")
    args = parser.parse_args()

    # CLI args take precedence; fall back to config.yaml values
    criteria = args.criteria or config_criteria
    top_n = args.top if args.top is not None else config_top_n

    csv_path = Path(args.input)
    if not csv_path.exists():
        log.error(f"Input file not found: {csv_path}")
        sys.exit(1)

    log.info(f"Criteria: {criteria[:120]}{'...' if len(criteria) > 120 else ''}")

    parallel_workers = cfg.get("scoring", {}).get("parallel_workers", 10)
    out_path = run_pipeline(
        csv_path=csv_path,
        criteria=criteria,
        top_n=top_n,
        output_path=Path(args.output) if args.output else None,
        parallel_workers=parallel_workers,
        weights=get_criteria_weights(cfg),
        qualifiers=get_qualifiers(cfg),
        disqualifiers=get_disqualifiers(cfg),
    )
    print(f"\nDone. Scored output: {out_path}")


if __name__ == "__main__":
    main()


# ── DB-backed analysis ────────────────────────────────────────────────────────

def run_analysis(
    db_path: Path,
    cfg: dict,
    criteria_id: int,
    top_n: int = 50,
    parallel_workers: int = 10,
) -> int:
    """Score all unscored jobs in the DB under the active criteria.

    Reads from the jobs_unscored view (jobs with no score under criteria_id),
    runs LLM scoring in parallel, writes results to job_scores via the write
    queue, then runs the Skills Advisor and writes to job_insights.

    Returns the number of jobs scored.
    """
    from database import (  # local import avoids circular dependency at module load
        INSERT_INSIGHT_SQL,
        INSERT_SCORE_SQL,
        SQLiteWriteQueue,
        get_unscored_jobs,
        _open_conn,
    )
    from config import build_criteria_string, get_criteria_weights, get_disqualifiers, get_qualifiers

    criteria = build_criteria_string(cfg)
    weights = get_criteria_weights(cfg)
    qualifiers = get_qualifiers(cfg)
    disqualifiers = get_disqualifiers(cfg)
    llm_config = get_llm_config()
    model_name = llm_config["model"].split("/")[-1]

    jobs = get_unscored_jobs(db_path)
    if not jobs:
        log.info("No unscored jobs found — nothing to analyze.")
        return 0

    log.info(f"Scoring {len(jobs)} unscored jobs ({parallel_workers} parallel workers)...")

    writer = SQLiteWriteQueue(db_path)
    scored_count = 0
    qualified_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel_workers) as executor:
        futures = {
            executor.submit(
                score_single_job, job, criteria, llm_config, weights, qualifiers, disqualifiers
            ): job
            for job in jobs
        }
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            job_id = futures[future].get("job_id")
            if job_id is None:
                continue

            is_qualified = not result.get("disqualified", False)
            dq_reason = result.get("disqualified_by") if not is_qualified else None

            writer.enqueue(
                INSERT_SCORE_SQL,
                (
                    job_id,
                    criteria_id,
                    is_qualified,
                    dq_reason,
                    result.get("score"),
                    result.get("score_relevance"),
                    result.get("score_duties"),
                    result.get("score_income"),
                    result.get("reasoning", ""),
                    model_name,
                ),
            )
            scored_count += 1
            if is_qualified:
                qualified_count += 1
                score_str = f"{result['score']:.1f}/10" if result["score"] is not None else "?/10"
                log.info(f"  ✓ {result['title']} @ {result['company']} → {score_str}")
            else:
                log.info(f"  ✗ Disqualified: '{result['title']}' — {dq_reason or ''}")

    writer.flush()

    # ── Skills Advisor ────────────────────────────────────────────────────────
    conn = _open_conn(db_path)
    top_rows = conn.execute(
        "SELECT title, company, location, score_overall, reasoning"
        " FROM jobs_qualified LIMIT ?",
        (top_n,),
    ).fetchall()
    conn.close()

    top_summary = "\n\n---\n\n".join(
        f"Title: {r['title']}\nCompany: {r['company']}\nLocation: {r['location']}\n"
        f"Score: {r['score_overall']}/10\nReasoning: {r['reasoning']}"
        for r in top_rows
        if r["score_overall"] is not None
    )

    if top_summary:
        llm = get_llm(llm_config)
        advisor_agent = Agent(
            role="Skills Advisor",
            goal=(
                "Identify the most important skills the user should highlight or develop "
                "based on the top-scoring job listings."
            ),
            backstory=(
                "You are a senior tech recruiter who has reviewed thousands of job descriptions. "
                "You can quickly identify patterns in what employers want and translate that "
                "into concrete, actionable advice for job seekers."
            ),
            llm=llm,
            verbose=False,
            allow_delegation=False,
        )
        advisor_task = Task(
            description=(
                f"The user's current criteria: {criteria}\n\n"
                f"Based on the following top-scoring job listings, identify:\n"
                f"1. The top 3–5 skills that appear most frequently in high-scoring listings.\n"
                f"2. Any skill gaps — skills that keep appearing but aren't in the user's criteria.\n"
                f"3. One concrete action the user should take this week to improve their candidacy.\n\n"
                f"Be specific and practical.\n\n"
                f"Top listings:\n\n{top_summary}"
            ),
            expected_output=(
                "A structured skills analysis with: top skills, skill gaps, and one recommended action."
            ),
            agent=advisor_agent,
        )
        crew = Crew(agents=[advisor_agent], tasks=[advisor_task], verbose=False)
        log.info("Running Skills Advisor...")
        crew.kickoff()
        skills_advice = strip_thinking_tags(advisor_task.output.raw if advisor_task.output else "")
        writer.enqueue(INSERT_INSIGHT_SQL, (skills_advice, scored_count, model_name))

    writer.close()

    log.info(
        f"Analysis complete: {scored_count} scored, {qualified_count} qualified,"
        f" {scored_count - qualified_count} disqualified"
    )
    return scored_count
