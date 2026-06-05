"""Flask + Socket.IO entry point for Guitar Fret Trainer."""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from typing import Any

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit

from audio_engine import AudioEngine, compare_chroma_vectors, cents_between
from calibration_store import (
    delete_chord_entry,
    delete_single_note_entry,
    ensure_store,
    load_calibration,
    reset_all,
    reset_chords,
    reset_single_notes,
    save_chord_entry,
    save_single_note_entry,
)
from music_theory import frequency_from_note_info, get_note_from_string_and_fret, note_from_frequency

GUITAR_STRINGS = [
    {"label": "Low E", "note": "E2"},
    {"label": "A", "note": "A2"},
    {"label": "D", "note": "D3"},
    {"label": "G", "note": "G3"},
    {"label": "B", "note": "B3"},
    {"label": "High E", "note": "E4"},
]

FRETS = list(range(13))
STRING_ORDER = {item["label"]: index for index, item in enumerate(GUITAR_STRINGS)}
EQUIVALENT_POSITION_TIE_CENTS = 2.0

COMMON_CHORDS = {
    "Em": ["E", "G", "B"],
    "E": ["E", "G#", "B"],
    "Am": ["A", "C", "E"],
    "A": ["A", "C#", "E"],
    "A9": ["A", "C#", "E", "G", "B"],
    "C": ["C", "E", "G"],
    "G": ["G", "B", "D"],
    "D": ["D", "F#", "A"],
    "Dm": ["D", "F", "A"],
    "F": ["F", "A", "C"],
    "B7": ["B", "D#", "F#", "A"],
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("guitar_fret_trainer")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "guitar-fret-trainer-local")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
audio_engine = AudioEngine(logger=logger)


@app.route("/")
def index() -> str:
    return render_template("index.html")


@app.get("/api/calibration")
def api_get_calibration() -> Any:
    data = load_calibration()
    return jsonify(
        {
            "ok": True,
            "version": data.get("version", 1),
            "settings": data.get("settings", {}),
            "single_notes": data["single_notes"],
            "chords": data["chords"],
            "entries": data["single_notes"],  # Backward-compatible key.
            "strings": GUITAR_STRINGS,
            "frets": FRETS,
            "common_chords": [
                {"name": name, "expected_notes": notes}
                for name, notes in COMMON_CHORDS.items()
            ],
        }
    )


@app.post("/api/calibration/single/start")
def api_start_single_calibration() -> Any:
    data = request.get_json(silent=True) or {}
    try:
        string_label, note, fret = _validate_single_calibration_request(data)
    except ValueError as exc:
        _emit_error(str(exc), mode="single_calibration")
        return jsonify({"ok": False, "message": str(exc)}), 400

    def on_complete(average_hz: float | None, stats: dict[str, Any]) -> None:
        if average_hz is None:
            _emit_status(
                {
                    "level": "warning",
                    "mode": "single_calibration",
                    "message": stats.get(
                        "message",
                        "Single-note calibration did not capture a stable pitch.",
                    ),
                }
            )
            return

        entry = {
            "string": string_label,
            "note": note,
            "fret": fret,
            "frequency_hz": round(float(average_hz), 2),
            "average_frequency_hz": stats.get("average_frequency_hz", round(float(average_hz), 2)),
            "median_frequency_hz": stats.get("median_frequency_hz", round(float(average_hz), 2)),
            "std_dev": stats.get("std_dev"),
            "rms_avg": stats.get("rms_avg"),
            "pitch_confidence_avg": stats.get("pitch_confidence_avg"),
            "sample_count": stats.get("sample_count", stats.get("samples", 0)),
            "fingerprint": stats.get("fingerprint"),
            "quality": {
                "warnings": [
                    "Detected calibration pitch was corrected as a harmonic."
                ]
                if abs(int(stats.get("harmonic_ratio_used", 1) or 1)) != 1
                else [],
                "needs_recalibration": False,
            },
            "timestamp": _now_iso(),
        }
        calibration = save_single_note_entry(entry)
        socketio.emit(
            "single_calibration_saved",
            {"entry": entry, "single_notes": calibration["single_notes"], "calibration": calibration},
        )
        # Keep the original event name for older browser code.
        socketio.emit(
            "calibration_saved",
            {"entry": entry, "entries": calibration["single_notes"], "calibration": calibration},
        )
        _emit_status(
            {
                "level": "success",
                "mode": "single_calibration",
                "message": (
                    f"Saved {string_label} {note} fret {fret} "
                    f"at {entry['frequency_hz']:.2f} Hz."
                ),
                "samples": stats.get("samples", 0),
            }
        )
        logger.info("Saved single-note calibration entry: %s", entry)

    ok, message = audio_engine.start_single_calibration(
        string_label=string_label,
        note=note,
        fret=fret,
        pitch_callback=_emit_pitch_update,
        completion_callback=on_complete,
        status_callback=_emit_status,
    )
    if not ok:
        _emit_status({"level": "error", "mode": "single_calibration", "message": message})
        status = 409 if "busy" in message.lower() else 503
        return jsonify({"ok": False, "message": message}), status

    return jsonify({"ok": True, "message": message})


# Backward-compatible endpoint from the original single-note-only app.
@app.post("/api/calibration/start")
def api_start_calibration_legacy() -> Any:
    return api_start_single_calibration()


