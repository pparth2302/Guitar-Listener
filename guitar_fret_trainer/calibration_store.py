"""JSON-backed calibration storage for Guitar Fret Trainer.

The app originally stored a plain list of single string/fret entries. This
module migrates that old shape into separate sections so chord fingerprints and
single-note calibrations never get mixed.
"""

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
        _write_data({"single_notes": [], "chords": []}, path)


def load_calibration(path: Path = DATA_FILE) -> dict[str, list[dict[str, Any]]]:
    """Load and automatically migrate calibration data."""
    ensure_store(path)
    try:
        raw_data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = path.with_name(f"{path.stem}.invalid-{int(time.time())}{path.suffix}")
        path.replace(backup)
        migrated = {"single_notes": [], "chords": []}
        _write_data(migrated, path)
        return migrated

    data, changed = _migrate_data(raw_data)
    if changed:
        _write_data(data, path)
    return data


def load_single_notes(path: Path = DATA_FILE) -> list[dict[str, Any]]:
    return load_calibration(path)["single_notes"]


def load_chords(path: Path = DATA_FILE) -> list[dict[str, Any]]:
    return load_calibration(path)["chords"]


def save_single_note_entry(
    entry: dict[str, Any],
    path: Path = DATA_FILE,
) -> dict[str, list[dict[str, Any]]]:
    """Add or replace a single string/fret calibration row."""
    normalized = _normalize_single_note(entry)
    if normalized is None:
        raise ValueError("Single-note calibration entry is missing required fields.")

    data = load_calibration(path)
    data["single_notes"] = [
        existing
        for existing in data["single_notes"]
        if not (
            existing["string"] == normalized["string"]
            and existing["note"] == normalized["note"]
            and existing["fret"] == normalized["fret"]
        )
    ]
    data["single_notes"].append(normalized)
    data["single_notes"] = _sort_single_notes(data["single_notes"])
    _write_data(data, path)
    return data


def save_chord_entry(
    entry: dict[str, Any],
    path: Path = DATA_FILE,
) -> dict[str, list[dict[str, Any]]]:
    """Add or replace a chord fingerprint by chord name."""
    normalized = _normalize_chord(entry)
    if normalized is None:
        raise ValueError("Chord calibration entry is missing required fields.")

    data = load_calibration(path)
    chord_key = normalized["chord_name"].casefold()
    data["chords"] = [
        existing
        for existing in data["chords"]
        if existing["chord_name"].casefold() != chord_key
    ]
    data["chords"].append(normalized)
    data["chords"] = _sort_chords(data["chords"])
    _write_data(data, path)
    return data


def delete_chord_entry(
    chord_name: str,
    path: Path = DATA_FILE,
) -> dict[str, list[dict[str, Any]]]:
    data = load_calibration(path)
    chord_key = chord_name.strip().casefold()
    data["chords"] = [
        chord for chord in data["chords"] if chord["chord_name"].casefold() != chord_key
    ]
    _write_data(data, path)
    return data


def reset_single_notes(path: Path = DATA_FILE) -> dict[str, list[dict[str, Any]]]:
    data = load_calibration(path)
    data["single_notes"] = []
    _write_data(data, path)
    return data


def reset_chords(path: Path = DATA_FILE) -> dict[str, list[dict[str, Any]]]:
    data = load_calibration(path)
    data["chords"] = []
    _write_data(data, path)
    return data


def reset_all(path: Path = DATA_FILE) -> dict[str, list[dict[str, Any]]]:
    data = {"single_notes": [], "chords": []}
    _write_data(data, path)
    return data


# Backward-compatible names used by the first version of app.py.
def save_calibration_entry(
    entry: dict[str, Any],
    path: Path = DATA_FILE,
) -> list[dict[str, Any]]:
    return save_single_note_entry(entry, path)["single_notes"]


def reset_calibration(path: Path = DATA_FILE) -> list[dict[str, Any]]:
    return reset_single_notes(path)["single_notes"]


