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
    return _select_guitar_audio(stem_root)


def _select_guitar_audio(stem_root: Path) -> Path:
    """Pick the best audio to transcribe from Demucs' 6 stems.

    htdemucs_6s separates: drums, bass, other, vocals, guitar, piano.
    Its "guitar" stem is trained on electric-guitar-in-a-band; for acoustic /
    fingerstyle recordings most of the guitar leaks into "other", leaving the
    guitar stem nearly silent. Transcribing that empty stem is why clean songs
    produced garbage.

    Strategy: compare RMS energy of the guitar stem vs the other stem. If the
    guitar stem is weak relative to other, sum guitar+other so we don't lose
    the acoustic guitar content. Drums/bass/vocals/piano are excluded to keep
    the signal guitar-focused.
    """
    import numpy as np
    import soundfile as sf

    guitar_p = stem_root / "guitar.wav"
    other_p  = stem_root / "other.wav"

    if not guitar_p.exists():
        if other_p.exists():
            return other_p
        raise RuntimeError(f"Could not find guitar stem in {stem_root}")

    def _rms(p: Path) -> float:
        try:
            data, _ = sf.read(str(p))
            if data.ndim > 1:
                data = data.mean(axis=1)
            return float(np.sqrt(np.mean(data ** 2))) if len(data) else 0.0
        except Exception:
            return 0.0

    g_rms = _rms(guitar_p)
    o_rms = _rms(other_p) if other_p.exists() else 0.0

    # Guitar stem is weak (acoustic leaked into "other") → combine the two.
    if other_p.exists() and (g_rms < 0.01 or g_rms < 0.5 * o_rms):
        try:
            g, sr = sf.read(str(guitar_p))
            o, _  = sf.read(str(other_p))
            if g.ndim > 1: g = g.mean(axis=1)
            if o.ndim > 1: o = o.mean(axis=1)
            n = min(len(g), len(o))
            combined = g[:n] + o[:n]
            peak = float(np.max(np.abs(combined))) or 1.0
            combined = combined / peak * 0.97   # normalise to avoid clipping
            out = stem_root / "guitar_plus_other.wav"
            sf.write(str(out), combined, sr)
            return out
        except Exception:
            return guitar_p

    return guitar_p


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


def transcribe_notes(job_id, wav_path: Path):
    """Polyphonic guitar transcription with Spotify's Basic Pitch (ONNX backend).

    Basic Pitch is a CNN trained on real polyphonic recordings (including
    GuitarSet — acoustic guitar). It outputs note events with pitch, start,
    end and amplitude, and natively detects CHORDS (multiple simultaneous
    notes), unlike a monophonic tracker. We then filter spurious notes by
    amplitude and clamp to the guitar range.
    """
    import warnings, logging
    warnings.filterwarnings("ignore")
    logging.disable(logging.WARNING)

    from basic_pitch.inference import predict
    from basic_pitch import ICASSP_2022_MODEL_PATH

    # onset/frame thresholds tuned for guitar:
    #  - lower onset_threshold catches soft fingerpicked notes
    #  - minimum_note_length filters sub-90ms blips (string noise, artifacts)
    _, _, note_events = predict(
        str(wav_path),
        ICASSP_2022_MODEL_PATH,
        onset_threshold=0.45,
        frame_threshold=0.30,
        minimum_note_length=90,        # ms
        minimum_frequency=73.4,        # D2 — covers Drop D / down-tunings
        maximum_frequency=1318.5,      # E6 — high frets on the high E string
        multiple_pitch_bends=False,
        melodia_trick=True,
    )

    if not note_events:
        return []

    # Amplitude-based spurious-note filter: keep notes whose confidence is at
    # least 18% of the loudest note. Removes the scattered "random notes" that
    # come from bleed/reverb without dropping genuine quiet chord tones.
    amps = [float(ev[3]) for ev in note_events if len(ev) > 3]
    amp_floor = (max(amps) * 0.18) if amps else 0.0

    notes = []
    for ev in note_events:
        start_t    = float(ev[0])
        end_t      = float(ev[1])
        pitch_midi = int(round(float(ev[2])))
        amplitude  = float(ev[3]) if len(ev) > 3 else 0.7

        if amplitude < amp_floor:
            continue
        if not (38 <= pitch_midi <= 88):      # E2 (low, Drop D) … E6
            continue

        notes.append({
            "start":    round(start_t, 3),
            "end":      round(end_t,   3),
            "pitch":    pitch_midi,
            "velocity": int(min(127, max(45, amplitude * 127))),
        })

    notes.sort(key=lambda n: (n["start"], n["pitch"]))
    return notes


