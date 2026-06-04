const state = {
  strings: [],
  frets: [],
  entries: [],
  currentMode: "calibration",
  calibrationRunning: false,
  detectionRunning: false,
  lastDetectionResult: null,
};

const els = {};

document.addEventListener("DOMContentLoaded", async () => {
  cacheElements();
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
  els.calibrationForm = document.querySelector("#calibrationForm");
  els.stringSelect = document.querySelector("#stringSelect");
  els.fretSelect = document.querySelector("#fretSelect");
  els.startCalibration = document.querySelector("#startCalibration");
  els.resetCalibration = document.querySelector("#resetCalibration");
  els.calibrationFrequency = document.querySelector("#calibrationFrequency");
  els.calibrationMeter = document.querySelector("#calibrationMeter");
  els.calibrationHint = document.querySelector("#calibrationHint");
  els.calibrationRows = document.querySelector("#calibrationRows");
  els.calibrationCount = document.querySelector("#calibrationCount");
  els.startDetection = document.querySelector("#startDetection");
  els.stopDetection = document.querySelector("#stopDetection");
  els.detectionCard = document.querySelector("#detectionCard");
  els.detectedTitle = document.querySelector("#detectedTitle");
  els.detectedNote = document.querySelector("#detectedNote");
  els.detectionMessage = document.querySelector("#detectionMessage");
  els.detectedFrequency = document.querySelector("#detectedFrequency");
  els.detectionMeter = document.querySelector("#detectionMeter");
  els.confidenceValue = document.querySelector("#confidenceValue");
  els.confidenceMeter = document.querySelector("#confidenceMeter");
  els.centsError = document.querySelector("#centsError");
  els.calibratedTarget = document.querySelector("#calibratedTarget");
}

function bindEvents() {
  els.calibrationTab.addEventListener("click", () => switchMode("calibration"));
  els.detectionTab.addEventListener("click", () => switchMode("detection"));
  els.calibrationForm.addEventListener("submit", startCalibration);
  els.resetCalibration.addEventListener("click", resetCalibration);
  els.startDetection.addEventListener("click", startDetection);
  els.stopDetection.addEventListener("click", stopDetection);
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
      payload.mode === "calibration" &&
      /saved|did not capture|unable/i.test(payload.message || "")
    ) {
      setCalibrationRunning(false);
    }
  });

  socket.on("pitch_update", handlePitchUpdate);

  socket.on("detection_result", renderDetectionResult);

  socket.on("calibration_saved", (payload) => {
    if (Array.isArray(payload.entries)) {
      state.entries = payload.entries;
      renderCalibrationTable();
    }
    setCalibrationRunning(false);
  });
}

async function loadCalibration() {
  try {
    const data = await requestJson("/api/calibration");
    state.strings = data.strings || [];
    state.frets = data.frets || [];
    state.entries = data.entries || [];
    populateControls();
    renderCalibrationTable();
  } catch (error) {
    showStatus(error.message, "error");
  }
}

function populateControls() {
  els.stringSelect.innerHTML = state.strings
    .map((item) => `<option value="${escapeHtml(item.label)}">${escapeHtml(item.label)} (${escapeHtml(item.note)})</option>`)
    .join("");

  els.fretSelect.innerHTML = state.frets
    .map((fret) => `<option value="${fret}">${fret}</option>`)
    .join("");
}

async function startCalibration(event) {
  event.preventDefault();

  const selectedString = state.strings.find((item) => item.label === els.stringSelect.value);
  if (!selectedString) {
    showStatus("Choose a valid guitar string.", "warning");
    return;
  }

  setCalibrationRunning(true);
  setCalibrationReadout(null);
  els.calibrationHint.textContent = "Listening now. Play one clean note and let it ring.";

  try {
    await requestJson("/api/calibration/start", {
      method: "POST",
      body: JSON.stringify({
        string: selectedString.label,
        note: selectedString.note,
        fret: Number(els.fretSelect.value),
      }),
    });
  } catch (error) {
    setCalibrationRunning(false);
    if (await recoverFromBusyAudio(error)) {
      return;
    }
    showStatus(error.message, "error");
  }
}

async function resetCalibration() {
  if (!window.confirm("Delete all saved calibration data?")) {
    return;
  }

  try {
    const data = await requestJson("/api/calibration/reset", { method: "POST" });
    state.entries = data.entries || [];
    renderCalibrationTable();
    resetDetectionDisplay("Calibration data cleared. Add calibration rows before detection.", {
      clearHeldResult: true,
    });
  } catch (error) {
    showStatus(error.message, "error");
  }
}

async function startDetection() {
  setDetectionRunning(true);
  resetDetectionDisplay("Listening...", { clearHeldResult: true });

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
    setCalibrationRunning(false);
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
  const frequency = payload.smoothed_frequency_hz || payload.frequency_hz;

  if (payload.mode === "calibration") {
    setCalibrationReadout(frequency);
    const remaining = payload.seconds_remaining ?? 0;
    const stableSamples = payload.stable_samples ?? 0;
    els.calibrationHint.textContent = payload.stable
      ? `Stable tone captured. ${remaining.toFixed(1)}s left, ${stableSamples} stable samples.`
      : `${payload.message || "Listening..."} ${remaining.toFixed(1)}s left.`;
    return;
  }

  if (payload.mode === "detection" && frequency) {
    els.detectedFrequency.textContent = formatFrequency(frequency);
    updateFrequencyMeter(els.detectionMeter, frequency);
  }
}

