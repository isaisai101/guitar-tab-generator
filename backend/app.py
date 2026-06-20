import os
import uuid
import json
import subprocess
import threading
import traceback
from pathlib import Path
from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS

# Patch torchaudio.save to use soundfile — torchcodec requires FFmpeg shared
# DLLs that are not reliably present on Windows. soundfile works everywhere.
def _patch_torchaudio():
    try:
        import torchaudio
        import soundfile as sf

        def _sf_save(uri, src, sample_rate, bits_per_sample=16, **kwargs):
            wav_np = src.numpy().T  # (samples, channels)
            subtype = "PCM_16" if bits_per_sample <= 16 else "PCM_24"
            sf.write(str(uri), wav_np, int(sample_rate), subtype=subtype)

        torchaudio.save = _sf_save
    except Exception:
        pass  # if torchaudio isn't installed, nothing to patch

_patch_torchaudio()

app = Flask(__name__, static_folder="../frontend", static_url_path="")
CORS(app)

BASE_DIR = Path(__file__).parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# In-memory job store  {job_id: {status, progress, message, result}}
jobs = {}
jobs_lock = threading.Lock()


def set_job(job_id, **kwargs):
    with jobs_lock:
        jobs.setdefault(job_id, {}).update(kwargs)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("../frontend", "index.html")


@app.route("/api/upload", methods=["POST"])
def upload():
    job_id = str(uuid.uuid4())
    set_job(job_id, status="queued", progress=0, message="Queued")

    if "file" in request.files:
        f = request.files["file"]
        ext = Path(f.filename).suffix.lower()
        if ext not in (".mp3", ".wav", ".flac", ".ogg", ".m4a"):
            return jsonify(error="Unsupported file type"), 400
        src = UPLOAD_DIR / f"{job_id}{ext}"
        f.save(src)
        thread = threading.Thread(target=process_audio, args=(job_id, src), daemon=True)
    elif request.json and request.json.get("url"):
        url = request.json["url"]
        thread = threading.Thread(target=process_youtube, args=(job_id, url), daemon=True)
    else:
        return jsonify(error="No file or URL provided"), 400

    thread.start()
    return jsonify(job_id=job_id)


