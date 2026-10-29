"""Unit tests: hashing, matching, store round-trips, metadata fallback.

Integration tests against real audio live in test_integration.py.
"""


import numpy as np
import pytest

from fingerprints import config as C
from fingerprints.hashgen import hash_pair, hash_quad
from fingerprints.matcher import match
from fingerprints.metadata import title_from_filename
from fingerprints.spectrum import find_peaks, stft
from fingerprints.store import SqliteStore, TrackMeta, md5_file


def make_clip(freqs, seconds=6.0, sr=C.SR):
    """A sweepy tone clip rich enough to fingerprint."""
    t = np.arange(int(seconds * sr)) / sr
    x = np.zeros_like(t)
    for i, f in enumerate(freqs):
        x += np.sin(2 * np.pi * f * t + i)
    x += 0.05 * np.sin(2 * np.pi * 30 * t)  # below MIN_BIN: excluded from peaks
    return (x * 0.2).astype(np.float32)


@pytest.fixture(scope="module")
def clip_samples():
    return make_clip([440.0, 660.0, 880.0, 1320.0, 1760.0])


def test_hashes_are_deterministic_and_offsets_kept(clip_samples):
    mag = stft(clip_samples)
    peaks = find_peaks(mag)
    assert len(peaks) > 50
    hs1 = hash_pair(peaks)
    hs2 = hash_pair(peaks)
    assert hs1 == hs2
    h, t0 = next(iter(hs1))
    f1, f2, dt = h >> 48, (h >> 32) & 0xFFFF, h & 0xFFFFFFFF
    assert f1 != f2
    assert 0 <= dt <= C.MAX_DT
    assert 0 <= t0 < 10 ** 4


def test_quad_hashes_fit_range(clip_samples):
    mag = stft(clip_samples)
    peaks = find_peaks(mag)
    qh = hash_quad(peaks)
    assert 0 < len(qh) <= len(hash_pair(peaks)) / 2
    for h, _ in qh:
        assert 0 <= h < 2 ** 33


def test_verification_rescues_pitch_shift(tmp_path):
    # Query = a section generated at 0.97x pitch via tone math, so
    # absolute-bin hashes break at factor 1.0. The matcher's pass B rotates
    # the query by 1.03 to restore bin equality, votes exactly, then
    # verifies with a tight (factor-1.0) peak alignment.
    from fingerprints.matcher import match as do_match

    db = tmp_path / "tp.db"
    store = SqliteStore(str(db))
    store.init_schema()
    meta = TrackMeta("Test", "Song", "", str(tmp_path / "song.wav"), "x", 12.0)
    tid = store.upsert_track(meta)
    sec1 = make_clip([440, 660, 880, 1320])
    sec2 = make_clip([550, 770, 990, 1430])
    long = np.concatenate([sec1, sec2])
    p = find_peaks(stft(long))
    store.write_fingerprints(tid, hash_pair(p), peaks=p)
    # Pitch-shifted copy of sec2 (tones at 0.97x), same tempo.
    shifted = make_clip([550 * 0.97, 770 * 0.97, 990 * 0.97, 1430 * 0.97])
    qp = find_peaks(stft(shifted))
    m = do_match(store, hash_pair(qp), query_peaks=qp)
    assert m, "pitch-shifted query should be found via pass B"
    assert m[0].track["id"] == tid
    assert m[0].verify_score >= 0.5, m[0].verify_score


def test_pink_noise_rejected(clip_samples, tmp_path):
    from fingerprints.matcher import match as do_match
    from fingerprints.matcher import verify_match

    db = tmp_path / "tn.db"
    store = SqliteStore(str(db))
    store.init_schema()
    meta = TrackMeta("Test", "Song", "", str(tmp_path / "song.wav"), "x", 6.0)
    tid = store.upsert_track(meta)
    store.write_fingerprints(tid, hash_pair(find_peaks(stft(clip_samples))),
                             peaks=find_peaks(stft(clip_samples)))
    rng = np.random.default_rng(5)
    noise = (rng.standard_normal(6 * C.SR) * 0.3).astype(np.float32)
    qp = find_peaks(stft(noise))
    assert do_match(store, hash_pair(qp), query_peaks=qp) == []
    # Direct check: tight alignment must give noise a low score.
    m = verify_match(store, tid, 0.0, qp)
    assert m is not None and m < 0.35, m


