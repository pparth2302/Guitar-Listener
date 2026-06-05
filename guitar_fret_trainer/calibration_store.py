"""JSON-backed calibration storage for Guitar Fret Trainer.

The app originally stored a plain list of single string/fret entries. This
module migrates that old shape into separate sections so chord fingerprints and
single-note calibrations never get mixed.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from music_theory import frequency_from_note_info, get_note_from_string_and_fret

DATA_FILE = Path(__file__).with_name("calibration_data.json")
DATA_VERSION = 2
DEFAULT_SETTINGS = {
    "sample_rate": 44100,
    "buffer_size": 2048,
    "hop_size": 512,
    "rms_threshold": 0.01,
}

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
        _write_data(_empty_store(), path)


def load_calibration(path: Path = DATA_FILE) -> dict[str, list[dict[str, Any]]]:
    """Load and automatically migrate calibration data."""
    ensure_store(path)
    try:
        raw_data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = path.with_name(f"{path.stem}.invalid-{int(time.time())}{path.suffix}")
        path.replace(backup)
        migrated = _empty_store()
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


def delete_single_note_entry(
    string_label: str,
    note: str,
    fret: int,
    path: Path = DATA_FILE,
) -> dict[str, list[dict[str, Any]]]:
    """Delete one single string/fret calibration row."""
    data = load_calibration(path)
    data["single_notes"] = [
        entry
        for entry in data["single_notes"]
        if not (
            entry["string"] == string_label
            and entry["note"] == note
            and int(entry["fret"]) == int(fret)
        )
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
    data = _empty_store()
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


def _empty_store() -> dict[str, Any]:
    return {
        "version": DATA_VERSION,
        "single_notes": [],
        "chords": [],
        "settings": dict(DEFAULT_SETTINGS),
    }


def _migrate_data(raw_data: Any) -> tuple[dict[str, Any], bool]:
    changed = False

    if isinstance(raw_data, list):
        single_notes = raw_data
        chords: list[Any] = []
        settings = dict(DEFAULT_SETTINGS)
        changed = True
    elif isinstance(raw_data, dict):
        single_notes = raw_data.get("single_notes", raw_data.get("entries", []))
        chords = raw_data.get("chords", [])
        settings = _normalize_settings(raw_data.get("settings", {}))
        if raw_data.get("version") != DATA_VERSION:
            changed = True
    else:
        single_notes = []
        chords = []
        settings = dict(DEFAULT_SETTINGS)
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
        "version": DATA_VERSION,
        "single_notes": _sort_single_notes(normalized_single),
        "chords": _sort_chords(normalized_chords),
        "settings": settings,
    }

    if not changed:
        changed = data != raw_data
    return data, changed


def _normalize_settings(settings: Any) -> dict[str, Any]:
    normalized = dict(DEFAULT_SETTINGS)
    if not isinstance(settings, dict):
        return normalized

    for key, default in DEFAULT_SETTINGS.items():
        value = settings.get(key, default)
        try:
            numeric = type(default)(value)
        except (TypeError, ValueError):
            numeric = default
        if isinstance(numeric, (int, float)) and numeric <= 0:
            numeric = default
        normalized[key] = numeric
    return normalized


def _normalize_single_note(entry: dict[str, Any]) -> dict[str, Any] | None:
    try:
        string_label = str(entry["string"])
        note = str(entry["note"])
        fret = int(entry["fret"])
        timestamp = str(entry.get("timestamp", ""))
    except (KeyError, TypeError, ValueError):
        return None

    frequency_hz = _read_float(
        entry.get("frequency_hz", entry.get("average_frequency_hz")),
        default=0.0,
        digits=2,
    )
    if frequency_hz <= 0:
        return None

    average_frequency_hz = _read_float(entry.get("average_frequency_hz"), default=frequency_hz, digits=2)
    median_frequency_hz = _read_float(entry.get("median_frequency_hz"), default=frequency_hz, digits=2)
    std_dev_value = _read_optional_float(entry.get("std_dev"), digits=3)
    rms_avg = _read_optional_float(entry.get("rms_avg"), digits=5)
    pitch_confidence_avg = _read_optional_float(entry.get("pitch_confidence_avg"), digits=3)
    sample_count = _read_int(entry.get("sample_count"), default=0)

    fundamental_hz, harmonic_ratio = _derive_fundamental_frequency(
        entry,
        detected_frequency_hz=frequency_hz,
        string_label=string_label,
        fret=fret,
    )
    fingerprint = _normalize_single_fingerprint(
        entry.get("fingerprint"),
        detected_frequency_hz=frequency_hz,
        fundamental_frequency_hz=fundamental_hz,
        harmonic_ratio=harmonic_ratio,
        string_label=string_label,
        fret=fret,
    )
    quality = _normalize_quality(entry.get("quality"), harmonic_ratio=harmonic_ratio)

    return {
        "string": string_label,
        "note": note,
        "fret": fret,
        "frequency_hz": frequency_hz,
        "average_frequency_hz": average_frequency_hz,
        "median_frequency_hz": median_frequency_hz,
        "std_dev": std_dev_value,
        "rms_avg": rms_avg,
        "pitch_confidence_avg": pitch_confidence_avg,
        "sample_count": sample_count,
        "fingerprint": fingerprint,
        "quality": quality,
        "timestamp": timestamp,
    }


def _derive_fundamental_frequency(
    entry: dict[str, Any],
    *,
    detected_frequency_hz: float,
    string_label: str,
    fret: int,
) -> tuple[float, int]:
    fingerprint = entry.get("fingerprint")
    if isinstance(fingerprint, dict):
        saved_fundamental = _read_float(
            fingerprint.get("fundamental_frequency_hz"),
            default=0.0,
            digits=2,
        )
        saved_ratio = _read_int(fingerprint.get("harmonic_ratio_used"), default=1)
        if saved_fundamental > 0:
            return saved_fundamental, max(1, saved_ratio)

    note_info = get_note_from_string_and_fret(string_label, fret)
    expected_hz = frequency_from_note_info(note_info)
    if not expected_hz:
        return detected_frequency_hz, 1

    candidates = [
        (detected_frequency_hz, 1),
        (detected_frequency_hz / 2.0, 2),
        (detected_frequency_hz / 3.0, 3),
        (detected_frequency_hz / 4.0, 4),
        (detected_frequency_hz * 2.0, -2),
    ]
    corrected_hz, ratio = min(
        candidates,
        key=lambda item: abs(_cents_between(item[0], expected_hz)),
    )
    return round(float(corrected_hz), 2), ratio


def _normalize_single_fingerprint(
    fingerprint: Any,
    *,
    detected_frequency_hz: float,
    fundamental_frequency_hz: float,
    harmonic_ratio: int,
    string_label: str,
    fret: int,
) -> dict[str, Any]:
    note_info = get_note_from_string_and_fret(string_label, fret)
    if not isinstance(fingerprint, dict):
        fingerprint = {}

    harmonic_peaks = _normalize_frequency_list(fingerprint.get("harmonic_peaks_hz"))
    if not harmonic_peaks:
        harmonic_peaks = _normalize_frequency_list(fingerprint.get("dominant_frequencies"))

    harmonic_strengths = fingerprint.get("harmonic_strengths", [])
    if isinstance(harmonic_strengths, dict):
        normalized_strengths: Any = {
            str(key): _read_float(value, default=0.0, digits=3)
            for key, value in harmonic_strengths.items()
        }
    elif isinstance(harmonic_strengths, list):
        normalized_strengths = [
            _read_float(value, default=0.0, digits=3)
            for value in harmonic_strengths[:12]
        ]
    else:
        normalized_strengths = []

    source = str(fingerprint.get("source", "recorded" if fingerprint else "legacy_migration"))
    return {
        "fundamental_frequency_hz": round(float(fundamental_frequency_hz), 2),
        "detected_frequency_hz": round(float(detected_frequency_hz), 2),
        "harmonic_ratio_used": harmonic_ratio,
        "harmonic_peaks_hz": harmonic_peaks[:12],
        "harmonic_strengths": normalized_strengths,
        "spectral_centroid": _read_float(
            fingerprint.get("spectral_centroid", fingerprint.get("spectral_centroid_avg")),
            default=0.0,
            digits=3,
        ),
        "spectral_bandwidth": _read_float(fingerprint.get("spectral_bandwidth"), default=0.0, digits=3),
        "rms_energy": _read_float(fingerprint.get("rms_energy", fingerprint.get("rms")), default=0.0, digits=5),
        "zero_crossing_rate": _read_float(fingerprint.get("zero_crossing_rate"), default=0.0, digits=5),
        "chroma_vector": _normalize_chroma_vector(fingerprint.get("chroma_vector", [])),
        "attack_time_ms": _read_float(fingerprint.get("attack_time_ms"), default=0.0, digits=2),
        "decay_profile": _normalize_decay_profile(fingerprint.get("decay_profile")),
        "string_name": str(fingerprint.get("string_name", string_label)),
        "fret": _read_int(fingerprint.get("fret"), default=fret),
        "note_name": str(fingerprint.get("note_name", note_info.get("full_note", ""))),
        "source": source,
        "needs_recalibration": bool(fingerprint.get("needs_recalibration", source == "legacy_migration")),
    }


def _normalize_quality(quality: Any, *, harmonic_ratio: int) -> dict[str, Any]:
    warnings: list[str] = []
    if isinstance(quality, dict):
        raw_warnings = quality.get("warnings", [])
        if isinstance(raw_warnings, list):
            warnings.extend(str(item) for item in raw_warnings if str(item).strip())

    if abs(harmonic_ratio) != 1:
        warning = (
            "Saved detected pitch appears to be a harmonic; "
            "the derived fundamental is used for matching."
        )
        if warning not in warnings:
            warnings.append(warning)

    return {
        "warnings": warnings,
        "needs_recalibration": bool(warnings),
    }


def _normalize_frequency_list(values: Any) -> list[float]:
    if not isinstance(values, list):
        return []

    frequencies: list[float] = []
    for value in values:
        frequency = _read_float(value, default=0.0, digits=2)
        if frequency > 0:
            frequencies.append(frequency)
    return frequencies


def _normalize_decay_profile(values: Any) -> list[float]:
    if not isinstance(values, list):
        return []
    return [_read_float(value, default=0.0, digits=3) for value in values[:12]]


def _read_float(value: Any, *, default: float, digits: int) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = default
    if not math.isfinite(numeric):
        numeric = default
    return round(numeric, digits)


def _read_optional_float(value: Any, *, digits: int) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return round(numeric, digits)


def _read_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _cents_between(frequency_hz: float, reference_hz: float) -> float:
    if frequency_hz <= 0 or reference_hz <= 0:
        return float("inf")
    return 1200.0 * math.log2(frequency_hz / reference_hz)


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
            "spectral_bandwidth": _read_float(
                fingerprint.get("spectral_bandwidth"),
                default=0.0,
                digits=3,
            ),
            "rms": _read_float(fingerprint.get("rms"), default=0.0, digits=5),
            "zero_crossing_rate": _read_float(
                fingerprint.get("zero_crossing_rate"),
                default=0.0,
                digits=5,
            ),
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


def _write_data(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)
