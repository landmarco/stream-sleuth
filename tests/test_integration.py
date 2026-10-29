"""Integration test against the real tracks in ./test_tracks.

Skips silently when the folder is empty (e.g. a fresh checkout). Ingest a
small slice, cut clips at random offsets, and require correct identification.
Run:  pytest tests/ -m slow  (or pour a coffee and run the whole thing)
"""

import random

import pytest

from fingerprints import config as C
from fingerprints.audio import decode
from fingerprints.hashgen import hash_pair
from fingerprints.matcher import match
from fingerprints.pipeline import ingest_file
from fingerprints.spectrum import find_peaks, stft
from fingerprints.store import SqliteStore

TEST_DIR = "test_tracks"

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def corpus():
    from pathlib import Path

    files = sorted(Path(TEST_DIR).glob("*"))
    return files


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    db = tmp_path_factory.mktemp("fp") / "it.db"
    s = SqliteStore(str(db))
    s.init_schema()
    yield s
    s.close()


def test_ingest_matches_clips(corpus, store):
    if not corpus:
        pytest.skip("test_tracks/ is empty")
    clip_seconds = 8
    rng = random.Random(42)
    correct = total = 0
    for path in corpus[:10]:
        result = ingest_file(store, str(path), force=True)
        assert result is not None, f"ingest failed silently for {path}"
        tid, _ = result
        samples = decode(str(path))
        # 3 attempts: clip length probes different offsets (no overlap).
        for _ in range(3):
            start = rng.randint(0, max(0, len(samples) - clip_seconds * C.SR))
            q = samples[start : start + clip_seconds * C.SR]
            qp = find_peaks(stft(q))
            res = match(store, hash_pair(qp), query_peaks=qp)  # full verify path
            total += 1
            if res and res[0].track["id"] == tid:
                correct += 1
    assert total > 0
    assert correct / total >= 0.9, f"only {correct}/{total} clips matched"
    print(f"integration: {correct}/{total} random-offset clips identified")
