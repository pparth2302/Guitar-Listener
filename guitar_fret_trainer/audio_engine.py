"""Realtime microphone pitch capture for Guitar Fret Trainer.

The audio callback does only the minimum work needed to estimate pitch and
store the latest reading. Worker loops outside the callback emit updates to the
web app so UI/network work does not block the microphone stream.
"""

from __future__ import annotations

import math
import statistics
import threading
import time
from collections import deque
from typing import Any, Callable

try:
    import aubio

    AUBIO_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - exercised on machines without audio deps
    aubio = None
    AUBIO_IMPORT_ERROR = exc

try:
    import numpy as np

    NUMPY_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover
    np = None
    NUMPY_IMPORT_ERROR = exc

try:
    import sounddevice as sd

    SOUNDDEVICE_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover
    sd = None
    SOUNDDEVICE_IMPORT_ERROR = exc


PitchCallback = Callable[[dict[str, Any]], None]
StatusCallback = Callable[[dict[str, Any]], None]
CalibrationCallback = Callable[[float | None, dict[str, Any]], None]


def cents_between(frequency_hz: float, reference_hz: float) -> float:
    """Return signed cents difference from reference_hz to frequency_hz."""
    if frequency_hz <= 0 or reference_hz <= 0:
        return float("inf")
    return 1200.0 * math.log2(frequency_hz / reference_hz)


