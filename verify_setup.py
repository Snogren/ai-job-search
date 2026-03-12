"""
verify_setup.py — Milestone 1 quality gate checker.

Run from the project root with the venv active:
    python verify_setup.py

Checks:
  1. venv is active
  2. All required packages importable (jobspy, crewai, dotenv)
  3. .env exists on disk
  4. .gitignore contains .env
  5. .env is not tracked by git
  6. Test scrape returns real job rows
"""

import sys
import os
import subprocess
import importlib.metadata
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
PASS = "[PASS]"
FAIL = "[FAIL]"


def check(label: str, condition: bool, detail: str = "") -> bool:
    status = PASS if condition else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  {status} {label}{suffix}")
    return condition


def main() -> None:
    print("\n=== Milestone 1 Quality Gate ===\n")
    results = []

    # 1. venv is active
    in_venv = sys.prefix != sys.base_prefix
    results.append(check("venv is active", in_venv, sys.prefix if in_venv else "not in a venv"))

    # 2. Package versions
    for pkg, import_name in [("python-jobspy", "jobspy"), ("crewai", "crewai"), ("python-dotenv", "dotenv")]:
        try:
            __import__(import_name)
            try:
                version = importlib.metadata.version(pkg)
            except importlib.metadata.PackageNotFoundError:
                version = "version unknown"
            results.append(check(f"{import_name} importable", True, version))
        except ImportError as e:
            results.append(check(f"{import_name} importable", False, str(e)))

    # 3. .env exists
    env_path = PROJECT_ROOT / ".env"
    results.append(check(".env exists on disk", env_path.exists(), str(env_path)))

    # 4. .gitignore contains .env
    gitignore_path = PROJECT_ROOT / ".gitignore"
    if gitignore_path.exists():
        gi_lines = gitignore_path.read_text().splitlines()
        dotenv_ignored = any(line.strip() in (".env", ".env*") for line in gi_lines)
    else:
        dotenv_ignored = False
    results.append(check(".gitignore contains .env", dotenv_ignored))

    # 5. .env is not git-tracked
    try:
        tracked = subprocess.run(
            ["git", "ls-files", ".env"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        not_tracked = tracked.stdout.strip() == ""
        results.append(check(".env is not git-tracked", not_tracked,
                             "untracked (good)" if not_tracked else "TRACKED — remove it!"))
    except FileNotFoundError:
        results.append(check(".env is not git-tracked", False, "git not found"))

    # 6. Test scrape
    print("\n  Running test scrape (Indeed, 'software engineer', 5 results) — may take 10–30s...")
    try:
        from jobspy import scrape_jobs
        jobs = scrape_jobs(
            site_name=["indeed"],
            search_term="software engineer",
            results_wanted=5,
        )
        has_rows = len(jobs) >= 1
        has_cols = all(c in jobs.columns for c in ["title", "company", "job_url"])
        results.append(check("test scrape returns ≥1 row", has_rows, f"{len(jobs)} rows"))
        results.append(check("result has title/company/job_url columns", has_cols))
        if has_rows:
            print("\n  Sample results (title | company | job_url):")
            for _, row in jobs.head(3).iterrows():
                print(f"    - {row.get('title','?')} @ {row.get('company','?')} → {str(row.get('job_url','?'))[:60]}")
    except Exception as e:
        results.append(check("test scrape runs without exception", False, str(e)))

    # Summary
    passed = sum(results)
    total = len(results)
    print(f"\n=== Result: {passed}/{total} checks passed ===")
    if passed == total:
        print("  ✓ Milestone 1 COMPLETE — ready for handoff.\n")
    else:
        print("  ✗ Milestone 1 INCOMPLETE — fix failures above before handoff.\n")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
