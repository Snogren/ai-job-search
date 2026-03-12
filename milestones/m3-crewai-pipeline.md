# Milestone 3 — CrewAI Pipeline

## Tasks

- [x] Define **Job Scorer** agent: reads job description + user criteria; rates relevance 1–10 with one-sentence reasoning
- [x] Define **Skills Advisor** agent: identifies top skills to highlight or acquire based on highest-scored listings
- [x] Wire both agents into a sequential CrewAI `Crew`
- [x] Accept a CSV path from Milestone 2 as input; pass job listings as task context
- [x] Write scored output to `output/scored_YYYY-MM-DD.json`

## Result — 5/5 PASS (2026-03-11)

```
[PASS] Pipeline runs end-to-end
[PASS] Job Scorer produces scores (all 5 jobs scored 1–10)
[PASS] Skills Advisor produces output (1710 chars of structured advice)
[PASS] LLM key loaded from .env (OPENROUTER_API_KEY via load_dotenv)
[PASS] Output file is valid JSON
```

Notes:
- Model: `openrouter/stepfun/step-3.5-flash:free` (thinking model using `<think>` tags)
- Added `strip_thinking_tags()` to handle orphaned `</think>` tails in captured output
- `litellm` package required separately (`pip install crewai[litellm]`)

## Quality Gate

All of the following must be true before handoff:

1. **Pipeline runs end-to-end** — `python src/pipeline.py --input output/jobs_raw_YYYY-MM-DD.csv` completes without exception
2. **Job Scorer produces scores** — every processed job has a numeric `score` (1–10) and a `reasoning` string in the output
3. **Skills Advisor produces output** — output includes a `skills_advice` section with ≥2 skill recommendations
4. **LLM key loaded from `.env`** — no API key appears in any source file; `load_dotenv()` is called before any LLM client is instantiated
5. **Output file is valid JSON** — `python -c "import json; json.load(open('output/scored_YYYY-MM-DD.json'))"` exits 0

## Handoff Artifact

- `src/pipeline.py` source
- Sample `output/scored_YYYY-MM-DD.json` with ≥5 scored jobs (real or anonymized)
