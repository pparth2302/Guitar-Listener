"""JSON-backed calibration storage for Guitar Fret Trainer."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

DATA_FILE = Path(__file__).with_name("calibration_data.json")

STRING_ORDER = {
    "Low E": 0,
    "A": 1,
    "D": 2,
    "G": 3,
    "B": 4,
    "High E": 5,
}


def ensure_store(path: Path = DATA_FILE) -> None:
    """Create the calibration JSON file when it does not exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("[]\n", encoding="utf-8")


def load_calibration(path: Path = DATA_FILE) -> list[dict[str, Any]]:
    """Load calibration rows, returning an empty list on missing/corrupt data."""
    ensure_store(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = path.with_name(f"{path.stem}.invalid-{int(time.time())}{path.suffix}")
        path.replace(backup)
        path.write_text("[]\n", encoding="utf-8")
        return []

    if not isinstance(data, list):
        path.write_text("[]\n", encoding="utf-8")
        return []

    entries = [_normalize_entry(entry) for entry in data if isinstance(entry, dict)]
    return _sort_entries([entry for entry in entries if entry is not None])


def save_calibration_entry(
    entry: dict[str, Any],
    path: Path = DATA_FILE,
) -> list[dict[str, Any]]:
    """Add or replace a calibration entry and return the full sorted table."""
    normalized = _normalize_entry(entry)
    if normalized is None:
        raise ValueError("Calibration entry is missing required fields.")

    entries = load_calibration(path)
    entries = [
        existing
        for existing in entries
        if not (
            existing["string"] == normalized["string"]
            and existing["note"] == normalized["note"]
            and existing["fret"] == normalized["fret"]
        )
    ]
    entries.append(normalized)
    entries = _sort_entries(entries)
    _write_entries(entries, path)
    return entries


def reset_calibration(path: Path = DATA_FILE) -> list[dict[str, Any]]:
    """Clear all saved calibration rows."""
    ensure_store(path)
    _write_entries([], path)
    return []


def _normalize_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    try:
        string_label = str(entry["string"])
        note = str(entry["note"])
        fret = int(entry["fret"])
        frequency_hz = round(float(entry["frequency_hz"]), 2)
        timestamp = str(entry.get("timestamp", ""))
    except (KeyError, TypeError, ValueError):
        return None

    if frequency_hz <= 0:
        return None

    return {
        "string": string_label,
        "note": note,
        "fret": fret,
        "frequency_hz": frequency_hz,
        "timestamp": timestamp,
    }


def _sort_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        entries,
        key=lambda entry: (
            STRING_ORDER.get(entry["string"], 999),
            int(entry["fret"]),
            entry["note"],
        ),
    )


def _write_entries(entries: list[dict[str, Any]], path: Path) -> None:
    ensure_store(path)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)