@app.route("/api/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify(error="Unknown job"), 404
    return jsonify(job)


@app.route("/api/download/<job_id>")
def download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify(error="Not ready"), 404
    tab_path = OUTPUT_DIR / f"{job_id}_tab.txt"
    if not tab_path.exists():
        return jsonify(error="Tab file missing"), 404
    return send_file(tab_path, as_attachment=True, download_name="guitar_tab.txt")


# ── Processing pipeline ───────────────────────────────────────────────────────

def process_youtube(job_id, url):
    try:
        set_job(job_id, status="running", progress=5, message="Downloading from YouTube…")
        out_path = UPLOAD_DIR / f"{job_id}.%(ext)s"
        cmd = [
            "yt-dlp", "-x", "--audio-format", "wav",
            "--audio-quality", "0",
            "-o", str(out_path),
            url
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        # find the downloaded file
        matches = list(UPLOAD_DIR.glob(f"{job_id}.*"))
        if not matches:
            raise RuntimeError("yt-dlp produced no output file")
        src = matches[0]
        process_audio(job_id, src)
    except Exception as e:
        set_job(job_id, status="error", message=str(e))
        traceback.print_exc()


def process_audio(job_id, src: Path):
    try:
        # 1) Separate stems with Demucs
        set_job(job_id, status="running", progress=10, message="Separating guitar track with Demucs…")
        guitar_wav = run_demucs(job_id, src)

        # 2) Detect tuning
        set_job(job_id, status="running", progress=50, message="Detecting guitar tuning…")
        tuning_name, open_strings = detect_tuning(guitar_wav)

        # 3) Detect overdrive/distortion
        set_job(job_id, status="running", progress=58, message="Analysing amp tone…")
        overdrive_info = detect_overdrive(guitar_wav)

        # 4) Transcribe notes
        set_job(job_id, status="running", progress=65, message="Transcribing notes…")
        notes = run_basic_pitch(job_id, guitar_wav)

        # 5) Build tab
        set_job(job_id, status="running", progress=85, message="Building guitar tab…")
        tab_text, tab_data, timings = build_tab(notes, open_strings, tuning_name)

        # 6) Save tab
        tab_path = OUTPUT_DIR / f"{job_id}_tab.txt"
        tab_path.write_text(tab_text, encoding="utf-8")

        set_job(
            job_id,
            status="done",
            progress=100,
            message="Done",
            result={
                "tuning": tuning_name,
                "tab": tab_data,
                "timings": timings,
                "tab_text": tab_text,
                "overdrive": overdrive_info,
            }
        )
    except Exception as e:
        set_job(job_id, status="error", message=str(e))
        traceback.print_exc()


def run_demucs(job_id, src: Path) -> Path:
    import torch
    from demucs.separate import main as demucs_main

    device = "cuda" if torch.cuda.is_available() else "cpu"
    out_root = OUTPUT_DIR / job_id

    # Run in-process so the torchaudio.save patch above is in effect
    demucs_main([
        "-n", "htdemucs_6s",
        "--device", device,
        "-o", str(out_root),
        str(src),
    ])

    # htdemucs_6s produces: drums, bass, guitar, piano, vocals, other
    stem_root = out_root / "htdemucs_6s" / src.stem
    guitar_path = stem_root / "guitar.wav"
    if not guitar_path.exists():
        guitar_path = stem_root / "other.wav"
    if not guitar_path.exists():
        raise RuntimeError(f"Could not find guitar stem in {stem_root}")
    return guitar_path


def detect_overdrive(wav_path: Path) -> dict:
    import librosa
    import numpy as np

    y, sr = librosa.load(str(wav_path), mono=True, duration=30)
    if len(y) == 0:
        return {"detected": False, "level": "Clean", "score": 0.0}

    # 1. Spectral flatness: pure tone → 0, white noise → 1; distortion pushes toward noise
    flatness = librosa.feature.spectral_flatness(y=y)
    mean_flatness = float(np.mean(flatness))  # clean ~0.005–0.05, distorted ~0.05–0.4

    # 2. Zero-crossing rate: clipping saturates the waveform → more zero crossings
    zcr = librosa.feature.zero_crossing_rate(y)
    mean_zcr = float(np.mean(zcr))  # clean ~0.04–0.10, distorted ~0.10–0.30

    # 3. Crest factor (peak-to-RMS): distortion compresses dynamics → lower crest
    rms = float(np.sqrt(np.mean(y ** 2))) + 1e-8
    peak = float(np.max(np.abs(y))) + 1e-8
    crest = peak / rms  # clean guitar ~8–20, overdriven ~2–5

    # 4. High-freq energy ratio: distortion generates strong harmonics above 1 kHz
    stft = np.abs(librosa.stft(y, n_fft=2048))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    lo_e = stft[freqs < 800].mean() + 1e-8
    hi_e = stft[(freqs >= 800) & (freqs < 8000)].mean() + 1e-8
    hi_ratio = hi_e / lo_e  # clean ~0.1–0.25, distorted ~0.3–0.9

    # Normalise each feature into a 0-3 contribution (higher = more distortion)
    s_flat  = min(mean_flatness / 0.08,  3.0)   # saturates at 0.08 flatness
    s_zcr   = min(mean_zcr   / 0.10,  3.0)   # saturates at ZCR 0.10
    s_crest = min(max(0, (8 - crest) / 5), 3.0)  # 0 when crest≥8, 3 when crest≤3
    s_hi    = min(hi_ratio / 0.25, 3.0)       # saturates at ratio 0.25

    score = s_flat * 0.30 + s_zcr * 0.20 + s_crest * 0.30 + s_hi * 0.20

    if score >= 1.8:
        level = "Heavy"
    elif score >= 1.1:
        level = "Medium"
    elif score >= 0.5:
        level = "Light"
    else:
        level = "Clean"

    return {
        "detected": score >= 0.5,
        "level": level,
        "score": round(score, 3),
    }


def detect_tuning(wav_path: Path):
    import librosa
    import numpy as np

    y, sr = librosa.load(str(wav_path), mono=True, duration=60)
    # Estimate fundamental frequencies using harmonic product spectrum
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    mean_chroma = chroma.mean(axis=1)

    # Candidate tunings: (name, open string MIDI notes low→high)
    TUNINGS = {
        "Standard E":  [40, 45, 50, 55, 59, 64],  # E2 A2 D3 G3 B3 E4
        "Drop D":      [38, 45, 50, 55, 59, 64],  # D2 A2 D3 G3 B3 E4
        "Open G":      [38, 43, 50, 55, 59, 62],  # D2 G2 D3 G3 B3 D4
        "Open D":      [38, 45, 50, 54, 57, 62],  # D2 A2 D3 F#3 A3 D4
        "DADGAD":      [38, 45, 50, 55, 57, 62],  # D2 A2 D3 G3 A3 D4
        "Half Step Down": [39, 44, 49, 54, 58, 63],
        "Full Step Down": [38, 43, 48, 53, 57, 62],
    }

    def chroma_score(midi_notes):
        score = 0.0
        for m in midi_notes:
            pc = m % 12
            score += mean_chroma[pc]
        return score

    best = max(TUNINGS.items(), key=lambda kv: chroma_score(kv[1]))
    return best[0], best[1]


def run_basic_pitch(job_id, wav_path: Path):
    """Polyphonic guitar transcription: onset detection + CQT peak picking.

    pyin is monophonic (single note only) — useless for chords and distorted
    guitar.  This approach detects onsets (chord strikes / note attacks) and at
    each onset finds ALL significant frequency peaks in the CQT spectrum, giving
    us chord detection and much better accuracy on distorted signals.
    """
    import librosa
    import numpy as np

    y, sr = librosa.load(str(wav_path), mono=True)
    hop = 512

    # ── CQT covering full guitar range (E2 MIDI-40 → 5 octaves) ─────────────
    fmin = librosa.note_to_hz("E2")   # 82.4 Hz, MIDI 40
    n_bins = 60                        # 5 octaves × 12 bins/oct → up to MIDI 100
    C = np.abs(librosa.cqt(y, sr=sr, hop_length=hop, fmin=fmin,
                             n_bins=n_bins, bins_per_octave=12))

    # ── Onset detection on CQT-derived spectral flux ──────────────────────────
    onset_env = librosa.onset.onset_strength(
        S=librosa.amplitude_to_db(C, ref=np.max),
        sr=sr, hop_length=hop,
    )
    onset_frames = librosa.onset.onset_detect(
        onset_envelope=onset_env, sr=sr, hop_length=hop,
        backtrack=True, units="frames",
        pre_max=3, post_max=3, pre_avg=5, post_avg=5,
        delta=0.25, wait=3,
    )
    onset_times = librosa.frames_to_time(onset_frames, sr=sr, hop_length=hop)

    if len(onset_times) == 0:
        return []

    notes = []

    for i, (frame, onset_t) in enumerate(zip(onset_frames, onset_times)):
        # Short window right after the onset to measure attack spectrum
        win_end = min(frame + 6, C.shape[1])
        if frame >= C.shape[1]:
            continue

        mag = C[:, frame:win_end].max(axis=1)   # peak magnitude per pitch bin

        # Find local spectral peaks (must be locally maximal)
        peaks = []
        for b in range(1, len(mag) - 1):
            if mag[b] > mag[b - 1] and mag[b] >= mag[b + 1]:
                peaks.append((b, float(mag[b])))
        if not peaks:
            continue

        # Keep only peaks above 25 % of the strongest peak in this window
        max_mag = max(m for _, m in peaks)
        if max_mag == 0:
            continue
        peaks = [(b, m) for b, m in peaks if m >= 0.25 * max_mag]

        # Suppress harmonics: if bin b ≈ N × bin a (N = 2,3,4) and a is at
        # least 40 % as loud, bin b is a harmonic — remove it to avoid
        # duplicating the fundamental at its octave.
        def is_harmonic(b_hi, kept):
            f_hi = fmin * 2 ** (b_hi / 12)
            for b_lo, m_lo in kept:
                f_lo = fmin * 2 ** (b_lo / 12)
                for h in [2, 3, 4]:
                    if abs(f_hi / f_lo - h) < 0.09 and m_lo >= 0.40 * mag[b_hi]:
                        return True
            return False

        peaks.sort(key=lambda x: -x[1])   # strongest first
        kept = []
        for b, m in peaks:
            if not is_harmonic(b, kept):
                kept.append((b, m))
            if len(kept) >= 6:             # at most one note per string
                break

        # Duration = gap to next onset, capped at 2 s
        if i + 1 < len(onset_times):
            dur = min(onset_times[i + 1] - onset_t, 2.0)
        else:
            dur = 0.5
        dur = max(dur, 0.05)

        for b, _ in kept:
            midi = 40 + b                  # E2 (bin 0) = MIDI 40
            if not (40 <= midi <= 88):
                continue
            notes.append({
                "start":    round(onset_t, 3),
                "end":      round(onset_t + dur, 3),
                "pitch":    int(midi),
                "velocity": 80,
            })

    return notes


def build_tab(notes, open_strings, tuning_name):
    """Convert MIDI note events → ASCII guitar tab."""
    # open_strings: list of 6 MIDI notes, index 0 = low E (string 6), index 5 = high e (string 1)
    STRING_LABELS = ["e", "B", "G", "D", "A", "E"]  # high to low in display

    def midi_to_tab(midi_pitch):
        """Return (string_idx_display, fret) where string_idx_display 0=high e."""
        best = None
        for i, open_midi in enumerate(open_strings):
            fret = midi_pitch - open_midi
            if 0 <= fret <= 24:
                display_idx = 5 - i  # reverse: index 0 in open_strings = low E = display row 5
                if best is None or fret < best[1]:
                    best = (display_idx, fret)
        return best  # (display_row 0=high_e..5=low_E, fret)

    COL_WIDTH = 4
    COLS_PER_LINE = 16
    TOTAL_COLS = max(1, len(notes))

    # bucket notes into columns by time
    if not notes:
        return _render_tab_text([], STRING_LABELS, tuning_name), [], []

    duration = notes[-1]["end"] if notes else 1.0
    duration = max(duration, 0.1)

    columns = []  # list of {string_label: fret_str or "-"}
    tab_events = []

    for note in notes:
        result = midi_to_tab(note["pitch"])
        if result is None:
            continue
        disp_row, fret = result
        col_time = note["start"]
        tab_events.append({
            "time": col_time,
            "string": STRING_LABELS[disp_row],
            "fret": fret,
        })

    # Sort by time, group into columns (notes within 0.05s = same column)
    tab_events.sort(key=lambda e: e["time"])
    grouped = []  # list of (time, [events])
    current_group = []
    current_time = None
    for ev in tab_events:
        if current_time is None or abs(ev["time"] - current_time) < 0.05:
            current_group.append(ev)
            if current_time is None:
                current_time = ev["time"]
        else:
            grouped.append((current_time, current_group))
            current_group = [ev]
            current_time = ev["time"]
    if current_group:
        grouped.append((current_time, current_group))

    columns = []
    timings = []  # seconds timestamp per column for playback
    for t, group in grouped:
        col = {l: "-" for l in STRING_LABELS}
        for ev in group:
            col[ev["string"]] = str(ev["fret"])
        columns.append(col)
        timings.append(round(t, 3))

    tab_text = _render_tab_text(columns, STRING_LABELS, tuning_name)
    return tab_text, columns, timings


def _render_tab_text(columns, string_labels, tuning_name):
    COL_WIDTH = 4
    COLS_PER_LINE = 20

    lines_out = [f"Tuning: {tuning_name}\n"]
    lines_out.append("=" * (COL_WIDTH * COLS_PER_LINE + 2) + "\n\n")

    if not columns:
        for label in string_labels:
            lines_out.append(f"{label}|{'----' * COLS_PER_LINE}|\n")
        return "".join(lines_out)

    for chunk_start in range(0, len(columns), COLS_PER_LINE):
        chunk = columns[chunk_start: chunk_start + COLS_PER_LINE]
        for label in string_labels:
            row = f"{label}|"
            for col in chunk:
                cell = col.get(label, "-")
                row += cell.ljust(COL_WIDTH, "-")
            row += "|\n"
            lines_out.append(row)
        lines_out.append("\n")

    return "".join(lines_out)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