class PitchProcessor:
    """Pitch detector with RMS gating and rolling-median smoothing.

    aubio/yinfft is preferred. On Windows systems without a compiled aubio
    wheel, a NumPy autocorrelation fallback keeps the local app usable.
    """

    def __init__(
        self,
        sample_rate: int,
        buffer_size: int,
        hop_size: int,
        rms_threshold: float,
        method: str,
        median_window: int,
        stability_cents: float,
        min_frequency_hz: float,
        max_frequency_hz: float,
        min_aubio_confidence: float,
    ) -> None:
        if np is None:
            raise RuntimeError(f"NumPy is unavailable: {NUMPY_IMPORT_ERROR}")

        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.hop_size = hop_size
        self.rms_threshold = rms_threshold
        self.stability_cents = stability_cents
        self.min_frequency_hz = min_frequency_hz
        self.max_frequency_hz = max_frequency_hz
        self.min_aubio_confidence = min_aubio_confidence
        self._frequencies: deque[float] = deque(maxlen=median_window)
        self._minimum_window_for_stability = max(3, min(5, median_window))
        self._audio_window = np.zeros(buffer_size, dtype=np.float32)
        self.detector_name = "numpy-autocorrelation"

        self._pitch = None
        if aubio is not None:
            self.detector_name = f"aubio-{method}"
            self._pitch = aubio.pitch(method, buffer_size, hop_size, sample_rate)
            self._pitch.set_unit("Hz")
            self._pitch.set_silence(-40)
            try:
                self._pitch.set_tolerance(0.8)
            except Exception:
                # Not all aubio pitch methods expose tolerance; yin/yinfft do.
                pass

    def process(self, samples: Any) -> dict[str, Any]:
        """Process one audio block and return a UI-friendly pitch payload."""
        block = np.asarray(samples, dtype=np.float32).reshape(-1)

        if len(block) < self.hop_size:
            block = np.pad(block, (0, self.hop_size - len(block)))
        elif len(block) > self.hop_size:
            block = block[: self.hop_size]

        rms = float(np.sqrt(np.mean(np.square(block)))) if len(block) else 0.0
        if rms < self.rms_threshold:
            self._frequencies.clear()
            return self._no_pitch(rms, "Signal below noise threshold")

        if self._pitch is not None:
            try:
                frequency_hz = float(self._pitch(block)[0])
                detector_confidence = float(self._pitch.get_confidence())
            except Exception as exc:
                self._frequencies.clear()
                return self._no_pitch(
                    rms,
                    "Pitch detection failed",
                    detector=self.detector_name,
                    error=str(exc),
                )
        else:
            frequency_hz, detector_confidence = self._estimate_pitch_autocorrelation(block)

        if not math.isfinite(frequency_hz) or frequency_hz <= 0:
            self._frequencies.clear()
            return self._no_pitch(rms, "No clear note detected", detector=self.detector_name)

        if frequency_hz < self.min_frequency_hz or frequency_hz > self.max_frequency_hz:
            self._frequencies.clear()
            return self._no_pitch(
                rms,
                "Detected pitch is outside guitar range",
                detector=self.detector_name,
                raw_frequency_hz=round(frequency_hz, 2),
                detector_confidence=round(detector_confidence, 3),
            )

        if detector_confidence < self.min_aubio_confidence:
            self._frequencies.clear()
            return self._no_pitch(
                rms,
                "No clear note detected",
                detector=self.detector_name,
                raw_frequency_hz=round(frequency_hz, 2),
                detector_confidence=round(detector_confidence, 3),
            )

        self._frequencies.append(frequency_hz)
        smoothed_hz = float(statistics.median(self._frequencies))
        spread_cents = self._window_spread_cents(smoothed_hz)
        stable = (
            len(self._frequencies) >= self._minimum_window_for_stability
            and spread_cents <= self.stability_cents
        )

        return {
            "clear": stable,
            "stable": stable,
            "frequency_hz": round(frequency_hz, 2),
            "smoothed_frequency_hz": round(smoothed_hz, 2),
            "rms": round(rms, 5),
            "aubio_confidence": round(detector_confidence, 3),
            "detector_confidence": round(detector_confidence, 3),
            "detector": self.detector_name,
            "spread_cents": round(spread_cents, 2) if math.isfinite(spread_cents) else None,
            "message": "Stable pitch detected" if stable else "Stabilizing pitch...",
            "timestamp": time.time(),
        }

    def _estimate_pitch_autocorrelation(self, block: Any) -> tuple[float, float]:
        """Estimate pitch from a rolling audio window using autocorrelation."""
        self._audio_window = np.roll(self._audio_window, -len(block))
        self._audio_window[-len(block) :] = block

        windowed = self._audio_window.astype(np.float32, copy=True)
        windowed -= float(np.mean(windowed))
        energy = float(np.dot(windowed, windowed))
        if energy <= 1e-9:
            return 0.0, 0.0

        windowed *= np.hanning(len(windowed))
        correlation = np.correlate(windowed, windowed, mode="full")[len(windowed) - 1 :]
        if len(correlation) < 3 or correlation[0] <= 1e-9:
            return 0.0, 0.0

        min_lag = max(1, int(self.sample_rate / self.max_frequency_hz))
        max_lag = min(len(correlation) - 2, int(self.sample_rate / self.min_frequency_hz))
        if max_lag <= min_lag:
            return 0.0, 0.0

        search = correlation[min_lag : max_lag + 1]
        peak_index = int(np.argmax(search)) + min_lag
        peak_strength = float(correlation[peak_index] / correlation[0])
        if peak_strength < 0.18:
            return 0.0, max(0.0, peak_strength)

        lag = self._parabolic_peak_lag(correlation, peak_index)
        if lag <= 0:
            return 0.0, 0.0

        frequency_hz = self.sample_rate / lag
        confidence = max(0.0, min(1.0, peak_strength))
        return float(frequency_hz), confidence

    @staticmethod
    def _parabolic_peak_lag(correlation: Any, peak_index: int) -> float:
        if peak_index <= 0 or peak_index >= len(correlation) - 1:
            return float(peak_index)

        y0 = float(correlation[peak_index - 1])
        y1 = float(correlation[peak_index])
        y2 = float(correlation[peak_index + 1])
        denominator = y0 - 2.0 * y1 + y2
        if abs(denominator) < 1e-12:
            return float(peak_index)

        shift = 0.5 * (y0 - y2) / denominator
        return float(peak_index) + max(-0.5, min(0.5, shift))

    def _window_spread_cents(self, reference_hz: float) -> float:
        if reference_hz <= 0 or not self._frequencies:
            return float("inf")
        return max(abs(cents_between(value, reference_hz)) for value in self._frequencies)

    @staticmethod
    def _no_pitch(
        rms: float,
        message: str,
        *,
        detector: str = "unknown",
        raw_frequency_hz: float | None = None,
        detector_confidence: float = 0.0,
        error: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "clear": False,
            "stable": False,
            "frequency_hz": None,
            "smoothed_frequency_hz": None,
            "raw_frequency_hz": raw_frequency_hz,
            "rms": round(rms, 5),
            "aubio_confidence": detector_confidence,
            "detector_confidence": detector_confidence,
            "detector": detector,
            "spread_cents": None,
            "message": message,
            "timestamp": time.time(),
        }
        if error:
            payload["error"] = error
        return payload


