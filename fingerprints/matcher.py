"""Offset-voting matcher (Wang 2003 / dejavu lineage).

Every fingerprint row records (hash, track_id, time_offset). A query clip
produces (hash, query_offset) pairs; each database hit votes for
(track_id, delta) where delta = query_offset - db_offset. A correct match
keeps delta roughly constant across many hashes, so the track with the
largest single delta-bin wins. The winning delta also tells us where in the
song the clip is - useful for radio playlist verification.
"""

from collections import defaultdict

from . import config as C
from .hashgen import fingerprint_peaks


class Match:
    def __init__(self, track, votes, confidence, offset_seconds,
                 verify_score=None):
        self.track = track
        self.votes = votes
        self.confidence = confidence
        self.offset_seconds = offset_seconds
        self.verify_score = verify_score


def match(store, query_hashes, min_votes=None, min_gap=None, top_k=5,
          query_peaks=None, verify=True):
    """Rank tracks by offset-consistent vote counts for a query.

    Two-stage design:
    - Pass A (exact, factor 1.0): strict hash-equality voting + vote gates +
      peak verification. Covers every normal-speed case; fast.
    - Pass B (pitch rescue, rotated factors): only runs when A finds no
      winner. The query peaks are rotated by 0.94..1.06 and re-hashed, so
      a pitch-shifted clip restores hash equality under its own factor;
      candidates are checked by tight peak alignment (verify_match) and the
      first verified one wins. Candidate volume stays tiny (exact voting), so
      this scales to a 400k-track library. Beyond a few percent shift it
      degrades - exact-hash systems are pitch-fragile by design; the Phase 2
      neural matcher and the quad mode are the remedies.
    """
    min_votes = C.MIN_VOTES if min_votes is None else min_votes
    min_gap = C.MIN_GAP if min_gap is None else min_gap

    def _rank(qh, bin_frames):
        q_offsets = {h: t for h, t in qh}
        if C.HASH_MODE == "quad":
            rows = [(q_offsets[h], tid, off)
                    for h, tid, off in store.find_matches(list(q_offsets))]
        else:
            rows = [(q_offsets[h], tid, off)
                    for h, tid, off in store.find_matches(list(q_offsets))]
        matched_rows = len(rows)
        votes = defaultdict(int)
        for q_off, track_id, db_off in rows:
            delta = db_off - q_off  # song position of the query anchor
            votes[(track_id, delta // bin_frames)] += 1
        best = {}
        for (track_id, b), n in votes.items():
            if n > best.get(track_id, (0, 0))[0]:
                best[track_id] = (n, b)
        return sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0])), matched_rows

    def _verify_and_build(ranked, matched_rows, limit, verify_peaks):
        out = []
        seen = set()
        for rank, (track_id, (n, b)) in enumerate(ranked):
            if track_id in seen or len(seen) >= limit:
                continue
            seen.add(track_id)
            track = store.get_track(track_id)
            if track is None:
                continue
            delta_frames = (b + 0.5) * bin_frames
            m = Match(
                track,
                votes=n,
                confidence=min(1.0, n / max(1, matched_rows)),
                offset_seconds=delta_frames * C.HOP / C.SR,
            )
            if verify and verify_peaks is not None:
                m.verify_score = verify_match(
                    store, track["id"], m.offset_seconds, verify_peaks
                )
                # Thresholds overlap (true matches scored 0.40-0.46 on some
                # quiet segments; noise ~0.35 on the densest tracks), so a
                # clear vote lead lowers the bar: nobody else comes close
                # when the winner leads 10x, but a 2-3k-vote tie needs the
                # stricter alignment score.
                clear_lead = (
                    rank == 0
                    and (
                        len(ranked) == 1
                        or ranked[1][1][0] == 0
                        or n / ranked[1][1][0] >= 2.0
                    )
                )
                need = 0.38 if clear_lead else 0.55
                if not (m.verify_score is not None and m.verify_score >= need):
                    continue  # hash votes said maybe; peak alignment says no
            out.append(m)
        return out

    bin_frames = C.VOTE_BIN
    if query_peaks is None or not verify:
        # Votes-only path (unit tests, callers without peaks): exact, gated.
        ranked, matched_rows = _rank(query_hashes, bin_frames)
        if ranked and ranked[0][1][0] >= min_votes:
            gate_ok = (
                len(ranked) == 1
                or ranked[1][1][0] == 0
                or ranked[0][1][0] / ranked[1][1][0] >= min_gap
            )
            if gate_ok:
                return _verify_and_build(ranked, matched_rows, top_k, query_peaks)
        return []

    # Pass A: exact voting at factor 1.0. Votes only rank the candidates;
    # the peak-alignment check is the gate (the second-best track's votes
    # can legitimately approach the winner's in a dense library, so a vote
    # gap would reject real matches).
    ranked, matched_rows = _rank(query_hashes, bin_frames)
    if ranked and ranked[0][1][0] >= min_votes:
        found = _verify_and_build(ranked, matched_rows, top_k, query_peaks)
        if found:
            return found

    # Pass B: rotated factors, exact voting, verify-driven (candidate volume
    # stays tiny so this scales; tolerance-based ballots would not).
    for factor in (0.97, 1.03, 0.94, 1.06):
        rotated = fingerprint_peaks(_rotate(query_peaks, factor))
        # Verification must happen in the db's bin space; the rotated peaks
        # (already normalized) are the ones the alignment checks.
        verify_peaks = list(_rotate(query_peaks, factor))
        ranked, matched_rows = _rank(rotated, bin_frames)
        if not ranked:
            continue
        found = _verify_and_build(ranked, matched_rows, max(top_k, 15), verify_peaks)
        if found:
            return found
    return []


def _rotate(peaks, factor):
    """Rotate peak bins by a pitch factor, clamped to the analysed range."""
    if factor == 1.0:
        return peaks
    return [
        (t, max(C.MIN_BIN, min(C.MAX_BIN, round(b * factor))))
        for t, b in peaks
    ]


VERIFY_BIN_TOL = 2   # bins: absorbs rotation rounding + resampler smearing


def verify_match(store, track_id, offset_seconds, query_peaks):
    """Fraction of query peaks explained by the track at the estimated offset.

    Strict by design: the query peaks are compared IN THE DB'S OWN bin
    space (pitch rotation, when used, has already normalized them), so only
    factor 1.0 with a small bin tolerance is meaningful. A pitch-factor
    sweep here would let noise "verify" by chance - measured at ~60% for
    pink noise against arbitrary tracks. Returns best fraction within the
    delta uncertainty window, or None if the track has no peaks.
    """
    db_peaks = store.get_peaks(track_id)
    if not db_peaks:
        return None
    by_frame = {}
    for t, b in db_peaks:
        by_frame.setdefault(t, []).append(b)
    offset_frames = offset_seconds * C.SR / C.HOP
    # The vote delta is only exact to its bin width (VOTE_BIN frames), so
    # test the three neighbouring projections; noise peaks line up on one
    # of them by chance ~0.35 of the time at dense DBs, a real match ~0.7+.
    best = 0.0
    for shift in (-1, 0, 1):
        tf = round(offset_frames) + shift
        matched = 0
        for t, b in query_peaks:
            bins = by_frame.get(round(t) + tf)
            if bins is None:
                continue
            if any(abs(tb - b) <= VERIFY_BIN_TOL for tb in bins):
                matched += 1
        best = max(best, matched / max(1, len(query_peaks)))
    return best

    """True when a candidate clears the verification gate."""
    s = match_result.verify_score
    return s is not None and s >= VERIFY_THRESHOLD
