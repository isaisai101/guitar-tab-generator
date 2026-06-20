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


def _ensure_ffmpeg_in_path():
    """Add the winget-installed ffmpeg to PATH if it isn't already findable.

    On Windows, winget installs ffmpeg into the USER PATH, not MACHINE PATH.
    Flask subprocesses (yt-dlp, ffmpeg crop) inherit only the environment of
    the process that launched Flask, which may not include the user PATH.
    """
    import shutil, glob, os
    if shutil.which("ffmpeg"):
        return
    base = os.path.expandvars("%LOCALAPPDATA%\\Microsoft\\WinGet\\Packages")
    for exe in glob.glob(os.path.join(base, "Gyan.FFmpeg*", "**", "ffmpeg.exe"),
                         recursive=True):
        bin_dir = os.path.dirname(exe)
        os.environ["PATH"] = bin_dir + ";" + os.environ.get("PATH", "")
        print(f"[startup] ffmpeg added to PATH: {bin_dir}")
        return
    print("[startup] WARNING: ffmpeg not found — YouTube downloads may fail")


_ensure_ffmpeg_in_path()


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

def _clean_yt_url(url: str) -> str:
    """Strip playlist/radio params so yt-dlp downloads only the single video."""
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    # Keep only the video ID; drop list, start_radio, index, etc.
    clean_qs = {k: v for k, v in qs.items() if k == "v"}
    new_query = urlencode(clean_qs, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def process_youtube(job_id, url):
    try:
        url = _clean_yt_url(url)
        set_job(job_id, status="running", progress=5, message="Downloading from YouTube…")
        out_path = UPLOAD_DIR / f"{job_id}.%(ext)s"
        cmd = [
            "yt-dlp", "-x", "--audio-format", "wav",
            "--audio-quality", "0",
            "--no-playlist",
            "-o", str(out_path),
            url
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"yt-dlp failed:\n{stderr[-600:]}")
        matches = list(UPLOAD_DIR.glob(f"{job_id}.*"))
        if not matches:
            raise RuntimeError("yt-dlp produced no output file")
        src = matches[0]
        process_audio(job_id, src)
    except BaseException as e:
        # BaseException (not just Exception) catches SystemExit from yt-dlp/argparse
        set_job(job_id, status="error", message=str(e) or type(e).__name__)
        traceback.print_exc()


def _crop_to_90s(src: Path) -> Path:
    """Use ffmpeg to crop the input to 90 s before Demucs.

    Demucs on a 4-minute song takes 2-3 minutes even on GPU. 90 seconds covers
    the intro + first verse (most of what the user cares about) and keeps
    Demucs time to ~30-45 seconds.  Falls back to the original file if ffmpeg
    isn't in PATH or the file is already short.
    """
    out = UPLOAD_DIR / f"{src.stem}_90s.wav"
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src),
             "-t", "90",
             "-acodec", "pcm_s16le", "-ar", "44100",
             str(out)],
            capture_output=True, timeout=60,
        )
        if result.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return out
    except Exception:
        pass
    return src  # ffmpeg unavailable or failed — use full file


def process_audio(job_id, src: Path):
    try:
        # 1) Crop to 90 s so Demucs finishes in ~30-45 s instead of 2-3 min
        set_job(job_id, status="running", progress=8, message="Preparing audio…")
        src_90 = _crop_to_90s(src)

        # 2) Separate stems with Demucs
        set_job(job_id, status="running", progress=10,
                message="Separating guitar track with Demucs…")
        guitar_wav = run_demucs(job_id, src_90)

        # 3) Detect tuning
        set_job(job_id, status="running", progress=50, message="Detecting guitar tuning…")
        tuning_name, open_strings = detect_tuning(guitar_wav)

        # 4) Detect overdrive/distortion
        set_job(job_id, status="running", progress=58, message="Analysing amp tone…")
        overdrive_info = detect_overdrive(guitar_wav)

        # 5) Transcribe notes
        set_job(job_id, status="running", progress=65, message="Transcribing notes…")
        notes = run_basic_pitch(job_id, guitar_wav)

        # 6) Build tab
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
    except BaseException as e:
        # BaseException catches SystemExit (raised by demucs/argparse on failure),
        # which plain `except Exception` silently misses, leaving the job stuck "running".
        set_job(job_id, status="error", message=str(e) or type(e).__name__)
        traceback.print_exc()