function renderDetectionResult(result) {
  if (result.status === "no_pitch") {
    renderHeldDetectionResult();
    return;
  }

  els.detectionCard.dataset.state = result.status || "idle";

  if (result.status === "no_calibration") {
    state.lastDetectionResult = null;
    els.detectedTitle.textContent = "--";
    els.detectedNote.textContent = "Calibration needed";
    els.detectionMessage.textContent = result.message;
    els.detectedFrequency.textContent = formatFrequency(result.detected_frequency_hz);
    els.centsError.textContent = "--";
    els.calibratedTarget.textContent = "--";
    setConfidence(0);
    return;
  }

  state.lastDetectionResult = result;
  els.detectedTitle.textContent = `${result.string} / Fret ${result.fret}`;
  els.detectedNote.textContent = `${result.note} reference`;
  els.detectionMessage.textContent = result.message;
  els.detectedFrequency.textContent = formatFrequency(result.detected_frequency_hz);
  els.centsError.textContent =
    result.cents_error === null || result.cents_error === undefined
      ? "--"
      : `${result.cents_error > 0 ? "+" : ""}${Number(result.cents_error).toFixed(2)} cents`;
  els.calibratedTarget.textContent = result.calibrated_frequency_hz
    ? formatFrequency(result.calibrated_frequency_hz)
    : "--";
  setConfidence(result.confidence || 0);
  updateFrequencyMeter(els.detectionMeter, result.detected_frequency_hz);
}

function renderHeldDetectionResult() {
  if (!state.lastDetectionResult) {
    els.detectionCard.dataset.state = "no_pitch";
    els.detectedTitle.textContent = "--";
    els.detectedNote.textContent = "No stable pitch";
    els.detectionMessage.textContent = "No clear note detected";
    els.detectedFrequency.textContent = "-- Hz";
    els.centsError.textContent = "--";
    els.calibratedTarget.textContent = "--";
    setConfidence(0);
    updateFrequencyMeter(els.detectionMeter, null);
    return;
  }

  const held = state.lastDetectionResult;
  els.detectionCard.dataset.state = "holding";
  els.detectedTitle.textContent = `${held.string} / Fret ${held.fret}`;
  els.detectedNote.textContent = `${held.note} reference`;
  els.detectionMessage.textContent = "No new clear note detected. Holding the last string and fret.";
  els.detectedFrequency.textContent = held.detected_frequency_hz
    ? formatFrequency(held.detected_frequency_hz)
    : "-- Hz";
  els.centsError.textContent =
    held.cents_error === null || held.cents_error === undefined
      ? "--"
      : `${held.cents_error > 0 ? "+" : ""}${Number(held.cents_error).toFixed(2)} cents`;
  els.calibratedTarget.textContent = held.calibrated_frequency_hz
    ? formatFrequency(held.calibrated_frequency_hz)
    : "--";
  setConfidence(held.confidence || 0);
}

function renderCalibrationTable() {
  els.calibrationCount.textContent = `${state.entries.length} ${state.entries.length === 1 ? "row" : "rows"}`;

  if (!state.entries.length) {
    els.calibrationRows.innerHTML = `
      <tr>
        <td colspan="5">No calibration data saved yet.</td>
      </tr>
    `;
    return;
  }

  els.calibrationRows.innerHTML = state.entries
    .map((entry) => {
      const saved = entry.timestamp ? new Date(entry.timestamp).toLocaleString() : "--";
      return `
        <tr>
          <td>${escapeHtml(entry.string)}</td>
          <td>${escapeHtml(entry.note)}</td>
          <td>${entry.fret}</td>
          <td>${formatFrequency(entry.frequency_hz)}</td>
          <td>${escapeHtml(saved)}</td>
        </tr>
      `;
    })
    .join("");
}

function resetDetectionDisplay(
  message = "Start detection and play one note at a time.",
  options = {}
) {
  if (options.clearHeldResult) {
    state.lastDetectionResult = null;
  }

  els.detectionCard.dataset.state = "idle";
  els.detectedTitle.textContent = "--";
  els.detectedNote.textContent = "Waiting for detection";
  els.detectionMessage.textContent = message;
  els.detectedFrequency.textContent = "-- Hz";
  els.centsError.textContent = "--";
  els.calibratedTarget.textContent = "--";
  setConfidence(0);
  updateFrequencyMeter(els.detectionMeter, null);
}

function setCalibrationRunning(isRunning) {
  state.calibrationRunning = isRunning;
  els.startCalibration.disabled = isRunning;
  els.resetCalibration.disabled = isRunning;
  els.stringSelect.disabled = isRunning;
  els.fretSelect.disabled = isRunning;
  els.startCalibration.textContent = isRunning ? "Listening..." : "Start Calibration";
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

function setConfidence(confidence) {
  const value = Math.max(0, Math.min(100, Number(confidence) || 0));
  els.confidenceValue.textContent = `${value}%`;
  els.confidenceMeter.style.width = `${value}%`;
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

function formatFrequency(value) {
  if (!value) {
    return "-- Hz";
  }
  return `${Number(value).toFixed(2)} Hz`;
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