@app.post("/api/calibration/chord/start")
def api_start_chord_calibration() -> Any:
    data = request.get_json(silent=True) or {}
    try:
        chord_name, expected_notes = _validate_chord_calibration_request(data)
    except ValueError as exc:
        _emit_error(str(exc), mode="chord_calibration")
        return jsonify({"ok": False, "message": str(exc)}), 400

    def on_complete(fingerprint: dict[str, Any] | None, stats: dict[str, Any]) -> None:
        if fingerprint is None:
            _emit_status(
                {
                    "level": "warning",
                    "mode": "chord_calibration",
                    "message": stats.get(
                        "message",
                        "Chord calibration did not capture a usable fingerprint.",
                    ),
                }
            )
            return

        entry = {
            "chord_name": chord_name,
            "root": _infer_root(chord_name),
            "expected_notes": expected_notes,
            "fingerprint": fingerprint,
            "timestamp": _now_iso(),
        }
        calibration = save_chord_entry(entry)
        socketio.emit(
            "chord_calibration_saved",
            {"entry": entry, "chords": calibration["chords"], "calibration": calibration},
        )
        _emit_status(
            {
                "level": "success",
                "mode": "chord_calibration",
                "message": (
                    f"Saved {chord_name} chord with pitch classes "
                    f"{', '.join(fingerprint.get('pitch_classes', [])) or 'unknown'}."
                ),
                "samples": stats.get("samples", 0),
            }
        )
        logger.info("Saved chord calibration entry: %s", entry)

    ok, message = audio_engine.start_chord_calibration(
        chord_name=chord_name,
        expected_notes=expected_notes,
        chord_callback=_emit_pitch_update,
        completion_callback=on_complete,
        status_callback=_emit_status,
    )
    if not ok:
        _emit_status({"level": "error", "mode": "chord_calibration", "message": message})
        status = 409 if "busy" in message.lower() else 503
        return jsonify({"ok": False, "message": message}), status

    return jsonify({"ok": True, "message": message})


@app.post("/api/calibration/reset-single")
def api_reset_single() -> Any:
    calibration = reset_single_notes()
    socketio.emit(
        "single_calibration_saved",
        {"entry": None, "single_notes": calibration["single_notes"], "calibration": calibration},
    )
    socketio.emit(
        "calibration_saved",
        {"entry": None, "entries": calibration["single_notes"], "calibration": calibration},
    )
    _emit_status(
        {
            "level": "success",
            "mode": "single_calibration",
            "message": "Single-note calibration data reset.",
        }
    )
    logger.info("Single-note calibration data reset")
    return jsonify({"ok": True, "calibration": calibration})


