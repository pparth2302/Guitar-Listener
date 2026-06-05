const state = {
  strings: [],
  frets: [],
  commonChords: [],
  singleNotes: [],
  chords: [],
  currentMode: "calibration",
  singleCalibrationRunning: false,
  chordCalibrationRunning: false,
  detectionRunning: false,
  lastDetectionResult: null,
  latestSingleResult: null,
  latestChordResult: null,
};

const els = {};
const FRETBOARD_STRINGS = ["High E", "B", "G", "D", "A", "Low E"];
const CHROMATIC_SCALE = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const OPEN_STRING_MIDI = {
  "Low E": 40,
  A: 45,
  D: 50,
  G: 55,
  B: 59,
  "High E": 64,
};
const MAX_FRETBOARD_FRET = 15;
const MIN_NOTE_CONFIDENCE = 60;

document.addEventListener("DOMContentLoaded", async () => {
  cacheElements();
  buildFretboard();
  bindEvents();
  bindLifecycleEvents();
  await stopAudioOnPageLoad();
  connectSocket();
  loadCalibration();
});

function cacheElements() {
  els.connectionBadge = document.querySelector("#connectionBadge");
  els.statusBar = document.querySelector("#statusBar");
  els.calibrationTab = document.querySelector("#calibrationTab");
  els.detectionTab = document.querySelector("#detectionTab");
  els.calibrationPanel = document.querySelector("#calibrationPanel");
  els.detectionPanel = document.querySelector("#detectionPanel");

  els.singleCalibrationForm = document.querySelector("#singleCalibrationForm");
  els.stringSelect = document.querySelector("#stringSelect");
  els.fretSelect = document.querySelector("#fretSelect");
  els.startSingleCalibration = document.querySelector("#startSingleCalibration");
  els.resetSingleCalibration = document.querySelector("#resetSingleCalibration");

  els.chordCalibrationForm = document.querySelector("#chordCalibrationForm");
  els.chordSelect = document.querySelector("#chordSelect");
  els.customChordName = document.querySelector("#customChordName");
  els.expectedNotesInput = document.querySelector("#expectedNotesInput");
  els.startChordCalibration = document.querySelector("#startChordCalibration");
  els.resetChordCalibration = document.querySelector("#resetChordCalibration");
  els.resetAllCalibration = document.querySelector("#resetAllCalibration");

  els.calibrationFrequency = document.querySelector("#calibrationFrequency");
  els.calibrationMeter = document.querySelector("#calibrationMeter");
  els.calibrationHint = document.querySelector("#calibrationHint");
  els.chordCalibrationHint = document.querySelector("#chordCalibrationHint");

  els.singleCalibrationRows = document.querySelector("#singleCalibrationRows");
  els.singleCalibrationCount = document.querySelector("#singleCalibrationCount");
  els.chordCalibrationRows = document.querySelector("#chordCalibrationRows");
  els.chordCalibrationCount = document.querySelector("#chordCalibrationCount");

  els.startDetection = document.querySelector("#startDetection");
  els.stopDetection = document.querySelector("#stopDetection");
  els.detectionCard = document.querySelector("#detectionCard");
  els.detectedType = document.querySelector("#detectedType");
  els.detectedTitle = document.querySelector("#detectedTitle");
  els.detectionMessage = document.querySelector("#detectionMessage");
  els.currentNoteCard = document.querySelector("#currentNoteCard");
  els.currentNoteName = document.querySelector("#currentNoteName");
  els.currentFullNote = document.querySelector("#currentFullNote");
  els.currentNoteHint = document.querySelector("#currentNoteHint");
  els.fretboard = document.querySelector("#fretboard");
  els.fretboardStatus = document.querySelector("#fretboardStatus");

  els.singleGuessCard = document.querySelector("#singleGuessCard");
  els.singleGuessTitle = document.querySelector("#singleGuessTitle");
  els.singleFrequency = document.querySelector("#singleFrequency");
  els.singleConfidence = document.querySelector("#singleConfidence");
  els.singleCentsError = document.querySelector("#singleCentsError");
  els.singleStatus = document.querySelector("#singleStatus");

  els.chordGuessCard = document.querySelector("#chordGuessCard");
  els.chordGuessTitle = document.querySelector("#chordGuessTitle");
  els.chordPitchClasses = document.querySelector("#chordPitchClasses");
  els.chordConfidence = document.querySelector("#chordConfidence");
  els.closestChord = document.querySelector("#closestChord");
  els.chordStatus = document.querySelector("#chordStatus");
}

