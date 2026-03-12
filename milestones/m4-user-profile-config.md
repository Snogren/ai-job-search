# Milestone 4 — User Profile Config

## Tasks

- [x] Create `config.yaml`: desired roles, required skills, preferred locations, salary range, remote preference
- [x] Load `config.yaml` at runtime in both `scraper.py` and `pipeline.py`
- [x] Pass user profile fields as context to both CrewAI agents
- [x] Ensure only secrets remain in `.env`; all non-secret preferences move to `config.yaml`
- [x] Add `config.example.yaml` with placeholder values (safe to commit)

## Result — 7/7 PASS (2026-03-11)

```
[PASS] .env contains only secrets (OPENROUTER_API_KEY, CREWAI_MODEL)
[PASS] config.yaml contains no secrets
[PASS] config.example.yaml exists on disk
[PASS] config.yaml is in .gitignore
[PASS] Criteria string reflects config.yaml roles
[PASS] Criteria string reflects config.yaml skills
[PASS] config.yaml search.term is non-empty
```

Config separation verified: `src/config.py` provides `load_config()` and `build_criteria_string()`.
Both `scraper.py` and `pipeline.py` load config at startup; CLI flags override config values.
Scraper with no flags used `python backend engineer` from config.yaml and returned 23 results.

## Quality Gate

All of the following must be true before handoff:

1. **Clean separation** — `.env` contains only keys/secrets; `config.yaml` contains only user preferences (no secrets)
2. **`config.example.yaml` is committed** — the example file exists in version control with placeholder values and no real data
3. **`config.yaml` is gitignored** — listed in `.gitignore` (user preferences are personal; not everyone wants them committed)
4. **Agents use config** — scoring criteria in the Job Scorer agent visibly reflect the roles/skills listed in `config.yaml` (demonstrated by changing a value and seeing the scores shift)
5. **Scraper uses config** — running `scraper.py` with no CLI flags still works, using `config.yaml` defaults for search term and location

## Handoff Artifact

- `config.example.yaml` (committed)
- Updated `src/scraper.py` and `src/pipeline.py` showing config loading
- Short written note confirming config separation was verified manually
