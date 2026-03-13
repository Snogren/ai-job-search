"""
src/config.py — load config.yaml for the job search pipeline.
"""

import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
CONFIG_EXAMPLE_PATH = PROJECT_ROOT / "config.example.yaml"


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Load config.yaml, falling back to config.example.yaml with a warning."""
    import logging
    log = logging.getLogger(__name__)

    if not path.exists():
        fallback = CONFIG_EXAMPLE_PATH
        if fallback.exists():
            log.warning(
                f"config.yaml not found — using config.example.yaml (placeholder values). "
                f"Copy config.example.yaml to config.yaml and fill in your preferences."
            )
            path = fallback
        else:
            raise FileNotFoundError(
                f"Neither {path} nor {fallback} found. "
                f"Copy config.example.yaml to config.yaml and fill in your preferences."
            )

    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_disqualifiers(config: dict[str, Any]) -> list[str]:
    """Return disqualifying descriptions from config (evaluated by the LLM).
    Reads from criteria.disqualifiers (new location) with fallback to top-level disqualifiers."""
    c = config.get("criteria", {})
    items = c.get("disqualifiers") or config.get("disqualifiers", [])
    return [str(d) for d in items]


def get_qualifiers(config: dict[str, Any]) -> list[str]:
    """Return qualifying descriptions from config (evaluated by the LLM).
    Reads from criteria.qualifiers (new location) with fallback to top-level qualifiers."""
    c = config.get("criteria", {})
    items = c.get("qualifiers") or config.get("qualifiers", [])
    return [str(q) for q in items]


def get_criteria_weights(config: dict[str, Any]) -> dict[str, float]:
    """Return normalized scoring weights from config, defaulting to equal weighting."""
    c = config.get("criteria", {})
    raw = c.get("weights", {})
    defaults = {"relevance": 1/3, "duties": 1/3, "income": 1/3}
    w = {k: float(raw.get(k, defaults[k])) for k in defaults}
    total = sum(w.values())
    if total > 0:
        w = {k: v / total for k, v in w.items()}
    return w


def build_criteria_string(config: dict[str, Any]) -> str:
    """Build per-category scoring instructions for the Job Scorer agent.

    Location is handled as a qualifier (pass/fail), not a scored category.
    Scoring covers relevance, duties, and income only.
    """
    c = config.get("criteria", {})
    weights = get_criteria_weights(config)
    categories = [
        ("Relevance", "relevance", "How well the job title and posting keywords match the target role"),
        ("Duties",    "duties",    "How well the actual responsibilities match the work the candidate wants to do"),
        ("Income",    "income",    "How well the stated or implied salary matches expectations"),
    ]
    parts = []
    for label, key, desc in categories:
        cat = c.get(key, {})
        weight_pct = round(weights.get(key, 1/3) * 100)
        parts.append(f"[{label}] {weight_pct}% weight — {desc}")
        parts.append(f"  Score 10 (ideal): {cat.get('ideal', 'Not specified')}")
        parts.append(f"  Score  1 (worst): {cat.get('worst', 'Not specified')}")
        parts.append("")
    return "\n".join(parts).rstrip()


def get_model_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return model settings from config.yaml, with env var fallbacks for backward compat.

    Non-secret settings (model name, base_url, parallelism) live in config.yaml.
    The API key remains in .env.
    """
    model = cfg.get("model", {})
    return {
        "name": model.get("name") or os.getenv("CREWAI_MODEL", "openrouter/anthropic/claude-3.5-sonnet"),
        "base_url": model.get("base_url", "https://openrouter.ai/api/v1"),
        "parallel_workers": model.get("parallel_workers") or cfg.get("scoring", {}).get("parallel_workers", 10),
    }