function bindEvents() {
  els.calibrationTab.addEventListener("click", () => switchMode("calibration"));
  els.detectionTab.addEventListener("click", () => switchMode("detection"));
  els.singleCalibrationForm.addEventListener("submit", startSingleCalibration);
  els.chordCalibrationForm.addEventListener("submit", startChordCalibration);
  els.resetSingleCalibration.addEventListener("click", resetSingleCalibration);
  els.resetChordCalibration.addEventListener("click", resetChordCalibration);
  els.resetAllCalibration.addEventListener("click", resetAllCalibration);
  els.startDetection.addEventListener("click", startDetection);
  els.stopDetection.addEventListener("click", stopDetection);
  els.chordSelect.addEventListener("change", syncCommonChordFields);
  els.chordCalibrationRows.addEventListener("click", handleChordTableClick);
}

function bindLifecycleEvents() {
  window.addEventListener("pagehide", stopAudioForUnload);
}

async function stopAudioOnPageLoad() {
  try {
    const data = await requestJson("/api/audio/stop", { method: "POST" });
    setDetectionRunning(false);
    resetDetectionDisplay("Mic is off. Press Start Detection when you are ready.", {
      clearHeldResult: true,
    });

    if (data.stopped) {
      showStatus("Previous audio stream stopped. Mic is off.", "info");
    } else {
      showStatus("Mic is off. Press Start Calibration or Start Detection to begin.", "info");
    }
  } catch (error) {
    showStatus(`Could not confirm the mic is off: ${error.message}`, "warning");
  }
}

function stopAudioForUnload() {
  const payload = "{}";
  const blob = new Blob([payload], { type: "application/json" });

  if (navigator.sendBeacon && navigator.sendBeacon("/api/audio/stop", blob)) {
    return;
  }

  fetch("/api/audio/stop", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: payload,
    keepalive: true,
  }).catch(() => {});
}

function connectSocket() {
  if (!window.io) {
    setConnection(false, "Socket.IO client missing");
    showStatus(
      "Socket.IO client failed to load. Check your network connection or download the client locally.",
      "error"
    );
    return;
  }

  const socket = window.io();

  socket.on("connect", () => {
    setConnection(true, "Connected");
  });

  socket.on("disconnect", () => {
    setConnection(false, "Disconnected");
    showStatus("Browser disconnected from the local server.", "error");
    setDetectionRunning(false);
  });

  socket.on("status", (payload) => {
    showStatus(payload.message || "Status update received.", payload.level || "info");

    if (payload.mode === "detection" && /stopped|unable/i.test(payload.message || "")) {
      setDetectionRunning(false);
    }
    if (
      payload.mode === "single_calibration" &&
      /saved|did not capture|unable/i.test(payload.message || "")
    ) {
      setSingleCalibrationRunning(false);
    }
    if (
      payload.mode === "chord_calibration" &&
      /saved|did not|unable/i.test(payload.message || "")
    ) {
      setChordCalibrationRunning(false);
    }
  });

  socket.on("error", (payload) => {
    showStatus(payload.message || "Backend error", "error");
  });

  socket.on("pitch_update", handlePitchUpdate);
  socket.on("single_note_result", renderSingleResult);
  socket.on("chord_result", renderChordResult);
  socket.on("detection_result", renderDetectionResult);

  socket.on("single_calibration_saved", (payload) => {
    applyCalibrationPayload(payload.calibration || { single_notes: payload.single_notes });
    setSingleCalibrationRunning(false);
  });

  socket.on("chord_calibration_saved", (payload) => {
    applyCalibrationPayload(payload.calibration || { chords: payload.chords });
    setChordCalibrationRunning(false);
  });

  socket.on("calibration_saved", (payload) => {
    applyCalibrationPayload(payload.calibration || { single_notes: payload.entries });
    setSingleCalibrationRunning(false);
  });
}