# Backwards-compatible alias (process_audio still calls run_basic_pitch)
def run_basic_pitch(job_id, wav_path: Path):
    return transcribe_notes(job_id, wav_path)


def build_tab(notes, open_strings, tuning_name):
    """Convert MIDI note events → ASCII guitar tab with chord support.

    open_strings: 6 MIDI notes, index 0 = low E (string 6) … index 5 = high e.
    Notes that start within CHORD_WINDOW seconds of each other are treated as a
    chord/strum and assigned to DISTINCT strings (the previous version let a
    later note overwrite an earlier one on the same string, silently dropping
    chord tones).
    """
    STRING_LABELS = ["e", "B", "G", "D", "A", "E"]  # display order: high → low
    CHORD_WINDOW = 0.07   # seconds — notes this close = same strum
    MAX_FRET = 22

    if not notes:
        return _render_tab_text([], STRING_LABELS, tuning_name), [], []

    def candidate_strings(midi_pitch):
        """All playable (open_string_idx, fret) for this pitch, low string first."""
        out = []
        for i, open_midi in enumerate(open_strings):   # i: 0=low E … 5=high e
            fret = midi_pitch - open_midi
            if 0 <= fret <= MAX_FRET:
                out.append((i, fret))
        return out

    # ── Group notes into time columns ────────────────────────────────────────
    grouped = []          # list of (time, {pitch: velocity})
    cur_notes, cur_time = {}, None
    for note in notes:
        t = note["start"]
        p, v = note["pitch"], note.get("velocity", 80)
        if cur_time is None or abs(t - cur_time) <= CHORD_WINDOW:
            cur_notes[p] = max(cur_notes.get(p, 0), v)
            if cur_time is None:
                cur_time = t
        else:
            grouped.append((cur_time, cur_notes))
            cur_notes, cur_time = {p: v}, t
    if cur_notes:
        grouped.append((cur_time, cur_notes))

    def drop_octave_ghosts(note_vels):
        """Remove a note at P+12 if it's a weak harmonic of a stronger P.
        Genuine octave doublings (both struck) survive because both are loud;
        a harmonic ghost is much quieter than its fundamental."""
        pitches = set(note_vels)
        kept = {}
        for p, v in note_vels.items():
            if (p - 12) in pitches and v < 0.6 * note_vels[p - 12]:
                continue   # p is a weak octave-up ghost of p-12
            kept[p] = v
        return kept

    columns = []
    timings = []
    for t, note_vels in grouped:
        note_vels = drop_octave_ghosts(note_vels)
        # Keep the 6 loudest if more pitches than strings (compact, real voicing)
        if len(note_vels) > 6:
            top = sorted(note_vels.items(), key=lambda kv: -kv[1])[:6]
            note_vels = dict(top)
        pitches = list(note_vels)

        # Assign HIGHEST pitch to the HIGHEST available string. This reproduces
        # standard guitar voicings: bass notes land on bass strings at low frets,
        # treble notes on treble strings — e.g. C4 E4 G4 C3 E3 → open C major
        # (x32010) instead of a cluster of fret-15 notes on the low strings.
        used = set()                       # open_strings indices already taken
        col = {l: "-" for l in STRING_LABELS}
        for pitch in sorted(set(pitches), reverse=True):
            # candidate strings, highest string (thinnest) first
            cands = sorted(candidate_strings(pitch), key=lambda c: -c[0])
            for str_idx, fret in cands:
                if str_idx not in used:
                    used.add(str_idx)
                    display_idx = 5 - str_idx           # low E (idx0) → bottom row
                    col[STRING_LABELS[display_idx]] = str(fret)
                    break
            # If every candidate string is taken, the note can't be voiced
            # simultaneously on a 6-string guitar — drop it.
        if any(v != "-" for v in col.values()):
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