def run_demucs(job_id, src: Path) -> Path:
    import torch
    from demucs.separate import main as demucs_main

    device = "cuda" if torch.cuda.is_available() else "cpu"
    out_root = OUTPUT_DIR / job_id

    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    _stop = threading.Event()

    def _ticker():
        pct = 10
        while not _stop.wait(6):
            pct = min(pct + 3, 45)
            set_job(job_id, progress=pct,
                    message=f"Separating guitar track ({device.upper()})…")

    t = threading.Thread(target=_ticker, daemon=True)
    t.start()

    try:
        demucs_main([
            "-n", "htdemucs_6s",
            "--device", device,
            "-o", str(out_root),
            str(src),
        ])
    finally:
        _stop.set()

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
    lo_e = float(stft[freqs < 800].mean()) + 1e-8
    hi_e = float(stft[(freqs >= 800) & (freqs < 8000)].mean()) + 1e-8
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
        "detected": bool(score >= 0.5),
        "level": level,
        "score": round(float(score), 3),
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
    """Guitar pitch detection: pYIN (time-domain autocorrelation) + power-chord check.

    WHY pYIN instead of CQT salience or Basic Pitch:

    Distortion preserves the FUNDAMENTAL PERIOD of a note — it just adds
    harmonics at integer multiples. pYIN finds periodicity in the time domain
    (Cumulative Mean Normalized Difference Function) and is therefore robust
    to any amount of distortion. CQT peak-picking and neural models trained on
    clean audio both fail because they look at the frequency domain, where
    distortion creates a flat wall of harmonic energy across all bins.

    For a distorted E5 power chord (E2 + B2), the combined waveform has the
    period of E2 (root), so pYIN correctly returns E2 even through a
    WaveShaper-style guitar distortion.

    After detecting the root via pYIN, we check whether the fifth (+7 semitones)
    has significant CQT energy at the onset — if yes, it's a power chord and
    we emit both notes. Single-note riffs only produce one note per onset.
    """
    import librosa
    import numpy as np

    y, sr = librosa.load(str(wav_path), mono=True, duration=90.0)
    hop = 512

    FMIN_HZ = librosa.note_to_hz("D2")   # 73.4 Hz — MIDI 38, covers Drop D
    FMAX_HZ = librosa.note_to_hz("A5")   # 880 Hz — MIDI 81, covers high frets

    # ── CQT — used only for onset detection and fifth-energy check ────────────
    C = np.abs(librosa.cqt(y, sr=sr, hop_length=hop,
                            fmin=FMIN_HZ, n_bins=62, bins_per_octave=12))

    onset_env = librosa.onset.onset_strength(
        S=librosa.amplitude_to_db(C, ref=np.max), sr=sr, hop_length=hop,
    )
    onset_frames = librosa.onset.onset_detect(
        onset_envelope=onset_env, sr=sr, hop_length=hop,
        backtrack=True, units="frames",
        pre_max=3, post_max=3, pre_avg=7, post_avg=7,
        delta=0.3, wait=6,
    )
    onset_times = librosa.frames_to_time(onset_frames, sr=sr, hop_length=hop)

    if len(onset_times) == 0:
        return []

    # ── pYIN — time-domain pitch over the full audio ─────────────────────────
    # frame_length=2048 ≈ 46ms, gives pitch accuracy of ±0.5 semitones
    f0_arr, voiced_flag, _ = librosa.pyin(
        y,
        fmin=FMIN_HZ,
        fmax=FMAX_HZ,
        sr=sr,
        hop_length=hop,
        frame_length=2048,
    )

    notes = []

    for i, (frame, onset_t) in enumerate(zip(onset_frames, onset_times)):
        win_end = min(frame + 8, len(f0_arr))
        if frame >= len(f0_arr):
            continue

        # Take voiced pYIN pitch across 8 frames (~93 ms after onset)
        f0_win     = f0_arr[frame:win_end]
        voiced_win = voiced_flag[frame:win_end]
        valid_f0   = f0_win[voiced_win & ~np.isnan(f0_win)]

        if len(valid_f0) == 0:
            continue

        root_hz  = float(np.median(valid_f0))
        root_midi = int(round(librosa.hz_to_midi(root_hz)))

        if not (38 <= root_midi <= 81):
            continue

        dur = float(onset_times[i + 1] - onset_t) if i + 1 < len(onset_times) else 0.5
        dur = max(min(dur, 2.0), 0.06)

        notes.append({
            "start":    round(float(onset_t), 3),
            "end":      round(float(onset_t) + dur, 3),
            "pitch":    root_midi,
            "velocity": 80,
        })

        # ── Power-chord fifth detection ────────────────────────────────────
        # The fifth is root + 7 semitones (e.g. E2 + B2).
        # B2 is NOT a harmonic of E2 (harmonics are 2x, 3x, … not 1.5x),
        # so CQT energy at the fifth bin comes only from the fifth actually
        # being played — not from the root's own harmonic series.
        fifth_midi = root_midi + 7
        if 38 <= fifth_midi <= 88:
            root_bin  = int(round(12 * np.log2(root_hz / FMIN_HZ)))
            fifth_bin = root_bin + 7
            win_c     = min(frame + 8, C.shape[1])
            if 0 <= root_bin < C.shape[0] and fifth_bin < C.shape[0]:
                root_e  = float(C[root_bin,  frame:win_c].mean())
                fifth_e = float(C[fifth_bin, frame:win_c].mean())
                if root_e > 1e-8 and fifth_e / root_e > 0.30:
                    notes.append({
                        "start":    round(float(onset_t), 3),
                        "end":      round(float(onset_t) + dur, 3),
                        "pitch":    fifth_midi,
                        "velocity": 72,
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
    # use_reloader=False: Werkzeug's reloader would restart the process mid-job,
    # wiping the in-memory jobs dict and killing background threads.
    app.run(debug=True, port=5000, use_reloader=False)
