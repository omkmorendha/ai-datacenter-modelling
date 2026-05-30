"""Central configuration: paths, environment, and assumption/scenario loading.

Everything that needs to find a file, read an env var, or load the YAML
assumption/scenario layer goes through here so the rest of the package never
hard-codes a path.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

try:  # optional: load a .env if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
# config.py lives at src/config.py -> repo root is two parents up.
REPO_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = REPO_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
ASSUMPTIONS_DIR = DATA_DIR / "assumptions"

BASE_ASSUMPTIONS_PATH = ASSUMPTIONS_DIR / "base_assumptions.yaml"
SCENARIOS_PATH = ASSUMPTIONS_DIR / "scenarios.yaml"
ENTITY_UNIVERSE_PATH = ASSUMPTIONS_DIR / "entity_universe.yaml"


def cache_dir() -> Path:
    """On-disk HTTP cache directory (override via AI_DC_CACHE_DIR)."""
    d = os.environ.get("AI_DC_CACHE_DIR")
    path = Path(d) if d else RAW_DIR / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------- #
# Environment / API keys (all optional for v0)
# --------------------------------------------------------------------------- #
def env(key: str, default: str | None = None) -> str | None:
    val = os.environ.get(key, default)
    if val is not None and val.strip() == "":
        return default
    return val


FRED_API_KEY = env("FRED_API_KEY")
EIA_API_KEY = env("EIA_API_KEY")
SEC_USER_AGENT = env(
    "SEC_USER_AGENT", "ai-datacenter-macro-model contact@example.com"
)


def cache_ttl_hours() -> float:
    try:
        return float(env("AI_DC_CACHE_TTL_HOURS", "24") or 24)
    except ValueError:
        return 24.0


def is_offline() -> bool:
    return str(env("AI_DC_OFFLINE", "0")).strip().lower() in {"1", "true", "yes"}


# --------------------------------------------------------------------------- #
# YAML loading (cached)
# --------------------------------------------------------------------------- #
def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Required YAML not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data or {}


@lru_cache(maxsize=1)
def load_base_assumptions() -> dict[str, Any]:
    return _read_yaml(BASE_ASSUMPTIONS_PATH)


@lru_cache(maxsize=1)
def load_scenarios() -> dict[str, Any]:
    data = _read_yaml(SCENARIOS_PATH)
    return data.get("scenarios", data)


@lru_cache(maxsize=1)
def load_entity_universe() -> dict[str, Any]:
    return _read_yaml(ENTITY_UNIVERSE_PATH)


def clear_caches() -> None:
    """Drop cached YAML (useful after edits in the dashboard / notebooks)."""
    load_base_assumptions.cache_clear()
    load_scenarios.cache_clear()
    load_entity_universe.cache_clear()


# --------------------------------------------------------------------------- #
# Assumption accessor
# --------------------------------------------------------------------------- #
def assumption_value(key: str, default: Any = None) -> Any:
    """Return the ``value`` field of a structured assumption block.

    Base assumptions are stored as blocks with value/unit/confidence/etc.
    This unwraps the ``value`` (or returns a plain scalar if it isn't a block).
    """
    block = load_base_assumptions().get(key)
    if block is None:
        return default
    if isinstance(block, dict) and "value" in block:
        return block["value"]
    return block
