const API = "http://localhost:5000/api";

// ── State ──────────────────────────────────────────────────────────────────
let pollTimer      = null;
let currentJobId   = null;
let tabColumns     = [];
let tabTimings     = [];
let hasOverdrive   = false;

// ── DOM refs ───────────────────────────────────────────────────────────────
const inputSection    = document.getElementById("inputSection");
const progressSection = document.getElementById("progressSection");
const resultSection   = document.getElementById("resultSection");
const errorSection    = document.getElementById("errorSection");

const progressMsg  = document.getElementById("progressMsg");
const progressFill = document.getElementById("progressFill");
const progressPct  = document.getElementById("progressPct");

const tuningBadge    = document.getElementById("tuningBadge");
const overdriveBadge = document.getElementById("overdriveBadge");
const visualTab      = document.getElementById("visualTab");
const playBtn        = document.getElementById("playBtn");
const stopBtn        = document.getElementById("stopBtn");
const playCursor     = document.getElementById("playCursor");

// ── Tab switcher ───────────────────────────────────────────────────────────
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-content").forEach(c => c.classList.add("hidden"));
    btn.classList.add("active");
    document.getElementById("tab-" + btn.dataset.tab).classList.remove("hidden");
  });
});

// ── File upload ────────────────────────────────────────────────────────────
const dropzone  = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const browseBtn = document.getElementById("browseBtn");

browseBtn.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("click", e => { if (e.target !== browseBtn) fileInput.click(); });

dropzone.addEventListener("dragover", e => { e.preventDefault(); dropzone.classList.add("drag-over"); });
dropzone.addEventListener("dragleave",  () => dropzone.classList.remove("drag-over"));
dropzone.addEventListener("drop", e => {
  e.preventDefault();
  dropzone.classList.remove("drag-over");
  const file = e.dataTransfer.files[0];
  if (file) uploadFile(file);
});

fileInput.addEventListener("change", () => {
  if (fileInput.files[0]) uploadFile(fileInput.files[0]);
});

