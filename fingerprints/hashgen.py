"""Combinatorial hashing over spectral peak landmarks.

pair (default): Wang 2003 - each anchor peak is combined with the next
FANOUT peaks; a hash is (f1, f2, dt) packed into a 64-bit integer, stored
with the anchor's frame index as the time offset. Dense and simple; the
benchmark measures the rows-per-track cost.

quad (experimental): Sonnleitner & Widmer 2016 lineage, "quad-lite" - four
*consecutive* peaks are hashed by the frequency *ratios* inside the quad, the
inter-peak time *fractions*, and the total span. Ratios cancel under a
uniform pitch scaling and fractions under a uniform tempo scaling, so the
hash survives pitch shifts / speed changes that break pair hashes; it emits
roughly one hash per peak (~10x fewer rows than pairs). The cost of ratio-
only hashing is a shorter hash (33 bits) - collisions are absorbed by the
matcher's offset voting, since colliding rows scatter across delta bins.
A full combinatorial quad selection (their paper) can tighten robustness
later; this is the benchmarkable version.
"""


from . import config as C


def _pack(f1, f2, dt):
    # 16 bits each for frequency, 32 bits for the time delta: no collisions
    # until far past any personal-library size.
    return (f1 << 48) | (f2 << 32) | dt


def hash_pair(peaks):
    """[(hash, offset_frame), ...] from peak landmarks."""
    out = set()
    for i, (t, f1) in enumerate(peaks):
        end = min(i + 1 + C.FANOUT, len(peaks))
        for j in range(i + 1, end):
            t2, f2 = peaks[j]
            dt = t2 - t
            if dt <= 0 or dt > C.MAX_DT:
                continue
            out.add((_pack(f1, f2, dt), t))
    return out


def _ratio(num, den):
    """Signed 6-bit quantized ratio: round(64 * num / den), clamped."""
    if den == 0:
        return 0
    return max(-32, min(31, round(64 * num / den)))


def _frac(num, den):
    """Unsigned 4-bit fraction: round(16 * num / den), clamped."""
    if den == 0:
        return 0
    return max(0, min(15, round(16 * num / den)))


def hash_quad(peaks):
    """[(hash, offset_frame), ...] - time-spread 4-peak quads, ratio-encoded.

    Each quad is an anchor peak plus the next three peaks at least QUAD_GAP
    frames apart, all within QUAD_WINDOW*2 frames. Hash layout (33 bits):
    three 6-bit frequency ratios, two 4-bit time fractions, 7-bit total
    span. No absolute frequency - that is what makes it invariant under
    pitch scaling.
    """
    out = set()
    n = len(peaks)
    for i, (t0, f1) in enumerate(peaks):
        members = [(t0, f1)]
        j, t_prev = i, t0
        ok = True
        for _ in range(3):
            j = _spread(peaks, j, t_prev, C.QUAD_GAP)
            if j is None or j >= n or peaks[j][0] - t0 > 2 * C.QUAD_WINDOW:
                ok = False
                break
            t_prev = peaks[j][0]
            members.append(peaks[j])
        if not ok:
            continue
        (t1, f2), (t2, f3), (t3, f4) = members[1], members[2], members[3]
        span = t3 - t0
        r1 = _ratio(f2 - f1, f1)
        r2 = _ratio(f3 - f2, f2)
        r3 = _ratio(f4 - f3, f3)
        s1 = _frac(t1 - t0, span)
        s2 = _frac(t2 - t0, span)
        h = (
            ((r1 + 32) << 27)
            | ((r2 + 32) << 21)
            | ((r3 + 32) << 15)
            | (s1 << 11)
            | (s2 << 7)
            | span
        )
        out.add((h, t0))
    return out


def _spread(peaks, idx, t_prev, min_gap):
    """Index of the next peak >= min_gap frames after t_prev, else None."""
    while idx + 1 < len(peaks) and peaks[idx + 1][0] - t_prev < min_gap:
        idx += 1
    return idx + 1 if idx + 1 < len(peaks) else None


def fingerprint_peaks(peaks):
    """Dispatch on SLEUTH_HASH_MODE."""
    if C.HASH_MODE == "quad":
        return hash_quad(peaks)
    return hash_pair(peaks)


def unpack_pair(h):
    """(f1, f2, dt) back out of a pair hash (the pack layout)."""
    return (h >> 48, (h >> 32) & 0xFFFF, h & 0xFFFFFFFF)
