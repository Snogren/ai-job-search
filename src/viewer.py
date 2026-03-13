"""src/viewer.py — FastAPI web UI for browsing scored jobs.

Run from the project root:
    uvicorn src.viewer:app --reload --port 8000

Visit: http://localhost:8000
"""

import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

DB_PATH = Path(__file__).parent.parent / "ai_job_search.db"
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

app = FastAPI()

_VALID_ACTION_TYPES = {"dismissed", "saved", "applied", "archived", "active"}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    site: str = "",
    location: str = "",
    job_type: str = "",
    min_score: float = 0.0,
    qualified_only: bool = False,
    show_dismissed: bool = False,
    criteria_id: int | None = None,
):
    conn = _conn()

    sites = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT site FROM jobs WHERE site IS NOT NULL ORDER BY site"
        ).fetchall()
    ]
    job_types = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT job_type FROM jobs WHERE job_type IS NOT NULL ORDER BY job_type"
        ).fetchall()
    ]

    # Resolve effective criteria_id (default to is_default criteria)
    criteria_list = [
        dict(r)
        for r in conn.execute(
            "SELECT criteria_id, name FROM criteria WHERE is_enabled = 1 ORDER BY created_at DESC"
        ).fetchall()
    ]
    if criteria_id is None:
        row = conn.execute("SELECT criteria_id FROM criteria WHERE is_default = 1 LIMIT 1").fetchone()
        effective_criteria_id: int | None = row[0] if row else None
    else:
        effective_criteria_id = criteria_id

    # Build score subquery — criteria-aware when possible
    if effective_criteria_id is not None:
        score_subq = "SELECT score_id FROM job_scores WHERE job_id = j.job_id AND criteria_id = ? ORDER BY scored_at DESC LIMIT 1"
        score_params: list = [effective_criteria_id]
    else:
        score_subq = "SELECT score_id FROM job_scores WHERE job_id = j.job_id ORDER BY scored_at DESC LIMIT 1"
        score_params = []

    sql = f"""
        SELECT j.job_id, j.job_url, j.title, j.company, j.location, j.job_type, j.site,
               j.first_seen_at, j.canonical_key,
               js.score_overall, js.score_relevance, js.score_duties, js.score_income,
               js.reasoning, js.is_qualified, js.disqualified_reason,
               ja.action_type AS current_action
        FROM jobs j
        LEFT JOIN job_scores js
               ON js.score_id = ({score_subq})
        LEFT JOIN job_actions ja ON ja.action_id = (
               SELECT action_id FROM job_actions
               WHERE job_id = j.job_id
               ORDER BY created_at DESC LIMIT 1
        )
        WHERE 1=1
    """
    params: list = score_params[:]

    if not show_dismissed:
        sql += " AND (ja.action_type IS NULL OR ja.action_type != 'dismissed')"

    if site:
        sql += " AND j.site = ?"
        params.append(site)
    if location:
        sql += " AND j.location LIKE ?"
        params.append(f"%{location}%")
    if job_type:
        sql += " AND j.job_type = ?"
        params.append(job_type)
    if min_score > 0:
        sql += " AND js.score_overall >= ?"
        params.append(min_score)
    if qualified_only:
        sql += " AND js.is_qualified = 1"

    sql += " ORDER BY js.score_overall DESC NULLS LAST, j.first_seen_at DESC LIMIT 200"

    raw_jobs = [dict(r) for r in conn.execute(sql, params).fetchall()]

    # Deduplicate: one card per canonical_key; rows are already ordered best-score-first
    seen_keys: set[str] = set()
    jobs: list[dict] = []
    for job in raw_jobs:
        key = job["canonical_key"]
        if key is None or key not in seen_keys:
            jobs.append(job)
            if key is not None:
                seen_keys.add(key)

    # Build canonical_sites: for each canonical_key, list all (site, job_url, score) rows
    # Uses the same criteria filter so alias badge scores stay consistent with the main view
    canonical_sites: dict[str, list[dict]] = {}
    canonical_keys = list({j["canonical_key"] for j in jobs if j.get("canonical_key")})
    if canonical_keys:
        placeholders = ",".join("?" * len(canonical_keys))
        if effective_criteria_id is not None:
            alias_score_subq = "SELECT score_id FROM job_scores WHERE job_id = j.job_id AND criteria_id = ? ORDER BY scored_at DESC LIMIT 1"
            alias_params: list = [effective_criteria_id] + canonical_keys
        else:
            alias_score_subq = "SELECT score_id FROM job_scores WHERE job_id = j.job_id ORDER BY scored_at DESC LIMIT 1"
            alias_params = canonical_keys
        alias_rows = conn.execute(
            f"""
            SELECT j.canonical_key, j.site, j.job_url, js.score_overall
            FROM jobs j
            LEFT JOIN job_scores js ON js.score_id = ({alias_score_subq})
            WHERE j.canonical_key IN ({placeholders})
            """,
            alias_params,
        ).fetchall()
        for row in alias_rows:
            key = row["canonical_key"]
            canonical_sites.setdefault(key, []).append(dict(row))

    # alias_groups: {canonical_key: {site: total_listing_count}} — used by template for compact badges
    alias_groups: dict[str, dict[str, int]] = {}
    for key, aliases in canonical_sites.items():
        groups: dict[str, int] = {}
        for alias in aliases:
            s = alias["site"]
            groups[s] = groups.get(s, 0) + 1
        alias_groups[key] = groups

    conn.close()

    return TEMPLATES.TemplateResponse(
        "jobs.html",
        {
            "request": request,
            "jobs": jobs,
            "sites": sites,
            "job_types": job_types,
            "criteria_list": criteria_list,
            "canonical_sites": canonical_sites,
            "alias_groups": alias_groups,
            "filters": {
                "site": site,
                "location": location,
                "job_type": job_type,
                "min_score": min_score if min_score > 0 else "",
                "qualified_only": qualified_only,
                "show_dismissed": show_dismissed,
                "criteria_id": effective_criteria_id,
            },
            "total": len(jobs),
        },
    )


class JobActionRequest(BaseModel):
    action_type: str
    note: str = ""


@app.post("/job/{job_id}/action")
def job_action(job_id: int, body: JobActionRequest) -> JSONResponse:
    if body.action_type not in _VALID_ACTION_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid action_type: {body.action_type!r}")
    from .database import record_job_action
    record_job_action(DB_PATH, job_id, body.action_type, body.note or None)
    return JSONResponse({"ok": True})
