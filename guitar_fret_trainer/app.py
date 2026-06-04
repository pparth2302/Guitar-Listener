"""Flask + Socket.IO entry point for Guitar Fret Trainer."""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from typing import Any

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit

from audio_engine import AudioEngine, cents_between
from calibration_store import ensure_store, load_calibration, reset_calibration, save_calibration_entry

GUITAR_STRINGS = [
    {"label": "Low E", "note": "E2"},
    {"label": "A", "note": "A2"},
    {"label": "D", "note": "D3"},
    {"label": "G", "note": "G3"},
    {"label": "B", "note": "B3"},
    {"label": "High E", "note": "E4"},
]

FRETS = list(range(13))

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
    return jsonify(
        {
            "ok": True,
            "entries": load_calibration(),
            "strings": GUITAR_STRINGS,
            "frets": FRETS,
        }
    )


@app.post("/api/calibration/start")
def api_start_calibration() -> Any:
    data = request.get_json(silent=True) or {}
    try:
        string_label, note, fret = _validate_calibration_request(data)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400

    def on_complete(average_hz: float | None, stats: dict[str, Any]) -> None:
        if average_hz is None:
            _emit_status(
                {
                    "level": "warning",
                    "mode": "calibration",
                    "message": stats.get(
                        "message",
                        "Calibration did not capture a stable pitch. Try again.",
                    ),
                }
            )
            return

        entry = {
            "string": string_label,
            "note": note,
            "fret": fret,
            "frequency_hz": round(float(average_hz), 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        entries = save_calibration_entry(entry)
        socketio.emit("calibration_saved", {"entry": entry, "entries": entries})
        _emit_status(
            {
                "level": "success",
                "mode": "calibration",
                "message": (
                    f"Saved {string_label} {note} fret {fret} "
                    f"at {entry['frequency_hz']:.2f} Hz."
                ),
                "samples": stats.get("samples", 0),
            }
        )
        logger.info("Saved calibration entry: %s", entry)

    ok, message = audio_engine.start_calibration(
        string_label=string_label,
        note=note,
        fret=fret,
        pitch_callback=_emit_pitch_update,
        completion_callback=on_complete,
        status_callback=_emit_status,
    )
    if not ok:
        _emit_status({"level": "error", "mode": "calibration", "message": message})
        status = 409 if "busy" in message.lower() else 503
        return jsonify({"ok": False, "message": message}), status

    return jsonify({"ok": True, "message": message})


@app.post("/api/detection/start")
def api_start_detection() -> Any:
    if not load_calibration():
        message = "Add at least one calibration entry before starting detection."
        _emit_status({"level": "warning", "mode": "detection", "message": message})
        return jsonify({"ok": False, "message": message}), 400

    def on_pitch(payload: dict[str, Any]) -> None:
        _emit_pitch_update(payload)
        socketio.emit("detection_result", _build_detection_result(payload))

    ok, message = audio_engine.start_detection(
        pitch_callback=on_pitch,
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


@app.post("/api/calibration/reset")
def api_reset_calibration() -> Any:
    entries = reset_calibration()
    socketio.emit("calibration_saved", {"entry": None, "entries": entries})
    _emit_status(
        {
            "level": "success",
            "mode": "calibration",
            "message": "Calibration data reset.",
        }
    )
    logger.info("Calibration data reset")
    return jsonify({"ok": True, "entries": entries})


@socketio.on("connect")
def on_connect() -> None:
    emit(
        "status",
        {
            "level": "success",
            "mode": "system",
            "message": "Browser connected to Guitar Fret Trainer.",
            "timestamp": _now_iso(),
        },
    )
    emit("calibration_saved", {"entry": None, "entries": load_calibration()})


def _validate_calibration_request(data: dict[str, Any]) -> tuple[str, str, int]:
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


def _build_detection_result(payload: dict[str, Any]) -> dict[str, Any]:
    frequency_hz = payload.get("smoothed_frequency_hz") or payload.get("frequency_hz")
    if not payload.get("stable") or not frequency_hz:
        return {
            "status": "no_pitch",
            "message": "No clear note detected",
            "detected_frequency_hz": None,
            "string": None,
            "note": None,
            "fret": None,
            "confidence": 0,
            "cents_error": None,
        }

    entries = load_calibration()
    if not entries:
        return {
            "status": "no_calibration",
            "message": "No calibration data available",
            "detected_frequency_hz": round(float(frequency_hz), 2),
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
        "message": "Matched calibrated note" if status == "ok" else "Uncertain — play one clean note",
        "detected_frequency_hz": round(detected_hz, 2),
        "string": closest["string"],
        "note": closest["note"],
        "fret": closest["fret"],
        "calibrated_frequency_hz": closest["frequency_hz"],
        "confidence": confidence,
        "cents_error": round(cents_error, 2) if math.isfinite(cents_error) else None,
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


def _emit_pitch_update(payload: dict[str, Any]) -> None:
    socketio.emit("pitch_update", payload)


def _emit_status(payload: dict[str, Any]) -> None:
    enriched = dict(payload)
    enriched.setdefault("level", "info")
    enriched.setdefault("mode", "system")
    enriched.setdefault("timestamp", _now_iso())
    socketio.emit("status", enriched)
    logger.info("%s: %s", enriched.get("mode"), enriched.get("message"))


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