class AudioEngine:
    """Owns microphone streams for calibration and detection."""

    def __init__(
        self,
        *,
        sample_rate: int = 44100,
        buffer_size: int = 2048,
        hop_size: int = 512,
        rms_threshold: float = 0.008,
        pitch_method: str = "yinfft",
        median_window: int = 7,
        stability_cents: float = 20.0,
        min_frequency_hz: float = 50.0,
        max_frequency_hz: float = 1200.0,
        min_aubio_confidence: float = 0.2,
        calibration_seconds: float = 3.0,
        emit_interval_seconds: float = 0.12,
        logger: Any | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.hop_size = hop_size
        self.rms_threshold = rms_threshold
        self.pitch_method = pitch_method
        self.median_window = median_window
        self.stability_cents = stability_cents
        self.min_frequency_hz = min_frequency_hz
        self.max_frequency_hz = max_frequency_hz
        self.min_aubio_confidence = min_aubio_confidence
        self.calibration_seconds = calibration_seconds
        self.emit_interval_seconds = emit_interval_seconds
        self.logger = logger

        self._lock = threading.RLock()
        self._mode = "idle"
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def audio_available(self) -> tuple[bool, str]:
        """Check dependencies and the presence of at least one input device."""
        if NUMPY_IMPORT_ERROR is not None or np is None:
            return (
                False,
                "Audio dependencies are unavailable. Run pip install -r requirements.txt. "
                f"Details: {NUMPY_IMPORT_ERROR}",
            )

        if SOUNDDEVICE_IMPORT_ERROR is not None or sd is None:
            return (
                False,
                "Audio dependencies are unavailable. Run pip install -r requirements.txt. "
                f"Details: {SOUNDDEVICE_IMPORT_ERROR}",
            )

        try:
            devices = sd.query_devices()
        except Exception as exc:
            return False, f"Unable to query audio devices: {exc}"

        has_input = any(int(device.get("max_input_channels", 0)) > 0 for device in devices)
        if not has_input:
            return False, "No audio input device found. Connect or enable a microphone and restart."
        if aubio is None:
            return (
                True,
                "Audio input is available. aubio is not installed, so the NumPy fallback detector will be used.",
            )
        return True, "Audio input is available."

    def start_calibration(
        self,
        string_label: str,
        note: str,
        fret: int,
        pitch_callback: PitchCallback,
        completion_callback: CalibrationCallback,
        status_callback: StatusCallback,
    ) -> tuple[bool, str]:
        available, message = self.audio_available()
        if not available:
            return False, message

        with self._lock:
            if self._mode != "idle":
                return False, f"Audio engine is busy in {self._mode} mode."

            self._mode = "calibration"
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_calibration,
                args=(string_label, note, fret, pitch_callback, completion_callback, status_callback),
                daemon=True,
            )
            self._thread.start()

        return True, "Calibration started."

    def start_detection(
        self,
        pitch_callback: PitchCallback,
        status_callback: StatusCallback,
    ) -> tuple[bool, str]:
        available, message = self.audio_available()
        if not available:
            return False, message

        with self._lock:
            if self._mode != "idle":
                return False, f"Audio engine is busy in {self._mode} mode."

            self._mode = "detection"
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_detection,
                args=(pitch_callback, status_callback),
                daemon=True,
            )
            self._thread.start()

        return True, "Detection started."

    def stop_detection(self) -> tuple[bool, str]:
        return self.stop_active_audio(expected_mode="detection")

    def stop_active_audio(self, expected_mode: str | None = None) -> tuple[bool, str]:
        """Stop whichever audio mode currently owns the microphone."""
        with self._lock:
            if self._mode == "idle":
                return False, "Audio is already stopped."
            if expected_mode is not None and self._mode != expected_mode:
                return False, f"{expected_mode.capitalize()} is not running."

            mode = self._mode
            thread = self._thread
            self._stop_event.set()

        if thread and thread.is_alive():
            thread.join(timeout=3.0)

        if thread and thread.is_alive():
            return True, f"Stop requested for {mode}; waiting for the audio stream to close."
        return True, f"{mode.capitalize()} stopped."

    def _run_calibration(
        self,
        string_label: str,
        note: str,
        fret: int,
        pitch_callback: PitchCallback,
        completion_callback: CalibrationCallback,
        status_callback: StatusCallback,
    ) -> None:
        readings: list[float] = []
        latest_payload: dict[str, Any] | None = None
        data_lock = threading.Lock()

        try:
            processor = self._make_processor()
        except Exception as exc:
            if self.logger:
                self.logger.exception("Unable to initialize pitch processor for calibration")
            try:
                completion_callback(
                    None,
                    {
                        "samples": 0,
                        "message": f"Unable to initialize pitch detection: {exc}",
                        "error": str(exc),
                    },
                )
            finally:
                self._mark_idle()
            return

        def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            nonlocal latest_payload
            if status and self.logger:
                self.logger.warning("Audio callback status during calibration: %s", status)
            try:
                payload = processor.process(indata[:, 0])
            except Exception as exc:
                if self.logger:
                    self.logger.exception("Pitch processing failed during calibration")
                payload = self._error_pitch_payload("Pitch detection failed", exc)

            if payload.get("stable") and payload.get("smoothed_frequency_hz"):
                with data_lock:
                    readings.append(float(payload["smoothed_frequency_hz"]))

            with data_lock:
                latest_payload = payload

        self._safe_status(
            status_callback,
            {
                "level": "info",
                "mode": "calibration",
                "message": (
                    f"Listening for {string_label} {note} fret {fret} for 3 seconds "
                    f"using {processor.detector_name}..."
                ),
            },
        )

        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                blocksize=self.hop_size,
                dtype="float32",
                callback=callback,
            ):
                deadline = time.monotonic() + self.calibration_seconds
                while time.monotonic() < deadline and not self._stop_event.is_set():
                    time.sleep(self.emit_interval_seconds)
                    seconds_remaining = max(0.0, deadline - time.monotonic())
                    with data_lock:
                        payload = dict(latest_payload) if latest_payload else None
                        sample_count = len(readings)

                    if payload:
                        payload.update(
                            {
                                "mode": "calibration",
                                "seconds_remaining": round(seconds_remaining, 1),
                                "stable_samples": sample_count,
                            }
                        )
                        self._safe_pitch(pitch_callback, payload)

            with data_lock:
                stable_values = list(readings)
            average_hz = self._average_best_stable_values(stable_values)

            if average_hz is None:
                completion_callback(
                    None,
                    {
                        "samples": len(stable_values),
                        "message": "Calibration did not capture a stable pitch. Try a clean single note.",
                    },
                )
            else:
                completion_callback(
                    round(average_hz, 2),
                    {
                        "samples": len(stable_values),
                        "message": "Calibration captured successfully.",
                    },
                )
        except Exception as exc:
            if self.logger:
                self.logger.exception("Calibration stream failed")
            completion_callback(
                None,
                {
                    "samples": 0,
                    "message": f"Unable to open microphone for calibration: {exc}",
                    "error": str(exc),
                },
            )
        finally:
            self._mark_idle()

    def _run_detection(
        self,
        pitch_callback: PitchCallback,
        status_callback: StatusCallback,
    ) -> None:
        latest_payload: dict[str, Any] | None = None
        data_lock = threading.Lock()

        try:
            processor = self._make_processor()
        except Exception as exc:
            if self.logger:
                self.logger.exception("Unable to initialize pitch processor for detection")
            self._safe_status(
                status_callback,
                {
                    "level": "error",
                    "mode": "detection",
                    "message": f"Unable to initialize pitch detection: {exc}",
                },
            )
            self._mark_idle()
            return

        def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            nonlocal latest_payload
            if status and self.logger:
                self.logger.warning("Audio callback status during detection: %s", status)
            try:
                payload = processor.process(indata[:, 0])
            except Exception as exc:
                if self.logger:
                    self.logger.exception("Pitch processing failed during detection")
                payload = self._error_pitch_payload("Pitch detection failed", exc)

            with data_lock:
                latest_payload = payload

        self._safe_status(
            status_callback,
            {
                "level": "success",
                "mode": "detection",
                "message": (
                    f"Detection started using {processor.detector_name}. "
                    "Play one clean note at a time."
                ),
            },
        )

        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                blocksize=self.hop_size,
                dtype="float32",
                callback=callback,
            ):
                while not self._stop_event.is_set():
                    time.sleep(self.emit_interval_seconds)
                    with data_lock:
                        payload = dict(latest_payload) if latest_payload else None
                    if payload:
                        payload["mode"] = "detection"
                        self._safe_pitch(pitch_callback, payload)
        except Exception as exc:
            if self.logger:
                self.logger.exception("Detection stream failed")
            self._safe_status(
                status_callback,
                {
                    "level": "error",
                    "mode": "detection",
                    "message": f"Unable to open microphone for detection: {exc}",
                },
            )
        finally:
            was_stopped = self._stop_event.is_set()
            self._mark_idle()
            if was_stopped:
                self._safe_status(
                    status_callback,
                    {
                        "level": "info",
                        "mode": "detection",
                        "message": "Detection stopped.",
                    },
                )

    def _make_processor(self) -> PitchProcessor:
        return PitchProcessor(
            sample_rate=self.sample_rate,
            buffer_size=self.buffer_size,
            hop_size=self.hop_size,
            rms_threshold=self.rms_threshold,
            method=self.pitch_method,
            median_window=self.median_window,
            stability_cents=self.stability_cents,
            min_frequency_hz=self.min_frequency_hz,
            max_frequency_hz=self.max_frequency_hz,
            min_aubio_confidence=self.min_aubio_confidence,
        )

    def _mark_idle(self) -> None:
        with self._lock:
            self._mode = "idle"
            self._thread = None
            self._stop_event.clear()

    def _safe_pitch(self, callback: PitchCallback, payload: dict[str, Any]) -> None:
        try:
            callback(payload)
        except Exception:
            if self.logger:
                self.logger.exception("Pitch callback failed")

    def _safe_status(self, callback: StatusCallback, payload: dict[str, Any]) -> None:
        try:
            callback(payload)
        except Exception:
            if self.logger:
                self.logger.exception("Status callback failed")

    @staticmethod
    def _error_pitch_payload(message: str, exc: Exception) -> dict[str, Any]:
        return {
            "clear": False,
            "stable": False,
            "frequency_hz": None,
            "smoothed_frequency_hz": None,
            "rms": 0.0,
            "aubio_confidence": 0.0,
            "spread_cents": None,
            "message": message,
            "error": str(exc),
            "timestamp": time.time(),
        }

    @staticmethod
    def _average_best_stable_values(values: list[float]) -> float | None:
        """Average stable values after trimming outliers around the median."""
        if len(values) < 5:
            return None

        median_hz = float(statistics.median(values))
        best_values = [
            value for value in values if abs(cents_between(value, median_hz)) <= 8.0
        ]
        if len(best_values) < 5:
            best_values = [
                value for value in values if abs(cents_between(value, median_hz)) <= 15.0
            ]
        if len(best_values) < 5:
            return None
        return float(statistics.fmean(best_values))
