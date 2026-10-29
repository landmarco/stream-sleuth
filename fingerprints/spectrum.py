"""Short-time Fourier transform and spectral peak picking (numpy only).

Peaks are local maxima in frequency, per time frame, above a ratio of the
frame's peak amplitude. This is the landmark selection at the heart of
Wang 2003; parameter knobs live in fingerprints/config.py.
"""

import numpy as np

from . import config as C


def stft(samples):
    """Return magnitude spectrogram, shape (n_frames, n_bins), for MIN_BIN..MAX_BIN."""
    n = len(samples)
    if n < C.FFT_SIZE:
        return np.zeros((0, C.MAX_BIN - C.MIN_BIN + 1), dtype=np.float32)
    pad = (C.FFT_SIZE - n % C.HOP) % C.HOP
    if pad:
        samples = np.pad(samples, (0, pad))
    frames = np.lib.stride_tricks.sliding_window_view(samples, C.FFT_SIZE)[:: C.HOP]
    windowed = frames * np.hanning(C.FFT_SIZE).astype(np.float32)
    mag = np.abs(np.fft.rfft(windowed, axis=1))
    return mag[:, C.MIN_BIN : C.MAX_BIN + 1].astype(np.float32)


def find_peaks(mag):
    """Return [(frame, freq_bin), ...] landmarks, strongest-first per frame."""
    if mag.size == 0:
        return []
    peaks = []
    for t in range(mag.shape[0]):
        col = mag[t]
        mx = float(col.max())
        if mx < 1e-4:
            continue  # quiet frame
        thresh = mx * C.MIN_PEAK_RATIO
        mask = col >= thresh
        for k in range(1, C.PEAK_RADIUS + 1):
            mask[:-k] = mask[:-k] & (col[:-k] > col[k:])
            mask[k:] = mask[k:] & (col[k:] > col[:-k])
        idx = np.flatnonzero(mask)
        if len(idx) == 0:
            continue
        if len(idx) > C.PEAKS_PER_FRAME:
            vals = col[idx]
            keep = np.argpartition(-vals, C.PEAKS_PER_FRAME)[: C.PEAKS_PER_FRAME]
            idx = idx[np.sort(keep)]
        for b in idx:
            peaks.append((t, int(b) + C.MIN_BIN))
    return peaks
