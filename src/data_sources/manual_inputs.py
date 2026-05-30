"""Human/estimated-data layer surfacing YAML assumption blocks and metadata.

This module provides a thin facade over ``config.load_base_assumptions()`` for
the dashboard editable table, provenance display, and confidence-tier filtering.

Many values are LOW-CONFIDENCE scenario assumptions, not facts. Private-credit
figures, AI-specific capex shares, and refinancing probabilities are
particularly speculative. Do not treat point estimates here as ground truth;
use scenario sensitivity instead.

Spec nuances honoured:
- dict-value blocks (e.g. regional maps, financing shares) are serialised to a
  short JSON string in the DataFrame so the cell stays printable.
- Plain-scalar blocks (no 'value' key) get unit='' and confidence='unknown'.
- override_value performs a deep copy; nothing is ever written to disk.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pandas as pd

from .. import config
from ..utils.logging import get_logger, log_assumption_used, log_missing_data

_logger = get_logger("ai_dc_model.manual_inputs")

# Columns emitted by list_assumptions()
_COLS = ["key", "value", "unit", "confidence", "source_note", "last_updated", "rationale"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _serialise_value(v: Any) -> Any:
    """Return a printable scalar from a block value; dicts -> short JSON string."""
    if isinstance(v, dict):
        return json.dumps(v, separators=(",", ":"))
    return v


def _parse_block(key: str, block: Any) -> dict[str, Any]:
    """Normalise one raw YAML block into a flat row dict."""
    if isinstance(block, dict) and "value" in block:
        return {
            "key": key,
            "value": _serialise_value(block.get("value")),
            "unit": block.get("unit", ""),
            "confidence": block.get("confidence", "unknown"),
            "source_note": block.get("source_note", ""),
            "last_updated": block.get("last_updated", ""),
            "rationale": block.get("rationale", ""),
        }
    # Plain scalar (no structured block)
    return {
        "key": key,
        "value": _serialise_value(block),
        "unit": "",
        "confidence": "unknown",
        "source_note": "",
        "last_updated": "",
        "rationale": "",
    }


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def list_assumptions() -> pd.DataFrame:
    """Return a DataFrame with one row per assumption block.

    Columns: key, value, unit, confidence, source_note, last_updated, rationale.
    Dict values (e.g. regional price maps, financing shares) are serialised to a
    compact JSON string so the cell is always a printable scalar.
    """
    assumptions = config.load_base_assumptions()
    if not assumptions:
        log_missing_data(_logger, what="base_assumptions.yaml", fallback="empty DataFrame")
        return pd.DataFrame(columns=_COLS)

    rows = [_parse_block(k, v) for k, v in assumptions.items()]
    df = pd.DataFrame(rows, columns=_COLS)

    # Log usage for audit trail
    for _, row in df.iterrows():
        log_assumption_used(
            _logger,
            key=row["key"],
            value=row["value"],
            confidence=str(row["confidence"]),
        )

    return df


def get_assumption(key: str) -> dict[str, Any]:
    """Return the full raw block for *key*, or an empty dict if not found."""
    assumptions = config.load_base_assumptions()
    block = assumptions.get(key)
    if block is None:
        log_missing_data(_logger, what=f"assumption key '{key}'", fallback={})
        return {}
    return dict(block) if isinstance(block, dict) else {"value": block}


def assumptions_by_confidence() -> dict[str, list[str]]:
    """Return mapping of confidence level -> list of keys at that level.

    Levels are low, medium, high, and unknown (for plain-scalar blocks or any
    block missing the 'confidence' field).
    """
    assumptions = config.load_base_assumptions()
    result: dict[str, list[str]] = {"low": [], "medium": [], "high": [], "unknown": []}
    for key, block in assumptions.items():
        if isinstance(block, dict) and "value" in block:
            level = str(block.get("confidence", "unknown")).lower()
        else:
            level = "unknown"
        bucket = result.setdefault(level, [])
        bucket.append(key)
    return result


def low_confidence_keys() -> list[str]:
    """Return keys whose confidence is 'low' (or 'LOW').

    These values carry the highest uncertainty and should be treated as scenario
    levers rather than calibrated facts.
    """
    return assumptions_by_confidence().get("low", [])


def provenance_note(key: str) -> str:
    """Return a single-line provenance string for display.

    Format: ``<key> = <value> [<confidence>] — <source_note>``
    """
    block = get_assumption(key)
    if not block:
        return f"{key} = <missing>"
    if "value" in block:
        val = _serialise_value(block["value"])
        confidence = block.get("confidence", "unknown")
        source_note = block.get("source_note", "")
        return f"{key} = {val} [{confidence}] — {source_note}"
    # Plain scalar stored under synthetic 'value' key
    val = _serialise_value(block.get("value", "<missing>"))
    return f"{key} = {val} [unknown] — "


def override_value(assumptions: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    """Return a deep copy of *assumptions* with block *key* 's value replaced.

    If the block is a structured dict (has a 'value' key), only the 'value'
    field is updated so that unit/confidence/source_note metadata is preserved.
    If the block is a plain scalar, it is replaced wholesale.

    Does NOT write anything to disk.
    """
    result = copy.deepcopy(assumptions)
    if key not in result:
        # Create a minimal plain-scalar block for a new override
        result[key] = value
        return result

    block = result[key]
    if isinstance(block, dict) and "value" in block:
        block["value"] = value
    else:
        result[key] = value
    return result
