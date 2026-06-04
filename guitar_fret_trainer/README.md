# Guitar Fret Trainer

A local Flask web app that listens through your computer microphone, detects guitar pitch with `aubio` when available, and maps the detected frequency to your calibrated guitar string/fret references. On Windows systems where `aubio` cannot install without native build tools, the app falls back to a built-in NumPy pitch detector.

## Features

- Calibration Mode for E2 low E, A2, D3, G3, B3, and E4 high E strings.
- Fret calibration from 0 to 12.
- Three second calibration capture with stable-pitch averaging.
- JSON storage in `calibration_data.json`.
- Test / Detection Mode with live frequency, closest string/fret, confidence, and cents error.
- Realtime browser updates through Flask-SocketIO.
- Graceful handling for missing calibration data, no microphone, silence, noisy readings, and pitch detector errors.

## Project Structure

```text
guitar_fret_trainer/
  app.py
  audio_engine.py
  calibration_store.py
  requirements.txt
  calibration_data.json
  templates/
    index.html
  static/
    style.css
    app.js
  README.md
```

## Installation

From this folder:

```powershell
cd guitar_fret_trainer
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

On macOS/Linux, activate the virtual environment with:

```bash
source venv/bin/activate
```

Then open:

```text
http://127.0.0.1:5000
```

## How To Use

1. Open Calibration Mode.
2. Select a guitar string and fret from 0 to 12.
3. Click Start Calibration.
4. Play one clean note and let it ring for the 3 second capture.
5. Repeat for as many string/fret positions as you want to detect.
6. Open Test / Detection Mode.
7. Click Start Detection and play one note at a time.

The detector compares the current stable pitch against saved calibration rows using cents difference:

```text
cents = 1200 * log2(detected_frequency / calibrated_frequency)
```

Confidence is mapped roughly like this:

- 0 to 5 cents: 100%
- 10 cents: about 90%
- 25 cents: about 60%
- More than 50 cents: low confidence

## Troubleshooting

- Microphone permission issue: allow microphone access for your terminal, Python, or browser depending on your operating system prompts.
- No audio input device found: connect or enable a microphone, then restart the app.
- aubio install issue: on Windows, `aubio` often requires Microsoft C++ Build Tools because PyPI may only provide source builds. The app can run without `aubio` on Windows by using its NumPy fallback detector. If you specifically want `aubio`, install Microsoft C++ Build Tools, then run `pip install aubio`.
- No clear note detected: increase input volume, move closer to the guitar, and avoid clipping.
- Use a quiet room: background noise can defeat the RMS gate and pitch stability checks.
- Play one note at a time: mute adjacent strings and avoid chords during calibration or detection.
- Tune guitar first: the app detects closest saved calibration references, so tuning drift lowers confidence.

## Notes

- Audio input uses `sounddevice` at 44100 Hz with a 2048 sample pitch window and 512 sample hop size.
- Pitch detection prefers aubio `yinfft`; if `aubio` is unavailable, a NumPy autocorrelation fallback is used.
- Readings below the RMS threshold are treated as silence/noise.
- Stable pitch is smoothed with a short rolling median and rejected when readings jump too much.
- The microphone is off on page load. It starts only after Start Calibration or Start Detection, and the app asks the backend to stop audio again when the page closes or reloads.
- The browser UI loads the Socket.IO client from the Socket.IO CDN. If you need fully offline browser assets, download the matching Socket.IO client into `static/` and update `templates/index.html`.