@app.post("/api/calibration/single/delete")
def api_delete_single() -> Any:
    data = request.get_json(silent=True) or {}
    string_label = str(data.get("string", "")).strip()
    note = str(data.get("note", "")).strip()
    try:
        fret = int(data.get("fret"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Choose a valid fret to delete."}), 400

    if not string_label or not note:
        return jsonify({"ok": False, "message": "Choose a valid single-note row to delete."}), 400

    calibration = delete_single_note_entry(string_label, note, fret)
    socketio.emit(
        "single_calibration_saved",
        {"entry": None, "single_notes": calibration["single_notes"], "calibration": calibration},
    )
    socketio.emit(
        "calibration_saved",
        {"entry": None, "entries": calibration["single_notes"], "calibration": calibration},
    )
    _emit_status(
        {
            "level": "success",
            "mode": "single_calibration",
            "message": f"Deleted {string_label} {note} fret {fret}.",
        }
    )
    return jsonify({"ok": True, "calibration": calibration})


@app.post("/api/calibration/reset-chords")
def api_reset_chords() -> Any:
    calibration = reset_chords()
    socketio.emit(
        "chord_calibration_saved",
        {"entry": None, "chords": calibration["chords"], "calibration": calibration},
    )
    _emit_status(
        {
            "level": "success",
            "mode": "chord_calibration",
            "message": "Chord calibration data reset.",
        }
    )
    logger.info("Chord calibration data reset")
    return jsonify({"ok": True, "calibration": calibration})


@app.post("/api/calibration/reset-all")
def api_reset_all() -> Any:
    calibration = reset_all()
    socketio.emit(
        "single_calibration_saved",
        {"entry": None, "single_notes": calibration["single_notes"], "calibration": calibration},
    )
    socketio.emit(
        "chord_calibration_saved",
        {"entry": None, "chords": calibration["chords"], "calibration": calibration},
    )
    socketio.emit(
        "calibration_saved",
        {"entry": None, "entries": calibration["single_notes"], "calibration": calibration},
    )
    _emit_status(
        {
            "level": "success",
            "mode": "calibration",
            "message": "All calibration data reset.",
        }
    )
    logger.info("All calibration data reset")
    return jsonify({"ok": True, "calibration": calibration})


# Backward-compatible reset endpoint.
@app.post("/api/calibration/reset")
def api_reset_calibration_legacy() -> Any:
    return api_reset_all()


@app.post("/api/calibration/chord/delete")
def api_delete_chord() -> Any:
    data = request.get_json(silent=True) or {}
    chord_name = str(data.get("chord_name", "")).strip()
    if not chord_name:
        return jsonify({"ok": False, "message": "Choose a chord to delete."}), 400

    calibration = delete_chord_entry(chord_name)
    socketio.emit(
        "chord_calibration_saved",
        {"entry": None, "chords": calibration["chords"], "calibration": calibration},
    )
    _emit_status(
        {
            "level": "success",
            "mode": "chord_calibration",
            "message": f"Deleted chord calibration for {chord_name}.",
        }
    )
    return jsonify({"ok": True, "calibration": calibration})


@app.post("/api/detection/start")
def api_start_detection() -> Any:
    data = request.get_json(silent=True) or {}
    string_filter = _validate_string_filter(data.get("string_filter"))
    detection_mode = _validate_detection_mode(data.get("detection_mode"))
    calibration = load_calibration()
    if not calibration["single_notes"] and not calibration["chords"]:
        message = "Add at least one single-note or chord calibration before starting detection."
        _emit_status({"level": "warning", "mode": "detection", "message": message})
        return jsonify({"ok": False, "message": message}), 400

    def on_audio(payload: dict[str, Any]) -> None:
        calibration_snapshot = load_calibration()
        single_result = _build_single_note_result(
            payload,
            calibration_snapshot["single_notes"],
            string_filter=string_filter,
        )
        chord_result = _build_chord_result(payload, calibration_snapshot["chords"])
        combined_result = _build_combined_detection_result(
            single_result,
            chord_result,
            detection_mode=detection_mode,
        )

        _emit_pitch_update(payload)
        socketio.emit("single_note_result", single_result)
        socketio.emit("chord_result", chord_result)
        socketio.emit("detection_result", combined_result)

    ok, message = audio_engine.start_detection(
        pitch_callback=on_audio,
        status_callback=_emit_status,
    )
    if not ok:
        _emit_status({"level": "error", "mode": "detection", "message": message})
        status = 409 if "busy" in message.lower() else 503
        return jsonify({"ok": False, "message": message}), status

    return jsonify({"ok": True, "message": message})


@app.post("/api/detection/stop")
def api_stop_detection() -> Any:
    ok, message = audio_engine.stop_detection()
    _emit_status(
        {
            "level": "success" if ok else "info",
            "mode": "detection",
            "message": message,
        }
    )
    return jsonify({"ok": ok, "message": message})


@app.post("/api/audio/stop")
def api_stop_audio() -> Any:
    ok, message = audio_engine.stop_active_audio()
    _emit_status(
        {
            "level": "success" if ok else "info",
            "mode": "system",
            "message": message,
        }
    )
    return jsonify({"ok": True, "stopped": ok, "message": message})


@socketio.on("connect")
def on_connect() -> None:
    calibration = load_calibration()
    emit(
        "status",
        {
            "level": "success",
            "mode": "system",
            "message": "Browser connected to Guitar Fret Trainer.",
            "timestamp": _now_iso(),
        },
    )
    emit(
        "single_calibration_saved",
        {"entry": None, "single_notes": calibration["single_notes"], "calibration": calibration},
    )
    emit(
        "chord_calibration_saved",
        {"entry": None, "chords": calibration["chords"], "calibration": calibration},
    )
    emit(
        "calibration_saved",
        {"entry": None, "entries": calibration["single_notes"], "calibration": calibration},
    )


def _validate_single_calibration_request(data: dict[str, Any]) -> tuple[str, str, int]:
    string_label = str(data.get("string", "")).strip()
    note = str(data.get("note", "")).strip()
    try:
        fret = int(data.get("fret"))
    except (TypeError, ValueError):
        raise ValueError("Choose a fret from 0 to 12.") from None

    valid_strings = {(item["label"], item["note"]) for item in GUITAR_STRINGS}
    if (string_label, note) not in valid_strings:
        raise ValueError("Choose a valid guitar string.")
    if fret not in FRETS:
        raise ValueError("Choose a fret from 0 to 12.")

    return string_label, note, fret


def _validate_chord_calibration_request(data: dict[str, Any]) -> tuple[str, list[str]]:
    chord_name = str(data.get("chord_name", "")).strip()
    if not chord_name:
        raise ValueError("Enter or choose a chord name.")

    expected_notes = data.get("expected_notes", [])
    if isinstance(expected_notes, str):
        expected_notes = _parse_expected_notes(expected_notes)
    elif isinstance(expected_notes, list):
        expected_notes = [
            str(note).strip().upper().replace("B#", "C").replace("E#", "F")
            for note in expected_notes
            if str(note).strip()
        ]
    else:
        expected_notes = []

    if not expected_notes:
        expected_notes = COMMON_CHORDS.get(chord_name, [])

    return chord_name, expected_notes


def _validate_string_filter(value: Any) -> str | None:
    string_filter = str(value or "").strip()
    if not string_filter or string_filter == "Any string":
        return None
    valid_strings = {item["label"] for item in GUITAR_STRINGS}
    return string_filter if string_filter in valid_strings else None


def _validate_detection_mode(value: Any) -> str:
    mode = str(value or "auto").strip().lower().replace(" ", "_")
    if mode in {"single", "single_note", "single-note"}:
        return "single"
    if mode == "chord":
        return "chord"
    return "auto"


def _parse_expected_notes(value: str) -> list[str]:
    return [
        item.strip().upper().replace("B#", "C").replace("E#", "F")
        for item in value.split(",")
        if item.strip()
    ]


def _build_single_note_result(
    payload: dict[str, Any],
    entries: list[dict[str, Any]],
    *,
    string_filter: str | None = None,
) -> dict[str, Any]:
    pitch_payload = payload.get("single_note", payload)
    frequency_hz = pitch_payload.get("smoothed_frequency_hz") or pitch_payload.get("frequency_hz")
    filtered_entries = [
        entry for entry in entries if string_filter is None or entry.get("string") == string_filter
    ]

    if not filtered_entries:
        filter_text = f" for {string_filter}" if string_filter else ""
        return {
            "status": "no_calibration",
            "message": f"No single-note calibration saved{filter_text} yet.",
            "detected_frequency_hz": round(float(frequency_hz), 2) if frequency_hz else None,
            "frequency": round(float(frequency_hz), 2) if frequency_hz else None,
            "corrected_fundamental_hz": None,
            "harmonic_ratio_used": None,
            "string": None,
            "note": None,
            "octave": None,
            "full_note": "--",
            "fret": None,
            "confidence": 0,
            "cents_error": None,
            "frequency_note": note_from_frequency(frequency_hz),
            "validation_warning": None,
            "alternate_positions": [],
            "ambiguous_match": False,
            "top_matches": [],
            "string_filter": string_filter,
        }

    if not pitch_payload.get("stable") or not frequency_hz:
        return {
            "status": "no_pitch",
            "message": "No clear single note detected",
            "detected_frequency_hz": None,
            "frequency": None,
            "corrected_fundamental_hz": None,
            "harmonic_ratio_used": None,
            "string": None,
            "note": None,
            "octave": None,
            "full_note": "--",
            "fret": None,
            "confidence": 0,
            "cents_error": None,
            "frequency_note": note_from_frequency(frequency_hz),
            "validation_warning": None,
            "alternate_positions": [],
            "ambiguous_match": False,
            "top_matches": [],
            "string_filter": string_filter,
        }

    detected_hz = float(frequency_hz)
    detected_fingerprint = payload.get("chord", {}).get("fingerprint", {})
    match = _select_single_note_match(
        detected_hz,
        filtered_entries,
        detected_fingerprint=detected_fingerprint,
    )
    if match is None:
        return {
            "status": "no_calibration",
            "message": "No usable single-note calibration rows were found.",
            "detected_frequency_hz": round(detected_hz, 2),
            "frequency": round(detected_hz, 2),
            "corrected_fundamental_hz": None,
            "harmonic_ratio_used": None,
            "string": None,
            "note": None,
            "octave": None,
            "full_note": "--",
            "fret": None,
            "confidence": 0,
            "cents_error": None,
            "frequency_note": note_from_frequency(detected_hz),
            "validation_warning": None,
            "alternate_positions": [],
            "ambiguous_match": False,
            "top_matches": [],
            "string_filter": string_filter,
        }

    selected = match["selected"]
    closest = selected["entry"]
    top_matches = [_public_single_match(candidate) for candidate in match["top_matches"]]
    alternate_positions = [_public_alternate_position(candidate) for candidate in match["alternates"]]
    cents_error = float(selected["cents_error"])
    confidence = int(round(float(selected["score"]) * 100))
    note_info = get_note_from_string_and_fret(closest["string"], closest["fret"])
    corrected_hz = float(selected["corrected_fundamental_hz"])
    frequency_note = note_from_frequency(corrected_hz)
    validation = _validate_note_frequency(note_info, frequency_note, corrected_hz)
    if validation["mismatch"]:
        confidence = max(0, confidence - validation["confidence_penalty"])
    status = "ok" if confidence >= 60 else "uncertain"
    message = "Matched calibrated note" if status == "ok" else "Uncertain - play one clean note"
    if string_filter:
        message = f"{message} on {string_filter}"
    if selected["harmonic_ratio_used"] != 1:
        message = (
            f"{message}. Harmonic correction used: {detected_hz:.2f} Hz -> "
            f"{corrected_hz:.2f} Hz"
        )
    if alternate_positions:
        message = (
            f"{message}. Same pitch also matches "
            f"{_format_alternate_positions(alternate_positions)}."
        )
    if selected.get("ambiguous"):
        message = (
            f"{message} Ambiguous match: select a string filter or recalibrate with "
            "spectral fingerprints for best physical-string accuracy."
        )
    if validation["warning"]:
        message = f"{message}. {validation['warning']}"
    logger.info(
        "single_match raw=%.2f corrected=%.2f harmonic=%s top3=%s",
        detected_hz,
        corrected_hz,
        selected["harmonic_ratio_used"],
        [
            {
                "string": item["string"],
                "fret": item["fret"],
                "score": item["score"],
                "cents": item["cents_error"],
            }
            for item in top_matches
        ],
    )

    return {
        "status": status,
        "type": "Single Note",
        "message": message,
        "detected_frequency_hz": round(detected_hz, 2),
        "frequency": round(detected_hz, 2),
        "corrected_fundamental_hz": round(corrected_hz, 2),
        "harmonic_ratio_used": selected["harmonic_ratio_used"],
        "string": closest["string"],
        "note": note_info["note"],
        "octave": note_info["octave"],
        "full_note": note_info["full_note"],
        "fret": closest["fret"],
        "calibrated_frequency_hz": selected["reference_frequency_hz"],
        "calibrated_detected_frequency_hz": closest["frequency_hz"],
        "confidence": confidence,
        "cents_error": round(cents_error, 2) if math.isfinite(cents_error) else None,
        "frequency_note": frequency_note,
        "validation_warning": validation["warning"],
        "alternate_positions": alternate_positions,
        "ambiguous_match": bool(alternate_positions) or bool(selected.get("ambiguous")),
        "top_matches": top_matches,
        "score_components": selected["score_components"],
        "string_filter": string_filter,
    }


def _select_single_note_match(
    detected_hz: float,
    entries: list[dict[str, Any]],
    *,
    detected_fingerprint: dict[str, Any],
) -> dict[str, Any] | None:
    """Choose the best string/fret using pitch correction plus spectral features."""
    candidates = [
        candidate
        for candidate in (
            _build_single_note_candidate(detected_hz, entry, detected_fingerprint)
            for entry in entries
        )
        if candidate is not None
    ]
    if not candidates:
        return None

    candidates.sort(key=lambda candidate: (-candidate["score"], _stable_position_sort_key(candidate)))
    best = candidates[0]
    top_matches = candidates[:3]
    tied_positions = _find_ambiguous_single_matches(best, candidates)

    selected = min(tied_positions, key=_stable_position_sort_key) if tied_positions else best
    selected["ambiguous"] = len(tied_positions) > 1
    alternates = [
        candidate
        for candidate in sorted(tied_positions, key=_stable_position_sort_key)
        if candidate is not selected
    ]

    return {
        "selected": selected,
        "alternates": alternates,
        "top_matches": top_matches,
    }


def _build_single_note_candidate(
    detected_hz: float,
    entry: dict[str, Any],
    detected_fingerprint: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        fret = int(entry["fret"])
    except (KeyError, TypeError, ValueError):
        return None

    reference_hz = _single_note_reference_frequency(entry)
    if reference_hz <= 0 or not math.isfinite(reference_hz):
        return None

    corrected_hz, harmonic_ratio = _best_corrected_frequency(detected_hz, reference_hz)
    cents_error = cents_between(corrected_hz, reference_hz)
    if not math.isfinite(cents_error):
        return None

    note_info = get_note_from_string_and_fret(entry.get("string", ""), fret)
    stored_fingerprint = entry.get("fingerprint", {})
    score_components = _single_note_score_components(
        abs(cents_error),
        detected_fingerprint,
        stored_fingerprint if isinstance(stored_fingerprint, dict) else {},
    )
    score = (
        score_components["frequency"] * 0.45
        + score_components["harmonics"] * 0.20
        + score_components["chroma"] * 0.15
        + score_components["centroid"] * 0.10
        + score_components["dynamics"] * 0.10
    ) * _harmonic_ratio_weight(harmonic_ratio)
    return {
        "entry": entry,
        "note_info": note_info,
        "detected_frequency_hz": round(detected_hz, 2),
        "reference_frequency_hz": round(reference_hz, 2),
        "corrected_fundamental_hz": round(corrected_hz, 2),
        "harmonic_ratio_used": harmonic_ratio,
        "cents_error": cents_error,
        "abs_cents": abs(cents_error),
        "score_components": {key: round(value, 3) for key, value in score_components.items()},
        "score": max(0.0, min(1.0, float(score))),
        "ambiguous": False,
    }


def _stable_position_sort_key(candidate: dict[str, Any]) -> tuple[int, int, float]:
    entry = candidate["entry"]
    try:
        fret = int(entry.get("fret", 99))
    except (TypeError, ValueError):
        fret = 99
    string_rank = STRING_ORDER.get(str(entry.get("string", "")), 99)
    return fret, string_rank, float(candidate.get("abs_cents", 0.0))


def _public_alternate_position(candidate: dict[str, Any]) -> dict[str, Any]:
    entry = candidate["entry"]
    note_info = candidate["note_info"]
    return {
        "string": entry.get("string"),
        "fret": entry.get("fret"),
        "note": note_info.get("note"),
        "octave": note_info.get("octave"),
        "full_note": note_info.get("full_note"),
        "calibrated_frequency_hz": candidate.get("reference_frequency_hz"),
        "corrected_fundamental_hz": candidate.get("corrected_fundamental_hz"),
        "harmonic_ratio_used": candidate.get("harmonic_ratio_used"),
        "score": round(float(candidate.get("score", 0.0)) * 100, 1),
        "cents_error": round(float(candidate["cents_error"]), 2),
    }


def _public_single_match(candidate: dict[str, Any]) -> dict[str, Any]:
    entry = candidate["entry"]
    note_info = candidate["note_info"]
    return {
        "string": entry.get("string"),
        "fret": entry.get("fret"),
        "note": note_info.get("note"),
        "full_note": note_info.get("full_note"),
        "score": round(float(candidate.get("score", 0.0)) * 100, 1),
        "confidence": int(round(float(candidate.get("score", 0.0)) * 100)),
        "detected_frequency_hz": candidate.get("detected_frequency_hz"),
        "corrected_fundamental_hz": candidate.get("corrected_fundamental_hz"),
        "reference_frequency_hz": candidate.get("reference_frequency_hz"),
        "harmonic_ratio_used": candidate.get("harmonic_ratio_used"),
        "cents_error": round(float(candidate["cents_error"]), 2),
        "score_components": candidate.get("score_components", {}),
    }


def _format_alternate_positions(alternate_positions: list[dict[str, Any]]) -> str:
    shown = alternate_positions[:3]
    formatted = [
        f"{position.get('string')} fret {position.get('fret')}"
        for position in shown
        if position.get("string") is not None and position.get("fret") is not None
    ]
    if len(alternate_positions) > len(shown):
        formatted.append(f"{len(alternate_positions) - len(shown)} more")
    return ", ".join(formatted) if formatted else "another calibrated position"


def _find_ambiguous_single_matches(
    best: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    best_score = float(best.get("score", 0.0))
    ambiguous = [
        candidate
        for candidate in candidates
        if best_score - float(candidate.get("score", 0.0)) <= 0.025
    ]
    return ambiguous or [best]


def _single_note_reference_frequency(entry: dict[str, Any]) -> float:
    fingerprint = entry.get("fingerprint", {})
    if isinstance(fingerprint, dict):
        try:
            frequency = float(fingerprint.get("fundamental_frequency_hz", 0.0))
        except (TypeError, ValueError):
            frequency = 0.0
        if frequency > 0 and math.isfinite(frequency):
            return frequency

    note_info = get_note_from_string_and_fret(entry.get("string", ""), entry.get("fret"))
    expected = frequency_from_note_info(note_info)
    if expected:
        return float(expected)

    return float(entry.get("average_frequency_hz", entry.get("frequency_hz", 0.0)) or 0.0)


def _best_corrected_frequency(detected_hz: float, reference_hz: float) -> tuple[float, float]:
    candidates = [
        (detected_hz, 1),
        (detected_hz / 2.0, 2),
        (detected_hz / 3.0, 3),
        (detected_hz / 4.0, 4),
        (detected_hz * 2.0, 0.5),
    ]
    return min(candidates, key=lambda item: abs(cents_between(item[0], reference_hz)))


def _single_note_score_components(
    abs_cents: float,
    detected_fingerprint: dict[str, Any],
    stored_fingerprint: dict[str, Any],
) -> dict[str, float]:
    return {
        "frequency": _frequency_score(abs_cents),
        "harmonics": _harmonic_peak_similarity(detected_fingerprint, stored_fingerprint),
        "chroma": _chroma_similarity_or_neutral(detected_fingerprint, stored_fingerprint),
        "centroid": _centroid_similarity(detected_fingerprint, stored_fingerprint),
        "dynamics": _dynamics_similarity(detected_fingerprint, stored_fingerprint),
    }


def _frequency_score(abs_cents: float) -> float:
    if abs_cents <= 5:
        return 1.0
    if abs_cents <= 10:
        return 1.0 - ((abs_cents - 5) / 5) * 0.1
    if abs_cents <= 25:
        return 0.9 - ((abs_cents - 10) / 15) * 0.3
    if abs_cents <= 50:
        return 0.6 - ((abs_cents - 25) / 25) * 0.4
    return max(0.0, 0.2 - ((abs_cents - 50) / 50) * 0.2)


def _harmonic_ratio_weight(ratio: float) -> float:
    if ratio == 0.5:
        return 0.7
    if ratio in {3, 4}:
        return 0.94
    return 1.0


def _harmonic_peak_similarity(
    detected_fingerprint: dict[str, Any],
    stored_fingerprint: dict[str, Any],
) -> float:
    detected_peaks = _fingerprint_peaks(detected_fingerprint)
    stored_peaks = _fingerprint_peaks(stored_fingerprint)
    if not detected_peaks or not stored_peaks:
        return 0.55

    matches = 0
    for stored_peak in stored_peaks[:8]:
        if any(abs(cents_between(detected_peak, stored_peak)) <= 35 for detected_peak in detected_peaks[:10]):
            matches += 1
    return max(0.0, min(1.0, matches / max(min(len(stored_peaks), 8), 1)))


def _fingerprint_peaks(fingerprint: dict[str, Any]) -> list[float]:
    values = fingerprint.get("harmonic_peaks_hz", fingerprint.get("dominant_frequencies", []))
    if not isinstance(values, list):
        return []

    peaks: list[float] = []
    for value in values:
        try:
            frequency = float(value)
        except (TypeError, ValueError):
            continue
        if frequency > 0 and math.isfinite(frequency):
            peaks.append(frequency)
    return peaks


def _chroma_similarity_or_neutral(
    detected_fingerprint: dict[str, Any],
    stored_fingerprint: dict[str, Any],
) -> float:
    detected = detected_fingerprint.get("chroma_vector", [])
    stored = stored_fingerprint.get("chroma_vector", [])
    if not _has_vector_energy(detected) or not _has_vector_energy(stored):
        return 0.55
    return compare_chroma_vectors(detected, stored)


def _has_vector_energy(values: Any) -> bool:
    if not isinstance(values, list):
        return False
    try:
        return sum(abs(float(value)) for value in values) > 1e-9
    except (TypeError, ValueError):
        return False


def _centroid_similarity(
    detected_fingerprint: dict[str, Any],
    stored_fingerprint: dict[str, Any],
) -> float:
    detected = _fingerprint_float(
        detected_fingerprint,
        "spectral_centroid",
        fallback_key="spectral_centroid_avg",
    )
    stored = _fingerprint_float(
        stored_fingerprint,
        "spectral_centroid",
        fallback_key="spectral_centroid_avg",
    )
    return _ratio_similarity_or_neutral(detected, stored)


def _dynamics_similarity(
    detected_fingerprint: dict[str, Any],
    stored_fingerprint: dict[str, Any],
) -> float:
    rms_score = _ratio_similarity_or_neutral(
        _fingerprint_float(detected_fingerprint, "rms_energy", fallback_key="rms"),
        _fingerprint_float(stored_fingerprint, "rms_energy", fallback_key="rms"),
    )
    attack_score = _ratio_similarity_or_neutral(
        _fingerprint_float(detected_fingerprint, "attack_time_ms"),
        _fingerprint_float(stored_fingerprint, "attack_time_ms"),
    )
    decay_score = _decay_similarity(
        detected_fingerprint.get("decay_profile", []),
        stored_fingerprint.get("decay_profile", []),
    )
    return (rms_score + attack_score + decay_score) / 3.0


def _decay_similarity(detected: Any, stored: Any) -> float:
    if not isinstance(detected, list) or not isinstance(stored, list) or not detected or not stored:
        return 0.55
    length = min(len(detected), len(stored))
    try:
        differences = [
            abs(float(detected[index]) - float(stored[index]))
            for index in range(length)
        ]
    except (TypeError, ValueError):
        return 0.55
    return max(0.0, min(1.0, 1.0 - (sum(differences) / length)))


def _fingerprint_float(
    fingerprint: dict[str, Any],
    key: str,
    *,
    fallback_key: str | None = None,
) -> float:
    value = fingerprint.get(key)
    if value in {None, ""} and fallback_key:
        value = fingerprint.get(fallback_key)
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return numeric if math.isfinite(numeric) else 0.0


def _ratio_similarity_or_neutral(value: float, reference: float) -> float:
    if value <= 0 or reference <= 0:
        return 0.55
    ratio = abs(math.log2(value / reference))
    return max(0.0, min(1.0, 1.0 - ratio))


def _build_chord_result(
    payload: dict[str, Any],
    chords: list[dict[str, Any]],
) -> dict[str, Any]:
    chord_payload = payload.get("chord", {})
    fingerprint = chord_payload.get("fingerprint", {})
    detected_chroma = fingerprint.get("chroma_vector", [0.0] * 12)
    detected_pitch_classes = fingerprint.get("pitch_classes", [])

    if not chords:
        return {
            "status": "no_calibration",
            "message": "No chord calibration saved yet.",
            "chord_name": None,
            "closest_chord_name": None,
            "detected_pitch_classes": detected_pitch_classes,
            "missing_expected_notes": [],
            "extra_detected_notes": [],
            "top_matches": [],
            "confidence": 0,
        }

    if not chord_payload.get("clear"):
        return {
            "status": "no_chord",
            "message": "No clear chord detected",
            "chord_name": None,
            "closest_chord_name": None,
            "detected_pitch_classes": detected_pitch_classes,
            "missing_expected_notes": [],
            "extra_detected_notes": [],
            "top_matches": [],
            "confidence": 0,
        }

    matches = []
    for chord in chords:
        stored_chroma = chord.get("fingerprint", {}).get("chroma_vector", [0.0] * 12)
        similarity = compare_chroma_vectors(detected_chroma, stored_chroma)
        expected = _pitch_class_set(chord.get("expected_notes", []))
        detected = _pitch_class_set(detected_pitch_classes)
        overlap = len(expected & detected) / max(len(expected), 1) if expected else 0.0
        score = (similarity * 0.75) + (overlap * 0.25)
        matches.append(
            {
                "score": score,
                "similarity": similarity,
                "overlap": overlap,
                "chord": chord,
                "missing_expected_notes": sorted(expected - detected),
                "extra_detected_notes": sorted(detected - expected),
            }
        )

    if not matches:
        return {
            "status": "no_calibration",
            "message": "No chord calibration saved yet.",
            "chord_name": None,
            "closest_chord_name": None,
            "detected_pitch_classes": detected_pitch_classes,
            "missing_expected_notes": [],
            "extra_detected_notes": [],
            "top_matches": [],
            "confidence": 0,
        }

    matches.sort(key=lambda item: item["score"], reverse=True)
    best_match = matches[0]
    closest = best_match["chord"]
    confidence = int(round(float(best_match["score"]) * 100))
    if confidence >= 85:
        status = "confident"
        message = "Confident chord match"
    elif confidence >= 70:
        status = "possible"
        message = "Possible chord match"
    else:
        status = "uncertain"
        message = "Uncertain - strum one clear chord"

    return {
        "status": status,
        "type": "Chord",
        "message": message,
        "chord_name": closest["chord_name"] if confidence >= 70 else None,
        "closest_chord_name": closest["chord_name"],
        "expected_notes": closest.get("expected_notes", []),
        "detected_pitch_classes": detected_pitch_classes,
        "missing_expected_notes": best_match["missing_expected_notes"],
        "extra_detected_notes": best_match["extra_detected_notes"],
        "top_matches": [_public_chord_match(match) for match in matches[:3]],
        "confidence": confidence,
        "confidence_baseline": closest.get("fingerprint", {}).get("confidence_baseline", 0),
        "fingerprint": fingerprint,
    }


def _public_chord_match(match: dict[str, Any]) -> dict[str, Any]:
    chord = match["chord"]
    return {
        "chord_name": chord.get("chord_name"),
        "confidence": int(round(float(match.get("score", 0.0)) * 100)),
        "chroma_similarity": round(float(match.get("similarity", 0.0)) * 100, 1),
        "note_overlap": round(float(match.get("overlap", 0.0)) * 100, 1),
        "missing_expected_notes": match.get("missing_expected_notes", []),
        "extra_detected_notes": match.get("extra_detected_notes", []),
    }


def _pitch_class_set(values: Any) -> set[str]:
    if not isinstance(values, list):
        return set()
    return {_normalize_pitch_class(value) for value in values if _normalize_pitch_class(value)}


def _normalize_pitch_class(value: Any) -> str:
    note = str(value or "").strip().upper()
    if not note:
        return ""
    if note.startswith("B#"):
        return "C"
    if note.startswith("E#"):
        return "F"
    if len(note) >= 2 and note[1] == "#":
        return note[:2]
    if len(note) >= 2 and note[1] == "B":
        flats = {
            "DB": "C#",
            "EB": "D#",
            "GB": "F#",
            "AB": "G#",
            "BB": "A#",
        }
        return flats.get(note[:2], note[:1])
    return note[:1]


def _build_combined_detection_result(
    single_result: dict[str, Any],
    chord_result: dict[str, Any],
    *,
    detection_mode: str = "auto",
) -> dict[str, Any]:
    single_confidence = int(single_result.get("confidence") or 0)
    chord_confidence = int(chord_result.get("confidence") or 0)
    chord_status = chord_result.get("status")
    single_status = single_result.get("status")
    chord_pitch_count = len(chord_result.get("detected_pitch_classes") or [])

    choose_chord = (
        chord_status in {"confident", "possible"}
        and chord_confidence >= 70
        and chord_pitch_count >= 2
        and chord_confidence >= max(70, single_confidence - 5)
    )
    choose_single = single_status == "ok" and single_confidence >= 60

    if detection_mode == "single" and choose_single:
        selected_type = "Single Note"
        status = single_status
        message = single_result.get("message", "Single note detected")
        primary = single_result
    elif detection_mode == "single":
        selected_type = "Unknown"
        status = "unknown"
        message = single_result.get("message", "Uncertain - play one clean note")
        primary = {}
    elif detection_mode == "chord" and chord_status in {"confident", "possible", "uncertain"}:
        selected_type = "Chord" if chord_status in {"confident", "possible"} else "Unknown"
        status = chord_status if selected_type == "Chord" else "unknown"
        message = chord_result.get("message", "Chord detection active")
        primary = chord_result if selected_type == "Chord" else {}
    elif detection_mode == "chord":
        selected_type = "Unknown"
        status = "unknown"
        message = chord_result.get("message", "Uncertain - strum one clear chord")
        primary = {}
    elif choose_chord:
        selected_type = "Chord"
        status = chord_status
        message = chord_result.get("message", "Chord detected")
        primary = chord_result
    elif choose_single:
        selected_type = "Single Note"
        status = single_status
        message = single_result.get("message", "Single note detected")
        primary = single_result
    elif chord_status in {"confident", "possible"}:
        selected_type = "Chord"
        status = chord_status
        message = chord_result.get("message", "Chord detected")
        primary = chord_result
    else:
        selected_type = "Unknown"
        status = "unknown"
        message = "Uncertain - play one clean note or strum one clear chord"
        primary = {}

    result = {
        "type": selected_type,
        "status": status,
        "message": message,
        "primary": primary,
        "single_note": single_result,
        "chord": chord_result,
        "detection_mode": detection_mode,
    }

    if selected_type == "Single Note":
        result.update(
            {
                "string": single_result.get("string"),
                "fret": single_result.get("fret"),
                "frequency": single_result.get("frequency"),
                "detected_frequency_hz": single_result.get("detected_frequency_hz"),
                "confidence": single_result.get("confidence"),
                "cents_error": single_result.get("cents_error"),
                "note": single_result.get("note"),
                "octave": single_result.get("octave"),
                "full_note": single_result.get("full_note"),
                "validation_warning": single_result.get("validation_warning"),
                "alternate_positions": single_result.get("alternate_positions", []),
                "ambiguous_match": single_result.get("ambiguous_match", False),
            }
        )
    else:
        result.update(
            {
                "string": None,
                "fret": None,
                "frequency": None,
                "detected_frequency_hz": None,
                "confidence": chord_result.get("confidence", 0) if selected_type == "Chord" else 0,
                "cents_error": None,
                "note": "--",
                "octave": None,
                "full_note": "--",
                "validation_warning": None,
                "alternate_positions": [],
                "ambiguous_match": False,
            }
        )

    return result


def _validate_note_frequency(
    string_fret_note: dict[str, Any],
    frequency_note: dict[str, Any],
    detected_hz: float,
) -> dict[str, Any]:
    """Compare standard-tuning note expectation with raw frequency note.

    We primarily compare pitch class rather than octave because guitar signals
    can contain strong harmonics. A pitch-class mismatch is a stronger sign that
    the closest calibrated string/fret may be wrong.
    """
    expected_note = string_fret_note.get("note")
    measured_note = frequency_note.get("note")
    if expected_note in {None, "--"} or measured_note in {None, "--"}:
        return {"mismatch": False, "warning": None, "confidence_penalty": 0}

    expected_frequency = frequency_from_note_info(string_fret_note)
    expected_cents = (
        abs(cents_between(detected_hz, expected_frequency))
        if expected_frequency
        else 0.0
    )

    if expected_note != measured_note:
        return {
            "mismatch": True,
            "warning": "Possible mismatch detected",
            "confidence_penalty": 30 if expected_cents > 65 else 18,
        }

    if expected_cents > 90:
        return {
            "mismatch": False,
            "warning": (
                "This calibrated row is far from the standard fret pitch; "
                "recalibrate it if the detector grabbed a harmonic."
            ),
            "confidence_penalty": 0,
        }

    return {"mismatch": False, "warning": None, "confidence_penalty": 0}


def _confidence_from_cents(abs_cents: float) -> int:
    """Map cents error to a practical confidence percentage."""
    if abs_cents <= 5:
        return 100
    if abs_cents <= 10:
        return round(100 - ((abs_cents - 5) / 5) * 10)
    if abs_cents <= 25:
        return round(90 - ((abs_cents - 10) / 15) * 30)
    if abs_cents <= 50:
        return round(60 - ((abs_cents - 25) / 25) * 40)
    return max(0, round(20 - ((abs_cents - 50) / 50) * 20))


def _infer_root(chord_name: str) -> str:
    name = chord_name.strip()
    if len(name) >= 2 and name[1] == "#":
        return name[:2]
    return name[:1]


def _emit_pitch_update(payload: dict[str, Any]) -> None:
    socketio.emit("pitch_update", payload)


def _emit_status(payload: dict[str, Any]) -> None:
    enriched = dict(payload)
    enriched.setdefault("level", "info")
    enriched.setdefault("mode", "system")
    enriched.setdefault("timestamp", _now_iso())
    socketio.emit("status", enriched)
    logger.info("%s: %s", enriched.get("mode"), enriched.get("message"))


def _emit_error(message: str, *, mode: str = "system") -> None:
    socketio.emit("error", {"message": message, "mode": mode, "timestamp": _now_iso()})
    logger.warning("%s error: %s", mode, message)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    ensure_store()
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    logger.info("Starting Guitar Fret Trainer at http://127.0.0.1:5000")
    socketio.run(
        app,
        host="127.0.0.1",
        port=5000,
        debug=debug,
        allow_unsafe_werkzeug=True,
    )