def test_matcher_finds_identical_clip_somewhere_inside(clip_samples, tmp_path):
    # Four DIFFERENT 6s sections; query = section 2 (offset robustness).
    sr = C.SR
    secs = [
        make_clip([440, 660, 880, 1320]),
        make_clip([550, 770, 990, 1430]),
        make_clip([330, 495, 660, 1100]),
        make_clip([700, 900, 1200, 1600]),
    ]
    long = np.concatenate(secs)
    db = tmp_path / "t.db"
    store = SqliteStore(str(db))
    store.init_schema()
    meta = TrackMeta("Test", "Song", "", str(tmp_path / "song.wav"), md5_file(__file__), 24.0)
    tid = store.upsert_track(meta)
    store.write_fingerprints(tid, hash_pair(find_peaks(stft(long))))
    q = secs[1]
    res = match(store, hash_pair(find_peaks(stft(q))))
    assert res, "no match returned"
    assert res[0].track["id"] == tid
    # Section 2 starts at 6s; the estimate should land in the right 6s window.
    # (Pure tones make hashes translation-invariant, smearing vote deltas, so
    # confidence stays modest here - real music, covered by the integration
    # test, concentrates votes in one bin.)
    assert 5.0 < res[0].offset_seconds < 12.0, res[0].offset_seconds
    assert res[0].votes >= C.MIN_VOTES


def test_matcher_rejects_unrelated_audio(clip_samples, tmp_path):
    db = tmp_path / "t2.db"
    store = SqliteStore(str(db))
    store.init_schema()
    meta = TrackMeta("Test", "Song", "", str(tmp_path / "song.wav"), "x", 6.0)
    tid = store.upsert_track(meta)
    store.write_fingerprints(tid, hash_pair(find_peaks(stft(clip_samples))))
    rng = np.random.default_rng(5)
    noise = (rng.standard_normal(int(6 * C.SR)) * 0.3).astype(np.float32)
    res = match(store, hash_pair(find_peaks(stft(noise))), min_votes=15)
    assert res == []


def test_store_round_trip_and_dedupe(tmp_path):
    store = SqliteStore(str(tmp_path / "t3.db"))
    store.init_schema()
    meta = TrackMeta("A", "B", "C", "/x/y.mp3", "abc", 3.0)
    tid = store.upsert_track(meta)
    store.write_fingerprints(tid, {(123, 10), (456, 20), (123, 30)})
    assert store.is_ingested("/x/y.mp3") is False
    store.mark_ingested("/x/y.mp3", tid)
    assert store.is_ingested("/x/y.mp3") is True
    rows = store.find_matches([123, 456])
    assert sorted(rows) == [(123, tid, 10), (123, tid, 30), (456, tid, 20)]
    hits = store.find_matches([999])
    assert hits == []
    # Re-ingest same md5 -> no duplicate track.
    tid2 = store.upsert_track(meta)
    assert tid2 == tid
    # Different md5, same path -> same id, fingerprints wiped.
    meta2 = TrackMeta("A", "B", "C", "/x/y.mp3", "def", 3.0)
    tid3 = store.upsert_track(meta2)
    assert tid3 == tid
    store.write_fingerprints(tid3, {(1, 1)})
    assert store.find_matches([123]) == []
    s = store.stats()
    assert s["tracks"] == 1 and s["fingerprint_rows"] == 1
    store.delete_track(tid3)
    assert store.stats()["tracks"] == 0
    store.close()


def test_metadata_filename_fallback():
    assert title_from_filename("01 I'm A Monk.mp3") == "I'm A Monk"
    assert title_from_filename("1-04 Apes Live A Life.mp3") == "Apes Live A Life"
    assert title_from_filename("# Normal Citizen.mp3") == "Normal Citizen"
    assert title_from_filename("06 Easy.mp3") == "Easy"
