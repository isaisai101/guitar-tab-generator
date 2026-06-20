const API = "http://localhost:5000/api";

// ── State ──────────────────────────────────────────────────────────────────
let pollTimer = null;
let currentJobId = null;

// ── DOM refs ───────────────────────────────────────────────────────────────
const inputSection    = document.getElementById("inputSection");
const progressSection = document.getElementById("progressSection");
const resultSection   = document.getElementById("resultSection");
const errorSection    = document.getElementById("errorSection");

const progressMsg  = document.getElementById("progressMsg");
const progressFill = document.getElementById("progressFill");
const progressPct  = document.getElementById("progressPct");

const tuningBadge  = document.getElementById("tuningBadge");
const tabPre       = document.getElementById("tabPre");
const visualTab    = document.getElementById("visualTab");

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

  tuningBadge.textContent = "Tuning: " + (result.tuning || "Unknown");
  tabPre.textContent      = result.tab_text || "(no tab generated)";

  renderVisualTab(result.tab || []);

  document.getElementById("downloadBtn").onclick = () => {
    window.location.href = `${API}/download/${currentJobId}`;
  };
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
    currentJobId = null;
    document.getElementById("fileInput").value = "";
    document.getElementById("urlInput").value  = "";
    hide([progressSection, resultSection, errorSection]);
    show(inputSection);
  });
});

// ── Visual tab renderer ────────────────────────────────────────────────────
function renderVisualTab(columns) {
  visualTab.innerHTML = "";
  if (!columns.length) return;

  const STRING_LABELS = ["e", "B", "G", "D", "A", "E"];
  const CHUNK = 20;

  const title = document.createElement("h3");
  title.textContent = "Visual Tab";
  visualTab.appendChild(title);

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

      chunk.forEach(col => {
        const cell = document.createElement("span");
        cell.className = "vt-cell";
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
