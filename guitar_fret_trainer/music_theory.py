"""Small music-theory helpers for standard-tuned guitar notes."""

from __future__ import annotations

import math
from typing import Any

CHROMATIC_SCALE = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# MIDI note numbers for standard tuning.
OPEN_STRING_MIDI = {
    "Low E": 40,   # E2
    "A": 45,       # A2
    "D": 50,       # D3
    "G": 55,       # G3
    "B": 59,       # B3
    "High E": 64,  # E4
}


def get_note_from_string_and_fret(string_name: str, fret: Any) -> dict[str, Any]:
    """Return note metadata for a standard-tuned guitar string/fret.

    Bad input returns a safe placeholder so UI code does not need special
    exception handling.
    """
    try:
        fret_number = int(fret)
    except (TypeError, ValueError):
        return _unknown_note()

    open_midi = OPEN_STRING_MIDI.get(str(string_name))
    if open_midi is None or fret_number < 0:
        return _unknown_note()

    return note_from_midi(open_midi + fret_number)


def note_from_frequency(frequency_hz: Any) -> dict[str, Any]:
    """Return nearest equal-temperament note for a frequency."""
    try:
        frequency = float(frequency_hz)
    except (TypeError, ValueError):
        return _unknown_note()

    if frequency <= 0 or not math.isfinite(frequency):
        return _unknown_note()

    midi_note = int(round(69 + 12 * math.log2(frequency / 440.0)))
    return note_from_midi(midi_note)


def frequency_from_note_info(note_info: dict[str, Any]) -> float | None:
    """Convert note metadata back to frequency when possible."""
    midi_note = note_info.get("midi")
    if midi_note is None:
        return None
    return 440.0 * (2 ** ((int(midi_note) - 69) / 12))


def note_from_midi(midi_note: int) -> dict[str, Any]:
    note = CHROMATIC_SCALE[midi_note % 12]
    octave = (midi_note // 12) - 1
    return {
        "note": note,
        "octave": octave,
        "full_note": f"{note}{octave}",
        "midi": midi_note,
    }


def _unknown_note() -> dict[str, Any]:
    return {
        "note": "--",
        "octave": None,
        "full_note": "--",
        "midi": None,
    }
