"""Runtime knobs for fingerprinting, all overridable via environment."""

import os


def _int(name, default):
    return int(os.environ.get(name, str(default)))


def _float(name, default):
    return float(os.environ.get(name, str(default)))


# Decode side: everything is normalized to mono 16 kHz before spectrograms.
SR = _int("SLEUTH_SR", 16000)

# Spectrogram: FFT window, hop in samples, analysed bin range.
FFT_SIZE = _int("SLEUTH_FFT_SIZE", 4096)          # ~186 ms window
HOP = _int("SLEUTH_HOP", 1024)                    # 64 ms between frames
MIN_BIN = _int("SLEUTH_MIN_BIN", 15)              # ~58 Hz at 16 kHz
MAX_BIN = _int("SLEUTH_MAX_BIN", 1800)            # ~7031 Hz

# Peak picking per frame.
PEAK_RADIUS = _int("SLEUTH_PEAK_RADIUS", 4)       # local-max radius in bins
PEAKS_PER_FRAME = _int("SLEUTH_PEAKS_PER_FRAME", 4)
MIN_PEAK_RATIO = _float("SLEUTH_MIN_PEAK_RATIO", 0.15)  # vs. frame max amplitude

# Combinatorial hashing.
FANOUT = _int("SLEUTH_FANOUT", 12)                # pairs per anchor peak
MAX_DT = _int("SLEUTH_MAX_DT", 2047)              # max pair span, in frames
HASH_MODE = os.environ.get("SLEUTH_HASH_MODE", "pair")   # pair | quad
QUAD_WINDOW = _int("SLEUTH_QUAD_WINDOW", 16)      # quad max span, frames
QUAD_GAP = _int("SLEUTH_QUAD_GAP", 2)             # min gap between quad members
QUAD_COMBOS = _int("SLEUTH_QUAD_COMBOS", 32)      # quad combos per anchor, cap

# Matching.
MIN_VOTES = _int("SLEUTH_MIN_VOTES", 20)          # votes for a credible match
MIN_GAP = _float("SLEUTH_MIN_GAP", 1.5)          # winner / second-best-track ratio
VOTE_BIN = _int("SLEUTH_VOTE_BIN", 3)             # offset delta bin, frames

# Store.
DB_URL = os.environ.get("SLEUTH_DB_URL", "sqlite:///./sleuth.db")
