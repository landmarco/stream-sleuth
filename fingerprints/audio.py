"""Audio decoding via ffmpeg into mono 16 kHz float samples."""

import subprocess

import numpy as np

from . import config as C


def decode(path, seconds=None):
    """Decode `path` to mono SR-Hz float32 samples in [-1, 1].

    Uses ffmpeg directly (already a stream-sleuth dependency) and reads raw
    PCM from stdout, so no extra codec stack is needed. `seconds` optionally
    truncates the decode (used by the benchmark for clip cutting).
    """
    args = [
        "ffmpeg", "-v", "error", "-i", str(path),
        "-f", "s16le", "-ac", "1", "-ar", str(C.SR),
    ]
    if seconds:
        args[1:1] = ["-t", str(seconds)]
    args.append("-")  # raw PCM to stdout
    proc = subprocess.run(args, capture_output=True, check=True)
    raw = np.frombuffer(proc.stdout, dtype=np.int16)
    return raw.astype(np.float32) / 32768.0
