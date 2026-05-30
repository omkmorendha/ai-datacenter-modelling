"""Logging helpers with explicit, structured warnings for data quality.

The model leans heavily on assumptions and best-effort remote data, so it must
be loud about *why* a number is what it is. These helpers give consistent,
greppable log lines for the situations the spec calls out:

  - missing data
  - stale data
  - assumptions used instead of observed data
  - API failures
  - scenario results
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

_CONFIGURED = False


def get_logger(name: str = "ai_dc_model") -> logging.Logger:
    """Return a configured logger (idempotent)."""
    global _CONFIGURED
    logger = logging.getLogger(name)
    if not _CONFIGURED:
        level = os.environ.get("AI_DC_LOG_LEVEL", "INFO").upper()
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        root = logging.getLogger("ai_dc_model")
        root.setLevel(getattr(logging, level, logging.INFO))
        if not root.handlers:
            root.addHandler(handler)
        root.propagate = False
        _CONFIGURED = True
    return logger


# --------------------------------------------------------------------------- #
# Structured, greppable data-quality events
# --------------------------------------------------------------------------- #
def log_missing_data(logger: logging.Logger, *, what: str, fallback: Any = None) -> None:
    logger.warning("MISSING_DATA | %s | falling back to: %r", what, fallback)


def log_stale_data(
    logger: logging.Logger, *, what: str, as_of: str, age_days: float
) -> None:
    logger.warning(
        "STALE_DATA | %s | as_of=%s | age_days=%.1f", what, as_of, age_days
    )


def log_assumption_used(
    logger: logging.Logger, *, key: str, value: Any, confidence: str = "unknown"
) -> None:
    logger.info(
        "ASSUMPTION_USED | %s = %r | confidence=%s", key, value, confidence
    )


def log_api_failure(
    logger: logging.Logger, *, source: str, detail: str, fallback: str = "cache/assumptions"
) -> None:
    logger.error(
        "API_FAILURE | source=%s | %s | falling back to %s", source, detail, fallback
    )


def log_scenario_result(
    logger: logging.Logger, *, scenario: str, summary: dict[str, Any]
) -> None:
    parts = " ".join(f"{k}={v}" for k, v in summary.items())
    logger.info("SCENARIO_RESULT | %s | %s", scenario, parts)