async function loadCalibration() {
  try {
    const data = await requestJson("/api/calibration");
    state.strings = data.strings || [];
    state.frets = data.frets || [];
    state.commonChords = data.common_chords || [];
    state.singleNotes = data.single_notes || data.entries || [];
    state.chords = data.chords || [];
    populateControls();
    renderSingleCalibrationTable();
    renderChordCalibrationTable();
  } catch (error) {
    showStatus(error.message, "error");
  }
}

function applyCalibrationPayload(calibration) {
  if (Array.isArray(calibration.single_notes)) {
    state.singleNotes = calibration.single_notes;
    renderSingleCalibrationTable();
  }
  if (Array.isArray(calibration.chords)) {
    state.chords = calibration.chords;
    renderChordCalibrationTable();
  }
}

function populateControls() {
  els.stringSelect.innerHTML = state.strings
    .map((item) => `<option value="${escapeHtml(item.label)}">${escapeHtml(item.label)} (${escapeHtml(item.note)})</option>`)
    .join("");

  els.fretSelect.innerHTML = state.frets
    .map((fret) => `<option value="${fret}">${fret}</option>`)
    .join("");

  els.chordSelect.innerHTML = [
    `<option value="">Choose common chord</option>`,
    ...state.commonChords.map(
      (item) => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)}</option>`
    ),
  ].join("");
}

function buildFretboard() {
  const fretHeader = ["<div class=\"fretboard-corner\">String</div>"];
  for (let fret = 0; fret <= MAX_FRETBOARD_FRET; fret += 1) {
    fretHeader.push(`<div class="fret-number">${fret}</div>`);
  }

  const rows = FRETBOARD_STRINGS.flatMap((stringName) => {
    const cells = [
      `<div class="string-label" data-string-label="${escapeHtml(stringName)}">${escapeHtml(stringName)}</div>`,
    ];
    for (let fret = 0; fret <= MAX_FRETBOARD_FRET; fret += 1) {
      const noteInfo = getNoteFromStringAndFret(stringName, fret);
      cells.push(`
        <div
          class="fret-cell"
          data-string="${escapeHtml(stringName)}"
          data-fret="${fret}"
          data-note="${escapeHtml(noteInfo.note)}"
        >
          <span>${escapeHtml(noteInfo.note)}</span>
        </div>
      `);
    }
    return cells;
  });

  els.fretboard.innerHTML = [...fretHeader, ...rows].join("");
}

function getNoteFromStringAndFret(stringName, fret) {
  const openMidi = OPEN_STRING_MIDI[stringName];
  const fretNumber = Number(fret);
  if (!Number.isInteger(openMidi) || !Number.isFinite(fretNumber) || fretNumber < 0) {
    return { note: "--", octave: null, full_note: "--" };
  }

  const midi = openMidi + fretNumber;
  const note = CHROMATIC_SCALE[midi % 12];
  const octave = Math.floor(midi / 12) - 1;
  return { note, octave, full_note: `${note}${octave}` };
}

function syncCommonChordFields() {
  const selected = findCommonChord(els.chordSelect.value);
  if (!selected) {
    return;
  }

  if (!els.customChordName.value.trim()) {
    els.customChordName.value = selected.name;
  }
  els.expectedNotesInput.value = selected.expected_notes.join(", ");
}

async function startSingleCalibration(event) {
  event.preventDefault();

  const selectedString = state.strings.find((item) => item.label === els.stringSelect.value);
  if (!selectedString) {
    showStatus("Choose a valid guitar string.", "warning");
    return;
  }

  setSingleCalibrationRunning(true);
  setCalibrationReadout(null);
  els.calibrationHint.textContent = "Listening now. Play one clean note and let it ring.";

  try {
    await requestJson("/api/calibration/single/start", {
      method: "POST",
      body: JSON.stringify({
        string: selectedString.label,
        note: selectedString.note,
        fret: Number(els.fretSelect.value),
      }),
    });
  } catch (error) {
    setSingleCalibrationRunning(false);
    if (await recoverFromBusyAudio(error)) {
      return;
    }
    showStatus(error.message, "error");
  }
}

async function startChordCalibration(event) {
  event.preventDefault();

  const chordName = getSelectedChordName();
  if (!chordName) {
    showStatus("Choose or type a chord name.", "warning");
    return;
  }

  setChordCalibrationRunning(true);
  setCalibrationReadout(null);
  els.chordCalibrationHint.textContent = `Listening for ${chordName}. Strum several times.`;

  try {
    await requestJson("/api/calibration/chord/start", {
      method: "POST",
      body: JSON.stringify({
        chord_name: chordName,
        expected_notes: parseExpectedNotesInput(),
      }),
    });
  } catch (error) {
    setChordCalibrationRunning(false);
    if (await recoverFromBusyAudio(error)) {
      return;
    }
    showStatus(error.message, "error");
  }
}

async function resetSingleCalibration() {
  if (!window.confirm("Delete all saved single-note calibration data?")) {
    return;
  }

  try {
    const data = await requestJson("/api/calibration/reset-single", { method: "POST" });
    applyCalibrationPayload(data.calibration);
    resetDetectionDisplay("Single-note calibration data cleared.", { clearHeldResult: true });
  } catch (error) {
    showStatus(error.message, "error");
  }
}

async function resetChordCalibration() {
  if (!window.confirm("Delete all saved chord calibration data?")) {
    return;
  }

  try {
    const data = await requestJson("/api/calibration/reset-chords", { method: "POST" });
    applyCalibrationPayload(data.calibration);
    resetDetectionDisplay("Chord calibration data cleared.", { clearHeldResult: true });
  } catch (error) {
    showStatus(error.message, "error");
  }
}

async function resetAllCalibration() {
  if (!window.confirm("Delete all single-note and chord calibration data?")) {
    return;
  }

  try {
    const data = await requestJson("/api/calibration/reset-all", { method: "POST" });
    applyCalibrationPayload(data.calibration);
    resetDetectionDisplay("All calibration data cleared.", { clearHeldResult: true });
  } catch (error) {
    showStatus(error.message, "error");
  }
}

async function handleChordTableClick(event) {
  const button = event.target.closest("[data-delete-chord]");
  if (!button) {
    return;
  }

  const chordName = button.dataset.deleteChord;
  if (!window.confirm(`Delete chord calibration for ${chordName}?`)) {
    return;
  }

  try {
    const data = await requestJson("/api/calibration/chord/delete", {
      method: "POST",
      body: JSON.stringify({ chord_name: chordName }),
    });
    applyCalibrationPayload(data.calibration);
  } catch (error) {
    showStatus(error.message, "error");
  }
}

async function startDetection() {
  setDetectionRunning(true);
  resetDetectionDisplay("Listening for a single note or chord...", { clearHeldResult: true });

  try {
    await requestJson("/api/detection/start", { method: "POST" });
  } catch (error) {
    setDetectionRunning(false);
    if (await recoverFromBusyAudio(error)) {
      return;
    }
    showStatus(error.message, "error");
  }
}

async function stopDetection() {
  try {
    await requestJson("/api/detection/stop", { method: "POST" });
  } catch (error) {
    showStatus(error.message, "error");
  } finally {
    setDetectionRunning(false);
  }
}

async function recoverFromBusyAudio(error) {
  if (!/Audio engine is busy/i.test(error.message || "")) {
    return false;
  }

  try {
    const data = await requestJson("/api/audio/stop", { method: "POST" });
    setSingleCalibrationRunning(false);
    setChordCalibrationRunning(false);
    setDetectionRunning(false);
    showStatus(
      data.stopped
        ? "Stopped the active mic stream. Press Start again when you are ready."
        : "Mic is already off. Press Start again when you are ready.",
      "warning"
    );
  } catch (stopError) {
    showStatus(`Audio is busy and could not be stopped: ${stopError.message}`, "error");
  }

  return true;
}

function switchMode(mode) {
  state.currentMode = mode;
  const calibrationActive = mode === "calibration";

  els.calibrationTab.classList.toggle("active", calibrationActive);
  els.detectionTab.classList.toggle("active", !calibrationActive);
  els.calibrationPanel.classList.toggle("active", calibrationActive);
  els.detectionPanel.classList.toggle("active", !calibrationActive);
}

function handlePitchUpdate(payload) {
  const frequency =
    payload.smoothed_frequency_hz ||
    payload.frequency_hz ||
    payload.single_note?.smoothed_frequency_hz ||
    payload.single_note?.frequency_hz;

  if (payload.mode === "single_calibration") {
    setCalibrationReadout(frequency);
    const remaining = payload.seconds_remaining ?? 0;
    const stableSamples = payload.stable_samples ?? 0;
    els.calibrationHint.textContent = payload.stable
      ? `Stable tone captured. ${remaining.toFixed(1)}s left, ${stableSamples} stable samples.`
      : `${payload.message || "Listening..."} ${remaining.toFixed(1)}s left.`;
    return;
  }

  if (payload.mode === "chord_calibration") {
    updateFrequencyMeter(els.calibrationMeter, (payload.rms || 0) * 10000);
    const remaining = payload.seconds_remaining ?? 0;
    els.chordCalibrationHint.textContent =
      `${payload.message || "Capturing chord..."} ${remaining.toFixed(1)}s left. RMS ${formatSmallNumber(payload.rms)}.`;
  }
}

function renderDetectionResult(result) {
  if (shouldHoldPreviousResult(result)) {
    renderHeldDetectionResult();
    return;
  }

  if (result.type !== "Unknown") {
    state.lastDetectionResult = result;
  }

  els.detectionCard.dataset.state = result.status || "idle";
  els.detectedType.textContent = `Type: ${result.type || "Unknown"}`;

  if (result.type === "Single Note") {
    const single = result.primary || result.single_note || {};
    els.detectedTitle.textContent = single.string
      ? `${single.string} / Fret ${single.fret}`
      : "--";
    els.detectionMessage.textContent = result.message || single.message || "Single note detected";
    updateNoteVisuals(single);
    setGuessHighlight("single");
    return;
  }

  if (result.type === "Chord") {
    const chord = result.primary || result.chord || {};
    els.detectedTitle.textContent = chord.chord_name || chord.closest_chord_name || "--";
    els.detectionMessage.textContent = result.message || chord.message || "Chord detected";
    resetNoteVisuals("Chord detected. Current Note shows single-note detections only.");
    setGuessHighlight("chord");
    return;
  }

  els.detectedTitle.textContent = "--";
  els.detectionMessage.textContent =
    result.message || "Uncertain - play one clean note or strum one clear chord";
  resetNoteVisuals("No confident single note detected.");
  setGuessHighlight(null);
}

function renderSingleResult(result) {
  state.latestSingleResult = result;

  if (result.status === "ok" || result.status === "uncertain") {
    els.singleGuessTitle.textContent = `${result.string} / Fret ${result.fret}`;
    els.singleFrequency.textContent = formatFrequency(result.detected_frequency_hz);
    els.singleConfidence.textContent = `${result.confidence || 0}%`;
    els.singleCentsError.textContent =
      result.cents_error === null || result.cents_error === undefined
        ? "--"
        : `${result.cents_error > 0 ? "+" : ""}${Number(result.cents_error).toFixed(2)} cents`;
    els.singleStatus.textContent = result.message || result.status;
    return;
  }

  els.singleGuessTitle.textContent = "--";
  els.singleFrequency.textContent = result.detected_frequency_hz
    ? formatFrequency(result.detected_frequency_hz)
    : "-- Hz";
  els.singleConfidence.textContent = "0%";
  els.singleCentsError.textContent = "--";
  els.singleStatus.textContent = result.message || "No single note";
}

function renderChordResult(result) {
  state.latestChordResult = result;

  if (["confident", "possible", "uncertain"].includes(result.status)) {
    els.chordGuessTitle.textContent = result.chord_name || result.closest_chord_name || "--";
    els.chordPitchClasses.textContent = formatList(result.detected_pitch_classes);
    els.chordConfidence.textContent = `${result.confidence || 0}%`;
    els.closestChord.textContent = result.closest_chord_name || "--";
    els.chordStatus.textContent = result.message || result.status;
    return;
  }

  els.chordGuessTitle.textContent = "--";
  els.chordPitchClasses.textContent = formatList(result.detected_pitch_classes);
  els.chordConfidence.textContent = "0%";
  els.closestChord.textContent = "--";
  els.chordStatus.textContent = result.message || "No chord";
}

function shouldHoldPreviousResult(result) {
  if (!state.lastDetectionResult || result.type !== "Unknown") {
    return false;
  }

  const singleStatus = result.single_note?.status;
  const chordStatus = result.chord?.status;
  const singleConfidence = result.single_note?.confidence || 0;
  const chordConfidence = result.chord?.confidence || 0;

  return (
    singleStatus === "no_pitch" &&
    ["no_chord", "no_calibration"].includes(chordStatus) &&
    singleConfidence === 0 &&
    chordConfidence === 0
  );
}

function renderHeldDetectionResult() {
  const held = state.lastDetectionResult;
  els.detectionCard.dataset.state = "holding";
  els.detectedType.textContent = `Type: ${held.type} (held)`;

  if (held.type === "Single Note") {
    const single = held.primary || {};
    els.detectedTitle.textContent = single.string
      ? `${single.string} / Fret ${single.fret}`
      : "--";
    resetNoteVisuals("No new clear note detected.");
    setGuessHighlight("single");
  } else if (held.type === "Chord") {
    const chord = held.primary || {};
    els.detectedTitle.textContent = chord.chord_name || chord.closest_chord_name || "--";
    resetNoteVisuals("No new clear note detected.");
    setGuessHighlight("chord");
  }

  els.detectionMessage.textContent = "No new clear sound detected. Holding the last result.";
}

function updateNoteVisuals(singleResult) {
  if (
    !singleResult ||
    singleResult.status !== "ok" ||
    (singleResult.confidence || 0) < MIN_NOTE_CONFIDENCE ||
    !singleResult.string ||
    singleResult.fret === null ||
    singleResult.fret === undefined ||
    !singleResult.note ||
    singleResult.note === "--"
  ) {
    resetNoteVisuals("Confidence is low, so the current note is hidden.");
    return;
  }

  els.currentNoteCard.dataset.state = singleResult.validation_warning ? "warning" : "ok";
  els.currentNoteName.textContent = singleResult.note;
  els.currentFullNote.textContent = singleResult.full_note || "--";
  els.currentNoteHint.textContent = singleResult.validation_warning
    ? singleResult.validation_warning
    : `${singleResult.string} string, fret ${singleResult.fret}`;
  highlightFretboardPosition(singleResult.string, singleResult.fret, singleResult.note);
}

function resetNoteVisuals(message = "Play one clean single note to show note name and fret position.") {
  els.currentNoteCard.dataset.state = "idle";
  els.currentNoteName.textContent = "--";
  els.currentFullNote.textContent = "--";
  els.currentNoteHint.textContent = message;
  clearFretboardHighlight();
  els.fretboardStatus.textContent = "Waiting";
}

function highlightFretboardPosition(stringName, fret, note) {
  clearFretboardHighlight();
  const fretNumber = Number(fret);
  const cell = Array.from(els.fretboard.querySelectorAll(".fret-cell")).find(
    (element) => element.dataset.string === stringName && Number(element.dataset.fret) === fretNumber
  );
  const stringLabel = Array.from(els.fretboard.querySelectorAll(".string-label")).find(
    (element) => element.dataset.stringLabel === stringName
  );

  if (!cell) {
    els.fretboardStatus.textContent = "Out of range";
    return;
  }

  cell.classList.add("active");
  cell.querySelector("span").textContent = note || cell.dataset.note || "--";
  if (stringLabel) {
    stringLabel.classList.add("active");
  }
  els.fretboardStatus.textContent = `${stringName} fret ${fretNumber}`;
}

function clearFretboardHighlight() {
  els.fretboard
    .querySelectorAll(".fret-cell.active, .string-label.active")
    .forEach((element) => element.classList.remove("active"));
}

function setGuessHighlight(which) {
  els.singleGuessCard.dataset.active = which === "single" ? "true" : "false";
  els.chordGuessCard.dataset.active = which === "chord" ? "true" : "false";
}

function renderSingleCalibrationTable() {
  els.singleCalibrationCount.textContent =
    `${state.singleNotes.length} ${state.singleNotes.length === 1 ? "row" : "rows"}`;

  if (!state.singleNotes.length) {
    els.singleCalibrationRows.innerHTML = `
      <tr>
        <td colspan="6">No single-note calibration data saved yet.</td>
      </tr>
    `;
    return;
  }

  els.singleCalibrationRows.innerHTML = state.singleNotes
    .map((entry) => {
      const saved = entry.timestamp ? new Date(entry.timestamp).toLocaleString() : "--";
      return `
        <tr>
          <td>${escapeHtml(entry.string)}</td>
          <td>${escapeHtml(entry.note)}</td>
          <td>${entry.fret}</td>
          <td>${formatFrequency(entry.frequency_hz)}</td>
          <td>${entry.std_dev === null || entry.std_dev === undefined ? "--" : Number(entry.std_dev).toFixed(3)}</td>
          <td>${escapeHtml(saved)}</td>
        </tr>
      `;
    })
    .join("");
}

function renderChordCalibrationTable() {
  els.chordCalibrationCount.textContent =
    `${state.chords.length} ${state.chords.length === 1 ? "row" : "rows"}`;

  if (!state.chords.length) {
    els.chordCalibrationRows.innerHTML = `
      <tr>
        <td colspan="6">No chord calibration data saved yet.</td>
      </tr>
    `;
    return;
  }

  els.chordCalibrationRows.innerHTML = state.chords
    .map((entry) => {
      const saved = entry.timestamp ? new Date(entry.timestamp).toLocaleString() : "--";
      const fingerprint = entry.fingerprint || {};
      return `
        <tr>
          <td>${escapeHtml(entry.chord_name)}</td>
          <td>${escapeHtml(formatList(entry.expected_notes))}</td>
          <td>${escapeHtml(formatList(fingerprint.pitch_classes))}</td>
          <td>${formatPercentFromUnit(fingerprint.confidence_baseline)}</td>
          <td>${escapeHtml(saved)}</td>
          <td>
            <button class="tiny-button danger" type="button" data-delete-chord="${escapeHtml(entry.chord_name)}">
              Delete
            </button>
          </td>
        </tr>
      `;
    })
    .join("");
}

function resetDetectionDisplay(
  message = "Start detection and play one clean note or strum one clear chord.",
  options = {}
) {
  if (options.clearHeldResult) {
    state.lastDetectionResult = null;
  }

  els.detectionCard.dataset.state = "idle";
  els.detectedType.textContent = "Type: Unknown";
  els.detectedTitle.textContent = "--";
  els.detectionMessage.textContent = message;
  resetNoteVisuals();
  setGuessHighlight(null);
  renderSingleResult({
    status: "idle",
    message: "No single note yet",
    confidence: 0,
  });
  renderChordResult({
    status: "idle",
    message: "No chord yet",
    confidence: 0,
  });
}

function setSingleCalibrationRunning(isRunning) {
  state.singleCalibrationRunning = isRunning;
  els.startSingleCalibration.disabled = isRunning;
  els.resetSingleCalibration.disabled = isRunning;
  els.stringSelect.disabled = isRunning;
  els.fretSelect.disabled = isRunning;
  els.startSingleCalibration.textContent = isRunning
    ? "Listening..."
    : "Start Single Calibration";
}

function setChordCalibrationRunning(isRunning) {
  state.chordCalibrationRunning = isRunning;
  els.startChordCalibration.disabled = isRunning;
  els.resetChordCalibration.disabled = isRunning;
  els.chordSelect.disabled = isRunning;
  els.customChordName.disabled = isRunning;
  els.expectedNotesInput.disabled = isRunning;
  els.startChordCalibration.textContent = isRunning
    ? "Listening..."
    : "Start Chord Calibration";
}

function setDetectionRunning(isRunning) {
  state.detectionRunning = isRunning;
  els.startDetection.disabled = isRunning;
  els.stopDetection.disabled = !isRunning;
  els.startDetection.textContent = isRunning ? "Detection Running" : "Start Detection";
}

function setConnection(isConnected, label) {
  els.connectionBadge.textContent = label;
  els.connectionBadge.classList.toggle("badge-online", isConnected);
  els.connectionBadge.classList.toggle("badge-offline", !isConnected);
  els.connectionBadge.classList.remove("badge-waiting");
}

function setCalibrationReadout(frequency) {
  els.calibrationFrequency.textContent = formatFrequency(frequency);
  updateFrequencyMeter(els.calibrationMeter, frequency);
}

function updateFrequencyMeter(element, frequency) {
  if (!frequency) {
    element.style.width = "0%";
    return;
  }

  const minHz = 70;
  const maxHz = 700;
  const clamped = Math.max(minHz, Math.min(maxHz, Number(frequency)));
  const percent = ((clamped - minHz) / (maxHz - minHz)) * 100;
  element.style.width = `${percent.toFixed(1)}%`;
}

function showStatus(message, level = "info") {
  els.statusBar.textContent = message;
  els.statusBar.className = `status-bar status-${level}`;
}

function getSelectedChordName() {
  return els.customChordName.value.trim() || els.chordSelect.value.trim();
}

function parseExpectedNotesInput() {
  const manual = els.expectedNotesInput.value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);

  if (manual.length) {
    return manual;
  }

  const selected = findCommonChord(getSelectedChordName());
  return selected ? selected.expected_notes : [];
}

function findCommonChord(name) {
  return state.commonChords.find((item) => item.name === name);
}

function formatFrequency(value) {
  if (!value) {
    return "-- Hz";
  }
  return `${Number(value).toFixed(2)} Hz`;
}

function formatList(values) {
  if (!Array.isArray(values) || !values.length) {
    return "--";
  }
  return values.join(", ");
}

function formatPercentFromUnit(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "--";
  }
  return `${Math.round(numeric * 100)}%`;
}

function formatSmallNumber(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "--";
  }
  return numeric.toFixed(5);
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.message || `Request failed with status ${response.status}`);
  }
  return data;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
