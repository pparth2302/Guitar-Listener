# Guitar Fret Trainer

A local Flask web app that listens through your computer microphone and detects either calibrated single guitar frets or calibrated chords.

Single-note calibration and chord calibration are stored separately:

```json
{
  "single_notes": [],
  "chords": []
}
```

Old calibration files that were a plain list of single-note rows are migrated automatically into `single_notes`.

## Features

- Single Note / Fret Calibration for E2, A2, D3, G3, B3, and E4 strings.
- Chord Calibration for common chords such as Em, E, Am, A, A9, C, G, D, Dm, F, B7, plus custom chord names.
- Chord fingerprints based on FFT peaks and 12-bin chroma vectors.
- One Detection Mode that automatically decides between Single Note, Chord, and Unknown.
- Real-time note detection with note plus octave display, such as `E2`, `F#3`, or `C4`.
- Visual fretboard highlighting for strings and frets 0 through 15.
- Frequency validation that lowers confidence if the detected frequency note disagrees with the string/fret note.
- Separate saved tables for single-note calibration rows and chord fingerprints.
- Realtime browser updates through Flask-SocketIO.
- JSON storage in `calibration_data.json`.
- Mic is off on page load and starts only after Start Calibration or Start Detection.

## Install

```powershell
cd "D:\Software\Guitar Listener\guitar_fret_trainer"
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

On macOS/Linux:

```bash
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

## Single-Note Calibration

1. Open Calibration Mode.
2. Use the Single Note / Fret card.
3. Choose the guitar string and fret.
4. Click Start Single Calibration.
5. Play one clean note and let it ring for the 3 second capture.

The app averages stable pitch readings and saves frequency plus standard deviation under `single_notes`.

## Chord Calibration

1. Open Calibration Mode.
2. Use the Chord Fingerprint card.
3. Choose a common chord or type a custom chord name.
4. Optionally enter expected notes, comma-separated.
5. Click Start Chord Calibration.
6. Strum the chord multiple times for about 5 seconds.

Chord calibration uses NumPy FFT analysis, maps frequency peaks to pitch classes, builds a normalized chroma vector, and stores the fingerprint under `chords`.

## Detection

Detection Mode listens continuously and evaluates both paths:

- Single-note result: stable pitch compared to saved `single_notes` using cents error.
- Chord result: current chroma vector compared to saved chord fingerprints using cosine similarity.

Decision rules:

- If one dominant stable pitch matches a saved fret, the app highlights Single Note.
- If multiple strong pitch classes match a saved chord above threshold, the app highlights Chord.
- If both are weak, the app shows Unknown.
- If no new sound is detected, the UI holds the previous clear result.

Current Note examples:

- Low E fret 3: G.
- A fret 3: C.
- D fret 2: E.
- G fret 5: C.
- B fret 1: C.
- High E fret 12: E.

The Current Note card and fretboard highlight clear to `--` when single-note confidence is low or no clear note is detected.

Chord confidence:

- 85% or higher: confident chord match.
- 70% to 84%: possible chord match.
- Below 70%: uncertain.

## Audio Notes

- Audio input uses `sounddevice`.
- Sample rate is 44100 Hz.
- Single-note pitch detection prefers `aubio` `yinfft`.
- On Windows, if `aubio` is not installed, the app uses a NumPy autocorrelation fallback.
- Chord analysis uses NumPy FFT/chroma and does not require SciPy.

## Troubleshooting

- Tune the guitar first.
- Calibrate in a quiet room.
- Use the same microphone position during calibration and testing.
- Strum chords cleanly and let strings ring naturally.
- Mute other strings for single-note testing.
- If no microphone is found, check OS microphone permissions and input device selection.
- If audio is busy, refresh the page. The app asks the backend to stop stale audio streams on page load.
- If `aubio` install fails on Windows, install Microsoft C++ Build Tools or use the built-in NumPy fallback.

Recommended Windows microphone settings:

- 1 channel.
- 16 bit.
- 48000 Hz if available.
- Disable audio enhancements.
- Disable exclusive control if the app has microphone issues.

## API

- `GET /api/calibration`
- `POST /api/calibration/single/start`
- `POST /api/calibration/chord/start`
- `POST /api/calibration/reset-single`
- `POST /api/calibration/reset-chords`
- `POST /api/calibration/reset-all`
- `POST /api/calibration/chord/delete`
- `POST /api/detection/start`
- `POST /api/detection/stop`
- `POST /api/audio/stop`

Socket.IO events:

- `pitch_update`
- `single_note_result`
- `chord_result`
- `detection_result`
- `single_calibration_saved`
- `chord_calibration_saved`
- `status`
- `error`