async function uploadFile(file) {
  const form = new FormData();
  form.append("file", file);
  showProgress("Uploading file…", 2);
  try {
    const res  = await fetch(`${API}/upload`, { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Upload failed");
    startPolling(data.job_id);
  } catch (err) {
    showError(err.message);
  }
}

// ── YouTube URL ────────────────────────────────────────────────────────────
document.getElementById("urlSubmitBtn").addEventListener("click", submitUrl);
document.getElementById("urlInput").addEventListener("keydown", e => {
  if (e.key === "Enter") submitUrl();
});

async function submitUrl() {
  const url = document.getElementById("urlInput").value.trim();
  if (!url) return;
  showProgress("Queuing YouTube download…", 2);
  try {
    const res  = await fetch(`${API}/upload`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to start");
    startPolling(data.job_id);
  } catch (err) {
    showError(err.message);
  }
}

// ── Polling ────────────────────────────────────────────────────────────────
function startPolling(jobId) {
  currentJobId = jobId;
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => pollStatus(jobId), 1500);
}

async function pollStatus(jobId) {
  try {
    const res  = await fetch(`${API}/status/${jobId}`);
    const data = await res.json();
    if (!res.ok) { showError(data.error || "Status check failed"); clearInterval(pollTimer); return; }

    updateProgress(data.message, data.progress);

    if (data.status === "done") {
      clearInterval(pollTimer);
      showResult(data.result);
    } else if (data.status === "error") {
      clearInterval(pollTimer);
      showError(data.message);
    }
  } catch {
    // network hiccup — keep polling
  }
}

// ── UI helpers ─────────────────────────────────────────────────────────────
function showProgress(msg, pct) {
  hide([inputSection, resultSection, errorSection]);
  show(progressSection);
  updateProgress(msg, pct);
}

function updateProgress(msg, pct) {
  progressMsg.textContent  = msg;
  progressFill.style.width = pct + "%";
  progressPct.textContent  = pct + "%";
}

function showResult(result) {
  hide([inputSection, progressSection, errorSection]);
  show(resultSection);

  // Tuning badge
  tuningBadge.textContent = "Tuning: " + (result.tuning || "Unknown");

  // Overdrive badge
  const od = result.overdrive || {};
  overdriveBadge.classList.remove("hidden", "clean", "light", "medium", "heavy");
  const lvl = (od.level || "Clean").toLowerCase();
  const icons = { clean: "🟢", light: "🟡", medium: "🟠", heavy: "🔴" };
  overdriveBadge.textContent = (icons[lvl] || "") + " Overdrive: " + (od.level || "Clean");
  overdriveBadge.classList.add(lvl);
  hasOverdrive = od.detected || false;

  // Store tab data for playback
  tabColumns = result.tab   || [];
  tabTimings = result.timings || [];

  renderVisualTab(tabColumns);

  document.getElementById("downloadBtn").onclick = () => {
    window.location.href = `${API}/download/${currentJobId}`;
  };

  // Reset playback UI
  stopPlayback();
  playBtn.classList.remove("hidden");
  stopBtn.classList.add("hidden");
  playCursor.style.left = "0%";
}

function showError(msg) {
  hide([inputSection, progressSection, resultSection]);
  show(errorSection);
  document.getElementById("errorMsg").textContent = msg;
}

function show(el) { el.classList.remove("hidden"); }
function hide(els) { els.forEach(e => e.classList.add("hidden")); }

// ── Reset ──────────────────────────────────────────────────────────────────
["resetBtn", "errorResetBtn"].forEach(id => {
  document.getElementById(id).addEventListener("click", () => {
    if (pollTimer) clearInterval(pollTimer);
    stopPlayback();
    currentJobId = null;
    tabColumns = [];
    tabTimings = [];
    document.getElementById("fileInput").value = "";
    document.getElementById("urlInput").value  = "";
    hide([progressSection, resultSection, errorSection]);
    show(inputSection);
  });
});

// ── Visual tab renderer ────────────────────────────────────────────────────
const STRING_LABELS = ["e", "B", "G", "D", "A", "E"];

function renderVisualTab(columns) {
  visualTab.innerHTML = "";
  if (!columns.length) return;

  const CHUNK = 20;

  for (let start = 0; start < columns.length; start += CHUNK) {
    const chunk = columns.slice(start, start + CHUNK);
    const block = document.createElement("div");
    block.className = "vt-lines";
    block.style.marginBottom = "1.2rem";

    STRING_LABELS.forEach(label => {
      const row = document.createElement("div");
      row.className = "vt-row";

      const lbl = document.createElement("span");
      lbl.className = "vt-label";
      lbl.textContent = label;
      row.appendChild(lbl);

      const line = document.createElement("div");
      line.className = "vt-line";

      chunk.forEach((col, ci) => {
        const cell = document.createElement("span");
        cell.className = "vt-cell";
        cell.dataset.colIdx = start + ci;   // global index for highlighting
        const val = col[label];
        if (val && val !== "-") {
          cell.textContent = val;
          cell.classList.add("has-note");
        } else {
          cell.textContent = "—";
        }
        line.appendChild(cell);
      });

      row.appendChild(line);
      block.appendChild(row);
    });

    visualTab.appendChild(block);
  }
}

// ── Web Audio Playback ─────────────────────────────────────────────────────
const OPEN_MIDI = { e: 64, B: 59, G: 55, D: 50, A: 45, E: 40 };

let audioCtx      = null;
let playNodes     = [];
let playRaf       = null;
let playStart     = 0;   // audioCtx.currentTime when playback began
let playDuration  = 0;   // total normalised seconds
let normTimings   = [];  // timings shifted so first note = 0

function midiToFreq(midi) {
  return 440 * Math.pow(2, (midi - 69) / 12);
}

// Soft-clip distortion curve (heavier at higher amount)
function makeDistortionCurve(amount) {
  const n = 512, curve = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const x = (i * 2) / n - 1;
    curve[i] = ((Math.PI + amount) * x) / (Math.PI + amount * Math.abs(x));
  }
  return curve;
}

// Plucked-string PeriodicWave (approximates decaying guitar harmonics)
function getGuitarWave(ctx) {
  const harmonics = [0, 1, 0.5, 0.25, 0.12, 0.06, 0.03, 0.015];
  const real = new Float32Array(harmonics.length);
  const imag = new Float32Array(harmonics.length);
  real.set(harmonics);
  return ctx.createPeriodicWave(real, imag, { disableNormalization: false });
}

playBtn.addEventListener("click", startPlayback);
stopBtn.addEventListener("click", stopPlayback);

function startPlayback() {
  if (!tabColumns.length) return;
  stopPlayback();

  audioCtx = audioCtx || new AudioContext();

  // ── Timing normalisation ───────────────────────────────────────────────
  // Shift all timings so the first detected note plays immediately at t=0.
  // Without this, if the guitar enters at 5 s in the track, playback begins
  // with 5 s of silence and then rushes — making speed feel wrong.
  const offset = tabTimings.length > 0 ? tabTimings[0] : 0;
  normTimings = tabTimings.map(t => t - offset);

  const lastNorm = normTimings.length > 0 ? normTimings[normTimings.length - 1] : tabColumns.length * 0.12;
  playDuration = lastNorm + 1.2;

  const now = audioCtx.currentTime + 0.05;
  playStart = now;
  playNodes = [];

  // ── Shared compressor ─────────────────────────────────────────────────
  const comp = audioCtx.createDynamicsCompressor();
  comp.threshold.setValueAtTime(-20, now);
  comp.knee.setValueAtTime(10, now);
  comp.ratio.setValueAtTime(6, now);
  comp.attack.setValueAtTime(0.003, now);
  comp.release.setValueAtTime(0.12, now);
  comp.connect(audioCtx.destination);

  const guitarWave = hasOverdrive ? null : getGuitarWave(audioCtx);

  tabColumns.forEach((col, i) => {
    const colT = now + normTimings[i];

    // Duration = gap to next onset (capped at 1.5 s), used for note decay
    const nextT = i + 1 < normTimings.length ? normTimings[i + 1] : normTimings[i] + 0.5;
    const noteDur = Math.min(Math.max(nextT - normTimings[i], 0.08), 1.5);

    Object.entries(col).forEach(([str, fret]) => {
      if (fret === "-" || !OPEN_MIDI[str]) return;
      const fretNum = parseInt(fret, 10);
      if (isNaN(fretNum)) return;

      const midi = OPEN_MIDI[str] + fretNum;
      const freq = midiToFreq(midi);
      const decayEnd = colT + noteDur + 0.15;

      const osc    = audioCtx.createOscillator();
      const envGain = audioCtx.createGain();
      const filter  = audioCtx.createBiquadFilter();

      osc.frequency.value = freq;

      if (hasOverdrive) {
        // Sawtooth → heavy clip → narrow bandpass (amp cabinet sim)
        osc.type = "sawtooth";
        const dist = audioCtx.createWaveShaper();
        dist.curve = makeDistortionCurve(400);
        dist.oversample = "4x";

        filter.type = "bandpass";
        filter.frequency.value = 1000;
        filter.Q.value = 0.6;

        envGain.gain.setValueAtTime(0,    colT);
        envGain.gain.linearRampToValueAtTime(0.20, colT + 0.004);
        envGain.gain.setValueAtTime(       0.20, colT + 0.004);
        envGain.gain.exponentialRampToValueAtTime(0.001, decayEnd);

        osc.connect(dist);
        dist.connect(filter);
      } else {
        // Guitar-harmonic PeriodicWave → lowpass (body resonance)
        osc.setPeriodicWave(guitarWave);
        filter.type = "lowpass";
        filter.frequency.value = 2800;
        filter.Q.value = 0.5;

        envGain.gain.setValueAtTime(0,    colT);
        envGain.gain.linearRampToValueAtTime(0.22, colT + 0.006);  // pluck attack
        envGain.gain.exponentialRampToValueAtTime(0.06, colT + 0.12);
        envGain.gain.exponentialRampToValueAtTime(0.001, decayEnd);

        osc.connect(filter);
      }

      filter.connect(envGain);
      envGain.connect(comp);

      osc.start(colT);
      osc.stop(decayEnd + 0.05);
      playNodes.push(osc);
    });
  });

  playBtn.classList.add("hidden");
  stopBtn.classList.remove("hidden");
  animatePlayback();
}

function animatePlayback() {
  playRaf = requestAnimationFrame(function tick() {
    if (!audioCtx) return;
    const elapsed = audioCtx.currentTime - playStart;
    if (elapsed >= playDuration) { stopPlayback(); return; }

    // Cursor uses normalised time
    playCursor.style.left = Math.min(elapsed / playDuration * 100, 100) + "%";

    // Find the last column whose normalised timestamp ≤ elapsed
    let activeIdx = 0;
    for (let i = 0; i < normTimings.length; i++) {
      if (normTimings[i] <= elapsed) activeIdx = i;
      else break;
    }
    highlightCol(activeIdx);

    playRaf = requestAnimationFrame(tick);
  });
}

let lastHighlightedIdx = -1;
function highlightCol(idx) {
  if (idx === lastHighlightedIdx) return;

  if (lastHighlightedIdx >= 0) {
    document.querySelectorAll(`.vt-cell[data-col-idx="${lastHighlightedIdx}"]`).forEach(el => {
      el.style.background = "";
      if (el.classList.contains("has-note")) el.style.color = "";
    });
  }

  const cells = document.querySelectorAll(`.vt-cell[data-col-idx="${idx}"]`);
  cells.forEach(el => {
    el.style.background = "rgba(124,58,237,0.22)";
    if (el.classList.contains("has-note")) el.style.color = "#fff";
  });

  if (cells[0]) cells[0].scrollIntoView({ behavior: "smooth", block: "nearest", inline: "center" });

  lastHighlightedIdx = idx;
}

function stopPlayback() {
  if (playRaf) { cancelAnimationFrame(playRaf); playRaf = null; }
  playNodes.forEach(n => { try { n.stop(); } catch {} });
  playNodes = [];
  lastHighlightedIdx = -1;
  document.querySelectorAll(".vt-cell").forEach(el => {
    el.style.background = "";
    el.style.color = "";
  });
  playCursor.style.left = "0%";
  playBtn.classList.remove("hidden");
  stopBtn.classList.add("hidden");
}
