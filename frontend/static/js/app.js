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

// Soft-clip distortion curve. `amount` ~ drive: keep MODERATE (20-40).
// The old value of 400 aliased a sawtooth into a harsh crack and pushed the
// signal so hot the compressor pumped everything down to silence.
function makeDistortionCurve(amount) {
  const n = 1024, curve = new Float32Array(n);
  const k = amount;
  for (let i = 0; i < n; i++) {
    const x = (i * 2) / n - 1;
    // tanh-style soft clip — smooth, no aliasing crack
    curve[i] = Math.tanh(k * x) / Math.tanh(k);
  }
  return curve;
}

// Gentle brick-wall limiter curve for the master bus — stops chord transients
// from clicking without audibly distorting normal levels.
function makeLimiterCurve() {
  const n = 1024, curve = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const x = (i * 2) / n - 1;
    curve[i] = Math.tanh(1.6 * x);   // ~unity near 0, soft ceiling near ±1
  }
  return curve;
}

// Plucked-string PeriodicWave. Slightly richer odd-harmonic content gives the
// woody guitar character (vs the rounder, more "piano/organ" prior spectrum).
function getGuitarWave(ctx) {
  const harmonics = [0, 1.0, 0.55, 0.40, 0.20, 0.14, 0.09, 0.05, 0.03, 0.02];
  const real = new Float32Array(harmonics.length);
  const imag = new Float32Array(harmonics.length);
  imag.set(harmonics);   // sine-phase harmonics
  return ctx.createPeriodicWave(real, imag, { disableNormalization: false });
}

// Short filtered-noise "pick" transient — the single strongest cue that a note
// was plucked (string/pick contact) rather than struck like a piano key.
function playPickNoise(ctx, dest, freq, startT, level) {
  const N = Math.max(1, Math.floor(ctx.sampleRate * 0.012));
  const buf = ctx.createBuffer(1, N, ctx.sampleRate);
  const d = buf.getChannelData(0);
  for (let i = 0; i < N; i++) d[i] = (Math.random() * 2 - 1) * (1 - i / N);
  const src = ctx.createBufferSource();
  src.buffer = buf;
  const bp = ctx.createBiquadFilter();
  bp.type = "bandpass";
  bp.frequency.value = Math.min(freq * 3, 3500);
  bp.Q.value = 0.7;
  const g = ctx.createGain();
  g.gain.value = level;
  src.connect(bp); bp.connect(g); g.connect(dest);
  src.start(startT); src.stop(startT + 0.02);
  playNodes.push(src);
}

playBtn.addEventListener("click", startPlayback);
stopBtn.addEventListener("click", stopPlayback);

function startPlayback() {
  if (!tabColumns.length) return;
  stopPlayback();

  audioCtx = audioCtx || new AudioContext();
  if (audioCtx.state === "suspended") audioCtx.resume();

  // ── Timing normalisation ───────────────────────────────────────────────
  const offset = tabTimings.length > 0 ? tabTimings[0] : 0;
  normTimings = tabTimings.map(t => t - offset);

  const lastNorm = normTimings.length > 0 ? normTimings[normTimings.length - 1] : tabColumns.length * 0.12;
  playDuration = lastNorm + 1.5;

  const now = audioCtx.currentTime + 0.05;
  playStart = now;
  playNodes = [];

  // ── Master bus: gentle compressor → soft limiter → output ───────────────
  // Conservative settings so it tames peaks without ducking to silence.
  const comp = audioCtx.createDynamicsCompressor();
  comp.threshold.setValueAtTime(-12, now);
  comp.knee.setValueAtTime(18, now);
  comp.ratio.setValueAtTime(4, now);
  comp.attack.setValueAtTime(0.004, now);
  comp.release.setValueAtTime(0.18, now);

  const master = audioCtx.createGain();
  master.gain.value = hasOverdrive ? 0.55 : 0.75;

  const limiter = audioCtx.createWaveShaper();
  limiter.curve = makeLimiterCurve();

  master.connect(comp);
  comp.connect(limiter);
  limiter.connect(audioCtx.destination);

  const guitarWave = hasOverdrive ? null : getGuitarWave(audioCtx);

  tabColumns.forEach((col, i) => {
    const colT = now + normTimings[i];
    const nextT = i + 1 < normTimings.length ? normTimings[i + 1] : normTimings[i] + 0.6;
    const noteDur = Math.min(Math.max(nextT - normTimings[i], 0.10), 1.8);

    // Count notes in this column so a 6-note chord doesn't sum to clipping.
    const voices = Object.values(col).filter(f => f !== "-" && !isNaN(parseInt(f, 10))).length || 1;
    const voiceScale = 1 / Math.sqrt(voices);   // equal-power chord scaling

    Object.entries(col).forEach(([str, fret]) => {
      if (fret === "-" || !OPEN_MIDI[str]) return;
      const fretNum = parseInt(fret, 10);
      if (isNaN(fretNum)) return;

      const midi = OPEN_MIDI[str] + fretNum;
      const freq = midiToFreq(midi);
      const decayEnd = colT + noteDur + 0.25;

      const osc     = audioCtx.createOscillator();
      const envGain = audioCtx.createGain();
      const filter  = audioCtx.createBiquadFilter();
      filter.type = "lowpass";
      filter.Q.value = 0.7;

      osc.frequency.value = freq;

      if (hasOverdrive) {
        // Sawtooth → MODERATE soft-clip → lowpass cabinet sim.
        // Lowpass (not bandpass@1kHz) lets the low power-chord fundamentals
        // through, so "Be Quiet and Drive"-style low chords are audible.
        osc.type = "sawtooth";
        const dist = audioCtx.createWaveShaper();
        dist.curve = makeDistortionCurve(30);
        dist.oversample = "4x";

        // Brightness eases down over the note (palm-mute-ish decay)
        filter.frequency.setValueAtTime(3800, colT);
        filter.frequency.exponentialRampToValueAtTime(1500, decayEnd);

        const peak = 0.16 * voiceScale;
        envGain.gain.setValueAtTime(0.0001, colT);
        envGain.gain.linearRampToValueAtTime(peak, colT + 0.006);
        envGain.gain.exponentialRampToValueAtTime(peak * 0.5, colT + 0.30);
        envGain.gain.exponentialRampToValueAtTime(0.0008, decayEnd);

        osc.connect(dist);
        dist.connect(filter);
        playPickNoise(audioCtx, master, freq, colT, 0.05 * voiceScale);
      } else {
        // Guitar PeriodicWave → lowpass that SWEEPS DOWN = string damping.
        // The downward brightness sweep is what makes it read as a plucked
        // guitar instead of a sustained piano/organ tone.
        osc.setPeriodicWave(guitarWave);
        filter.frequency.setValueAtTime(6500, colT);
        filter.frequency.exponentialRampToValueAtTime(700, decayEnd);

        const peak = 0.30 * voiceScale;
        envGain.gain.setValueAtTime(0.0001, colT);
        envGain.gain.linearRampToValueAtTime(peak, colT + 0.005);   // pluck
        envGain.gain.exponentialRampToValueAtTime(peak * 0.35, colT + 0.18);
        envGain.gain.exponentialRampToValueAtTime(0.0006, decayEnd);

        osc.connect(filter);
        playPickNoise(audioCtx, master, freq, colT, 0.06 * voiceScale);
      }

      filter.connect(envGain);
      envGain.connect(master);

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