def _migrate_data(raw_data: Any) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    changed = False

    if isinstance(raw_data, list):
        single_notes = raw_data
        chords: list[Any] = []
        changed = True
    elif isinstance(raw_data, dict):
        single_notes = raw_data.get("single_notes", raw_data.get("entries", []))
        chords = raw_data.get("chords", [])
    else:
        single_notes = []
        chords = []
        changed = True

    if not isinstance(single_notes, list):
        single_notes = []
        changed = True
    if not isinstance(chords, list):
        chords = []
        changed = True

    normalized_single = [
        note
        for note in (_normalize_single_note(entry) for entry in single_notes if isinstance(entry, dict))
        if note is not None
    ]
    normalized_chords = [
        chord
        for chord in (_normalize_chord(entry) for entry in chords if isinstance(entry, dict))
        if chord is not None
    ]

    data = {
        "single_notes": _sort_single_notes(normalized_single),
        "chords": _sort_chords(normalized_chords),
    }

    if not changed:
        changed = data != raw_data
    return data, changed


def _normalize_single_note(entry: dict[str, Any]) -> dict[str, Any] | None:
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

    std_dev = entry.get("std_dev")
    try:
        std_dev_value = round(float(std_dev), 3) if std_dev is not None else None
    except (TypeError, ValueError):
        std_dev_value = None

    return {
        "string": string_label,
        "note": note,
        "fret": fret,
        "frequency_hz": frequency_hz,
        "std_dev": std_dev_value,
        "timestamp": timestamp,
    }


def _normalize_chord(entry: dict[str, Any]) -> dict[str, Any] | None:
    try:
        chord_name = str(entry["chord_name"]).strip()
        fingerprint = entry["fingerprint"]
    except KeyError:
        return None

    if not chord_name or not isinstance(fingerprint, dict):
        return None

    expected_notes = entry.get("expected_notes", [])
    if not isinstance(expected_notes, list):
        expected_notes = []

    pitch_classes = fingerprint.get("pitch_classes", [])
    if not isinstance(pitch_classes, list):
        pitch_classes = []

    strengths = fingerprint.get("pitch_class_strengths", {})
    if not isinstance(strengths, dict):
        strengths = {}

    chroma_vector = _normalize_chroma_vector(fingerprint.get("chroma_vector", []))
    dominant_frequencies = []
    for value in fingerprint.get("dominant_frequencies", []):
        try:
            frequency = round(float(value), 2)
        except (TypeError, ValueError):
            continue
        if frequency > 0:
            dominant_frequencies.append(frequency)

    try:
        spectral_centroid_avg = round(float(fingerprint.get("spectral_centroid_avg", 0.0)), 3)
    except (TypeError, ValueError):
        spectral_centroid_avg = 0.0

    try:
        confidence_baseline = round(float(fingerprint.get("confidence_baseline", 0.0)), 3)
    except (TypeError, ValueError):
        confidence_baseline = 0.0

    normalized_expected = [str(note).strip() for note in expected_notes if str(note).strip()]
    normalized_pitch_classes = [str(note).strip() for note in pitch_classes if str(note).strip()]
    normalized_strengths = {}
    for key, value in strengths.items():
        try:
            normalized_strengths[str(key)] = round(float(value), 3)
        except (TypeError, ValueError):
            continue

    return {
        "chord_name": chord_name,
        "root": str(entry.get("root", _infer_root(chord_name))),
        "expected_notes": normalized_expected,
        "fingerprint": {
            "dominant_frequencies": dominant_frequencies[:12],
            "pitch_classes": normalized_pitch_classes,
            "pitch_class_strengths": normalized_strengths,
            "spectral_centroid_avg": spectral_centroid_avg,
            "chroma_vector": chroma_vector,
            "confidence_baseline": confidence_baseline,
        },
        "timestamp": str(entry.get("timestamp", "")),
    }


def _normalize_chroma_vector(values: Any) -> list[float]:
    if not isinstance(values, list):
        return [0.0] * 12

    normalized: list[float] = []
    for value in values[:12]:
        try:
            normalized.append(round(float(value), 6))
        except (TypeError, ValueError):
            normalized.append(0.0)

    while len(normalized) < 12:
        normalized.append(0.0)
    return normalized


def _infer_root(chord_name: str) -> str:
    name = chord_name.strip()
    if len(name) >= 2 and name[1] == "#":
        return name[:2]
    return name[:1]


def _sort_single_notes(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        entries,
        key=lambda entry: (
            STRING_ORDER.get(entry["string"], 999),
            int(entry["fret"]),
            entry["note"],
        ),
    )


def _sort_chords(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda entry: entry["chord_name"].casefold())


def _write_data(data: dict[str, list[dict[str, Any]]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)
