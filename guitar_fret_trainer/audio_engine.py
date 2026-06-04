"""Realtime microphone pitch and chord analysis for Guitar Fret Trainer.

Single-note work uses aubio/yinfft when available and a NumPy autocorrelation
fallback on Windows. Chord work is intentionally separate: it uses FFT peaks to
build a 12-bin chroma fingerprint so chord calibration never mixes with
single string/fret calibration.
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
ChordCalibrationCallback = Callable[[dict[str, Any] | None, dict[str, Any]], None]

PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def cents_between(frequency_hz: float, reference_hz: float) -> float:
    """Return signed cents difference from reference_hz to frequency_hz."""
    if frequency_hz <= 0 or reference_hz <= 0:
        return float("inf")
    return 1200.0 * math.log2(frequency_hz / reference_hz)


def frequency_to_midi(frequency_hz: float) -> float:
    if frequency_hz <= 0:
        return float("nan")
    return 69.0 + 12.0 * math.log2(frequency_hz / 440.0)


def midi_to_pitch_class(midi_note: float) -> str:
    if not math.isfinite(midi_note):
        return ""
    return PITCH_CLASSES[int(round(midi_note)) % 12]


def frequency_to_note_name(frequency_hz: float) -> str:
    midi_note = frequency_to_midi(frequency_hz)
    if not math.isfinite(midi_note):
        return ""
    rounded = int(round(midi_note))
    octave = (rounded // 12) - 1
    return f"{PITCH_CLASSES[rounded % 12]}{octave}"


def compare_chroma_vectors(v1: list[float], v2: list[float]) -> float:
    """Cosine similarity for normalized or raw 12-bin chroma vectors."""
    if np is None:
        return 0.0
    a = np.asarray(v1, dtype=np.float64)
    b = np.asarray(v2, dtype=np.float64)
    if a.shape[0] != 12 or b.shape[0] != 12:
        return 0.0
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denominator <= 1e-12:
        return 0.0
    return max(0.0, min(1.0, float(np.dot(a, b) / denominator)))


def extract_chroma_vector(audio_buffer: Any, sample_rate: int) -> list[float]:
    return extract_chord_fingerprint(audio_buffer, sample_rate)["chroma_vector"]


def extract_chord_fingerprint(
    audio_buffer: Any,
    sample_rate: int,
    *,
    rms_threshold: float = 0.008,
    min_frequency_hz: float = 60.0,
    max_frequency_hz: float = 1600.0,
) -> dict[str, Any]:
    """Build a chord fingerprint from FFT peaks.

    The analyzer splits the audio into overlapping frames, finds spectral peaks
    in guitar-friendly frequency ranges, maps those peaks to pitch classes, and
    folds the energy into a normalized 12-bin chroma vector.
    """
    if np is None:
        raise RuntimeError(f"NumPy is unavailable: {NUMPY_IMPORT_ERROR}")

    audio = np.asarray(audio_buffer, dtype=np.float32).reshape(-1)
    if len(audio) == 0:
        return _empty_fingerprint("No audio samples captured")

    audio = audio - float(np.mean(audio))
    total_rms = _rms(audio)
    if total_rms < rms_threshold:
        return _empty_fingerprint("Signal below noise threshold", rms=total_rms)

    frame_size = min(8192, max(2048, _previous_power_of_two(len(audio))))
    hop_size = max(512, frame_size // 2)
    chroma = np.zeros(12, dtype=np.float64)
    peak_strengths: dict[float, float] = {}
    centroid_values: list[float] = []
    active_frames = 0

    if len(audio) < frame_size:
        frames = [np.pad(audio, (0, frame_size - len(audio)))]
    else:
        frames = [
            audio[start : start + frame_size]
            for start in range(0, len(audio) - frame_size + 1, hop_size)
        ]

    for frame in frames:
        frame_rms = _rms(frame)
        if frame_rms < rms_threshold:
            continue

        active_frames += 1
        frame_chroma, peaks, centroid = _analyze_fft_frame(
            frame,
            sample_rate,
            min_frequency_hz=min_frequency_hz,
            max_frequency_hz=max_frequency_hz,
        )
        chroma += frame_chroma
        if centroid > 0:
            centroid_values.append(centroid)

        for frequency_hz, strength in peaks:
            bucket = round(float(frequency_hz), 1)
            peak_strengths[bucket] = peak_strengths.get(bucket, 0.0) + float(strength)

    if active_frames == 0 or float(np.max(chroma)) <= 1e-12:
        return _empty_fingerprint("No strong chord peaks detected", rms=total_rms)

    chroma_vector = _normalize_vector(chroma)
    max_chroma = float(np.max(chroma_vector)) if chroma_vector else 0.0
    pitch_class_strengths = {
        PITCH_CLASSES[index]: round(value / max_chroma, 3)
        for index, value in enumerate(chroma_vector)
        if max_chroma > 0 and value >= max_chroma * 0.2
    }
    pitch_classes = [
        name
        for name, _strength in sorted(
            pitch_class_strengths.items(),
            key=lambda item: item[1],
            reverse=True,
        )
    ][:8]

    dominant_frequencies = [
        frequency
        for frequency, _strength in sorted(
            peak_strengths.items(),
            key=lambda item: item[1],
            reverse=True,
        )
    ][:12]

    confidence_baseline = 0.0
    if pitch_class_strengths:
        confidence_baseline = float(statistics.fmean(pitch_class_strengths.values()))

    return {
        "dominant_frequencies": [round(float(value), 2) for value in dominant_frequencies],
        "pitch_classes": pitch_classes,
        "pitch_class_strengths": pitch_class_strengths,
        "spectral_centroid_avg": round(float(statistics.fmean(centroid_values)), 3)
        if centroid_values
        else 0.0,
        "chroma_vector": [round(float(value), 6) for value in chroma_vector],
        "confidence_baseline": round(confidence_baseline, 3),
        "rms": round(total_rms, 5),
        "active_frames": active_frames,
        "message": "Chord fingerprint extracted",
    }


def _analyze_fft_frame(
    frame: Any,
    sample_rate: int,
    *,
    min_frequency_hz: float,
    max_frequency_hz: float,
) -> tuple[Any, list[tuple[float, float]], float]:
    frame = np.asarray(frame, dtype=np.float32)
    frame = frame - float(np.mean(frame))
    windowed = frame * np.hanning(len(frame))
    spectrum = np.abs(np.fft.rfft(windowed))
    frequencies = np.fft.rfftfreq(len(windowed), d=1.0 / sample_rate)

    mask = (frequencies >= min_frequency_hz) & (frequencies <= max_frequency_hz)
    masked_freqs = frequencies[mask]
    masked_mags = spectrum[mask]
    if len(masked_mags) < 3 or float(np.max(masked_mags)) <= 1e-12:
        return np.zeros(12, dtype=np.float64), [], 0.0

    centroid = float(np.sum(masked_freqs * masked_mags) / max(float(np.sum(masked_mags)), 1e-12))
    peaks = _find_spectral_peaks(masked_freqs, masked_mags)
    chroma = np.zeros(12, dtype=np.float64)

    for frequency_hz, magnitude in peaks:
        midi_note = frequency_to_midi(float(frequency_hz))
        if not math.isfinite(midi_note):
            continue
        pitch_index = int(round(midi_note)) % 12
        # Lower fundamentals are usually more informative than very bright
        # harmonics, so reduce weight as frequency rises.
        weight = float(magnitude) / math.sqrt(max(float(frequency_hz), 1.0))
        chroma[pitch_index] += weight

    return chroma, peaks, centroid


def _find_spectral_peaks(
    frequencies: Any,
    magnitudes: Any,
    *,
    max_peaks: int = 18,
    relative_threshold: float = 0.12,
) -> list[tuple[float, float]]:
    max_magnitude = float(np.max(magnitudes))
    if max_magnitude <= 1e-12:
        return []

    threshold = max_magnitude * relative_threshold
    candidates: list[tuple[float, float]] = []
    for index in range(1, len(magnitudes) - 1):
        current = float(magnitudes[index])
        if (
            current >= threshold
            and current >= float(magnitudes[index - 1])
            and current >= float(magnitudes[index + 1])
        ):
            candidates.append((float(frequencies[index]), current))

    candidates.sort(key=lambda item: item[1], reverse=True)
    selected: list[tuple[float, float]] = []
    for frequency_hz, magnitude in candidates:
        if all(abs(frequency_hz - existing[0]) >= 8.0 for existing in selected):
            selected.append((frequency_hz, magnitude))
        if len(selected) >= max_peaks:
            break
    return selected


def _normalize_vector(vector: Any) -> list[float]:
    values = np.asarray(vector, dtype=np.float64)
    norm = float(np.linalg.norm(values))
    if norm <= 1e-12:
        return [0.0] * 12
    return [float(value) for value in values / norm]


def _empty_fingerprint(message: str, *, rms: float = 0.0) -> dict[str, Any]:
    return {
        "dominant_frequencies": [],
        "pitch_classes": [],
        "pitch_class_strengths": {},
        "spectral_centroid_avg": 0.0,
        "chroma_vector": [0.0] * 12,
        "confidence_baseline": 0.0,
        "rms": round(float(rms), 5),
        "active_frames": 0,
        "message": message,
    }


def _rms(samples: Any) -> float:
    if np is None:
        return 0.0
    values = np.asarray(samples, dtype=np.float32).reshape(-1)
    return float(np.sqrt(np.mean(np.square(values)))) if len(values) else 0.0


def _previous_power_of_two(value: int) -> int:
    if value <= 1:
        return 1
    return 1 << (int(value).bit_length() - 1)


class PitchProcessor:
    """Single-note detector with RMS gating and rolling-median smoothing."""

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
                pass

    def process(self, samples: Any) -> dict[str, Any]:
        """Process one audio block and return a UI-friendly pitch payload."""
        block = np.asarray(samples, dtype=np.float32).reshape(-1)
        if len(block) < self.hop_size:
            block = np.pad(block, (0, self.hop_size - len(block)))
        elif len(block) > self.hop_size:
            block = block[: self.hop_size]

        rms = _rms(block)
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


class RealtimeChordProcessor:
    """Maintains a rolling audio window for live chord chroma analysis."""

    def __init__(
        self,
        *,
        sample_rate: int,
        window_seconds: float,
        rms_threshold: float,
        analysis_interval_seconds: float = 0.18,
    ) -> None:
        if np is None:
            raise RuntimeError(f"NumPy is unavailable: {NUMPY_IMPORT_ERROR}")

        self.sample_rate = sample_rate
        self.rms_threshold = rms_threshold
        self._audio_window = np.zeros(int(sample_rate * window_seconds), dtype=np.float32)
        self._analysis_interval_samples = max(1, int(sample_rate * analysis_interval_seconds))
        self._samples_since_analysis = self._analysis_interval_samples
        self._last_result = {
            "clear": False,
            "fingerprint": _empty_fingerprint("Waiting for enough audio"),
            "strong_pitch_class_count": 0,
            "message": "Listening for chord spectrum...",
        }

    def process(self, samples: Any) -> dict[str, Any]:
        block = np.asarray(samples, dtype=np.float32).reshape(-1)
        if len(block) >= len(self._audio_window):
            self._audio_window = block[-len(self._audio_window) :].copy()
        else:
            self._audio_window = np.roll(self._audio_window, -len(block))
            self._audio_window[-len(block) :] = block

        self._samples_since_analysis += len(block)
        if self._samples_since_analysis < self._analysis_interval_samples:
            return self._last_result
        self._samples_since_analysis = 0

        window_rms = _rms(self._audio_window)
        if window_rms < self.rms_threshold:
            self._last_result = {
                "clear": False,
                "fingerprint": _empty_fingerprint(
                    "Signal below noise threshold",
                    rms=window_rms,
                ),
                "strong_pitch_class_count": 0,
                "message": "No chord energy detected",
            }
            return self._last_result

        try:
            fingerprint = extract_chord_fingerprint(
                self._audio_window,
                self.sample_rate,
                rms_threshold=self.rms_threshold,
            )
        except Exception as exc:
            self._last_result = {
                "clear": False,
                "fingerprint": _empty_fingerprint("FFT chord analysis failed", rms=window_rms),
                "strong_pitch_class_count": 0,
                "message": "FFT chord analysis failed",
                "error": str(exc),
            }
            return self._last_result

        strong_count = len(fingerprint.get("pitch_classes", []))
        clear = strong_count >= 2 and fingerprint.get("confidence_baseline", 0.0) > 0
        self._last_result = {
            "clear": clear,
            "fingerprint": fingerprint,
            "strong_pitch_class_count": strong_count,
            "message": "Chord-like spectrum detected" if clear else "Listening for chord spectrum...",
        }
        return self._last_result


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
        chord_calibration_seconds: float = 5.0,
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
        self.chord_calibration_seconds = chord_calibration_seconds
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

    def start_single_calibration(
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

            self._mode = "single_calibration"
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_single_calibration,
                args=(string_label, note, fret, pitch_callback, completion_callback, status_callback),
                daemon=True,
            )
            self._thread.start()

        return True, "Single-note calibration started."

    # Backward-compatible name from the first app version.
    def start_calibration(
        self,
        string_label: str,
        note: str,
        fret: int,
        pitch_callback: PitchCallback,
        completion_callback: CalibrationCallback,
        status_callback: StatusCallback,
    ) -> tuple[bool, str]:
        return self.start_single_calibration(
            string_label,
            note,
            fret,
            pitch_callback,
            completion_callback,
            status_callback,
        )

    def start_chord_calibration(
        self,
        chord_name: str,
        expected_notes: list[str],
        chord_callback: PitchCallback,
        completion_callback: ChordCalibrationCallback,
        status_callback: StatusCallback,
    ) -> tuple[bool, str]:
        available, message = self.audio_available()
        if not available:
            return False, message

        with self._lock:
            if self._mode != "idle":
                return False, f"Audio engine is busy in {self._mode} mode."

            self._mode = "chord_calibration"
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_chord_calibration,
                args=(chord_name, expected_notes, chord_callback, completion_callback, status_callback),
                daemon=True,
            )
            self._thread.start()

        return True, "Chord calibration started."

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

    def _run_single_calibration(
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
            processor = self._make_pitch_processor()
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
                self.logger.warning("Audio callback status during single calibration: %s", status)
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
                "mode": "single_calibration",
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
                                "mode": "single_calibration",
                                "seconds_remaining": round(seconds_remaining, 1),
                                "stable_samples": sample_count,
                            }
                        )
                        self._safe_pitch(pitch_callback, payload)

            with data_lock:
                stable_values = list(readings)
            summary = self._summarize_stable_values(stable_values)

            if summary is None:
                completion_callback(
                    None,
                    {
                        "samples": len(stable_values),
                        "message": "Calibration did not capture a stable pitch. Try a clean single note.",
                    },
                )
            else:
                average_hz, std_dev = summary
                completion_callback(
                    round(average_hz, 2),
                    {
                        "samples": len(stable_values),
                        "std_dev": round(std_dev, 3),
                        "message": "Single-note calibration captured successfully.",
                    },
                )
        except Exception as exc:
            if self.logger:
                self.logger.exception("Single calibration stream failed")
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

    def _run_chord_calibration(
        self,
        chord_name: str,
        expected_notes: list[str],
        chord_callback: PitchCallback,
        completion_callback: ChordCalibrationCallback,
        status_callback: StatusCallback,
    ) -> None:
        blocks: list[Any] = []
        latest_rms = 0.0
        data_lock = threading.Lock()

        def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            nonlocal latest_rms
            if status and self.logger:
                self.logger.warning("Audio callback status during chord calibration: %s", status)
            try:
                mono = np.asarray(indata[:, 0], dtype=np.float32).copy()
                with data_lock:
                    blocks.append(mono)
                    latest_rms = _rms(mono)
            except Exception:
                if self.logger:
                    self.logger.exception("Failed to capture chord calibration block")

        self._safe_status(
            status_callback,
            {
                "level": "info",
                "mode": "chord_calibration",
                "message": f"Strum {chord_name} several times for 5 seconds.",
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
                deadline = time.monotonic() + self.chord_calibration_seconds
                while time.monotonic() < deadline and not self._stop_event.is_set():
                    time.sleep(self.emit_interval_seconds)
                    seconds_remaining = max(0.0, deadline - time.monotonic())
                    with data_lock:
                        rms_value = latest_rms
                        block_count = len(blocks)
                    self._safe_pitch(
                        chord_callback,
                        {
                            "mode": "chord_calibration",
                            "chord_name": chord_name,
                            "seconds_remaining": round(seconds_remaining, 1),
                            "rms": round(rms_value, 5),
                            "captured_blocks": block_count,
                            "message": "Capturing chord strums...",
                        },
                    )

            with data_lock:
                captured = [block.copy() for block in blocks]
            if not captured:
                completion_callback(
                    None,
                    {
                        "message": "Chord calibration did not capture audio.",
                        "samples": 0,
                    },
                )
                return

            audio_buffer = np.concatenate(captured)
            fingerprint = extract_chord_fingerprint(
                audio_buffer,
                self.sample_rate,
                rms_threshold=self.rms_threshold,
            )
            if len(fingerprint.get("pitch_classes", [])) < 2:
                completion_callback(
                    None,
                    {
                        "message": "Chord calibration did not find enough strong pitch classes.",
                        "samples": len(audio_buffer),
                        "fingerprint": fingerprint,
                    },
                )
                return

            completion_callback(
                fingerprint,
                {
                    "message": "Chord calibration captured successfully.",
                    "samples": len(audio_buffer),
                    "expected_notes": expected_notes,
                },
            )
        except Exception as exc:
            if self.logger:
                self.logger.exception("Chord calibration stream failed")
            completion_callback(
                None,
                {
                    "samples": 0,
                    "message": f"Unable to open microphone for chord calibration: {exc}",
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
            pitch_processor = self._make_pitch_processor()
            chord_processor = RealtimeChordProcessor(
                sample_rate=self.sample_rate,
                window_seconds=1.1,
                rms_threshold=self.rms_threshold,
            )
        except Exception as exc:
            if self.logger:
                self.logger.exception("Unable to initialize detection processors")
            self._safe_status(
                status_callback,
                {
                    "level": "error",
                    "mode": "detection",
                    "message": f"Unable to initialize detection: {exc}",
                },
            )
            self._mark_idle()
            return

        def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            nonlocal latest_payload
            if status and self.logger:
                self.logger.warning("Audio callback status during detection: %s", status)
            try:
                mono = np.asarray(indata[:, 0], dtype=np.float32)
                pitch_payload = pitch_processor.process(mono)
                chord_payload = chord_processor.process(mono)
                payload = {
                    **pitch_payload,
                    "mode": "detection",
                    "single_note": pitch_payload,
                    "chord": chord_payload,
                    "timestamp": time.time(),
                }
            except Exception as exc:
                if self.logger:
                    self.logger.exception("Detection processing failed")
                payload = self._error_pitch_payload("Detection processing failed", exc)
                payload["mode"] = "detection"
                payload["single_note"] = payload.copy()
                payload["chord"] = {
                    "clear": False,
                    "fingerprint": _empty_fingerprint("FFT chord analysis failed"),
                    "strong_pitch_class_count": 0,
                    "message": "FFT chord analysis failed",
                }

            with data_lock:
                latest_payload = payload

        self._safe_status(
            status_callback,
            {
                "level": "success",
                "mode": "detection",
                "message": (
                    f"Detection started using {pitch_processor.detector_name} plus FFT chroma. "
                    "Play one clean note or strum one clear chord."
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

    def _make_pitch_processor(self) -> PitchProcessor:
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
            "detector_confidence": 0.0,
            "detector": "unknown",
            "spread_cents": None,
            "message": message,
            "error": str(exc),
            "timestamp": time.time(),
        }

    @staticmethod
    def _summarize_stable_values(values: list[float]) -> tuple[float, float] | None:
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

        average = float(statistics.fmean(best_values))
        std_dev = float(statistics.pstdev(best_values)) if len(best_values) > 1 else 0.0
        return average, std_dev
