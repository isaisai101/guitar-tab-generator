"""Subprocess entry point for Demucs separation.

Running Demucs in a subprocess (rather than in-process) gives each job a clean
CUDA/PyTorch state and lets us enforce a timeout + capture stderr for errors.
The torchaudio.save patch must be applied here before Demucs is imported.
"""
import sys

try:
    import soundfile as sf
    import torchaudio

    def _sf_save(uri, src, sample_rate, bits_per_sample=16, **kwargs):
        import numpy as np
        wav_np = src.numpy().T
        subtype = "PCM_16" if bits_per_sample <= 16 else "PCM_24"
        sf.write(str(uri), wav_np, int(sample_rate), subtype=subtype)

    torchaudio.save = _sf_save
except Exception:
    pass

from demucs.separate import main
main(sys.argv[1:])
