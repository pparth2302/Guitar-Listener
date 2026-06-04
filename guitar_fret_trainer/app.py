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
    ensure_store,
    load_calibration,
    reset_all,
    reset_chords,
    reset_single_notes,
    save_chord_entry,
    save_single_note_entry,
)

GUITAR_STRINGS = [
    {"label": "Low E", "note": "E2"},
    {"label": "A", "note": "A2"},
    {"label": "D", "note": "D3"},
    {"label": "G", "note": "G3"},
    {"label": "B", "note": "B3"},
    {"label": "High E", "note": "E4"},
]

FRETS = list(range(13))

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
            "std_dev": stats.get("std_dev"),
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
    calibration = load_calibration()
    if not calibration["single_notes"] and not calibration["chords"]:
        message = "Add at least one single-note or chord calibration before starting detection."
        _emit_status({"level": "warning", "mode": "detection", "message": message})
        return jsonify({"ok": False, "message": message}), 400

    def on_audio(payload: dict[str, Any]) -> None:
        calibration_snapshot = load_calibration()
        single_result = _build_single_note_result(payload, calibration_snapshot["single_notes"])
        chord_result = _build_chord_result(payload, calibration_snapshot["chords"])
        combined_result = _build_combined_detection_result(single_result, chord_result)

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


def _parse_expected_notes(value: str) -> list[str]:
    return [
        item.strip().upper().replace("B#", "C").replace("E#", "F")
        for item in value.split(",")
        if item.strip()
    ]


def _build_single_note_result(
    payload: dict[str, Any],
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    pitch_payload = payload.get("single_note", payload)
    frequency_hz = pitch_payload.get("smoothed_frequency_hz") or pitch_payload.get("frequency_hz")

    if not entries:
        return {
            "status": "no_calibration",
            "message": "No single-note calibration saved yet.",
            "detected_frequency_hz": round(float(frequency_hz), 2) if frequency_hz else None,
            "string": None,
            "note": None,
            "fret": None,
            "confidence": 0,
            "cents_error": None,
        }

    if not pitch_payload.get("stable") or not frequency_hz:
        return {
            "status": "no_pitch",
            "message": "No clear single note detected",
            "detected_frequency_hz": None,
            "string": None,
            "note": None,
            "fret": None,
            "confidence": 0,
            "cents_error": None,
        }

    detected_hz = float(frequency_hz)
    closest = min(
        entries,
        key=lambda entry: abs(cents_between(detected_hz, float(entry["frequency_hz"]))),
    )
    cents_error = cents_between(detected_hz, float(closest["frequency_hz"]))
    confidence = _confidence_from_cents(abs(cents_error))
    status = "ok" if confidence >= 60 else "uncertain"

    return {
        "status": status,
        "type": "Single Note",
        "message": "Matched calibrated note" if status == "ok" else "Uncertain - play one clean note",
        "detected_frequency_hz": round(detected_hz, 2),
        "string": closest["string"],
        "note": closest["note"],
        "fret": closest["fret"],
        "calibrated_frequency_hz": closest["frequency_hz"],
        "confidence": confidence,
        "cents_error": round(cents_error, 2) if math.isfinite(cents_error) else None,
    }


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
            "confidence": 0,
        }

    if not chord_payload.get("clear"):
        return {
            "status": "no_chord",
            "message": "No clear chord detected",
            "chord_name": None,
            "closest_chord_name": None,
            "detected_pitch_classes": detected_pitch_classes,
            "confidence": 0,
        }

    matches = []
    for chord in chords:
        stored_chroma = chord.get("fingerprint", {}).get("chroma_vector", [0.0] * 12)
        similarity = compare_chroma_vectors(detected_chroma, stored_chroma)
        matches.append((similarity, chord))

    if not matches:
        return {
            "status": "no_calibration",
            "message": "No chord calibration saved yet.",
            "chord_name": None,
            "closest_chord_name": None,
            "detected_pitch_classes": detected_pitch_classes,
            "confidence": 0,
        }

    similarity, closest = max(matches, key=lambda item: item[0])
    confidence = int(round(similarity * 100))
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
        "confidence": confidence,
        "confidence_baseline": closest.get("fingerprint", {}).get("confidence_baseline", 0),
        "fingerprint": fingerprint,
    }


def _build_combined_detection_result(
    single_result: dict[str, Any],
    chord_result: dict[str, Any],
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

    if choose_chord:
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

    return {
        "type": selected_type,
        "status": status,
        "message": message,
        "primary": primary,
        "single_note": single_result,
        "chord": chord_result,
    }


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
