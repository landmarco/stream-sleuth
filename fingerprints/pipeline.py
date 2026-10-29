"""The shared ingest/fingerprint pipeline used by the CLI, GraphQL and benchmark.

One track = one transaction: decode, spectrogram, hash, upsert metadata,
then bulk-load fingerprints. A failed track leaves no half-written state and
is simply retried next run (ingest_log marks completed sources).
"""

import os
import sys
import time
from pathlib import Path

from . import config as C
from .audio import decode
from .hashgen import fingerprint_peaks
from .metadata import read_metadata
from .spectrum import find_peaks, stft
from .store import TrackMeta, md5_file


def fingerprint_track(path, with_meta=True):
    """Decode + fingerprint one file. Returns (samples, peaks, hashes) or raises."""
    samples = decode(path)
    mag = stft(samples)
    peaks = find_peaks(mag)
    hashes = fingerprint_peaks(peaks)
    return samples, peaks, hashes


def ingest_file(store, path, artist=None, title=None, album=None, force=False):
    """Fingerprint `path` into `store`. Returns (track_id, n_hashes) or None if skipped."""
    path = str(path)
    if store.is_ingested(path) and not force:
        return None
    samples, peaks, hashes = fingerprint_track(path)
    if not hashes:
        raise ValueError(f"no peaks found in {path} - silence or broken decode?")
    if artist is None:
        artist, title, album = read_metadata(path)
    meta = TrackMeta(
        artist=artist or "",
        title=title or "",
        album=album or "",
        source_path=str(path),
        file_md5=md5_file(path),
        duration=len(samples) / C.SR,
    )
    track_id = store.upsert_track(meta)
    store.write_fingerprints(track_id, hashes, peaks=peaks)
    store.mark_ingested(str(path), track_id)
    return track_id, len(hashes)


AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".wma", ".opus")


def scan_library(root):
    """Walk nested directories for audio files, sorted for stable ordering."""
    found = []
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            if name.lower().endswith(AUDIO_EXTENSIONS):
                found.append(str(Path(dirpath) / name))
    return found


def ingest_library(store, root, force=False, limit=None, quiet=False, on_track=None):
    """Ingest every audio file under `root`. Resumable; returns (done, skipped, failed)."""
    files = scan_library(root)
    if limit:
        files = files[:limit]
    done = skipped = failed = 0
    t0 = time.time()
    for i, path in enumerate(files, 1):
        try:
            result = ingest_file(store, path, force=force)
            if result is None:
                skipped += 1
            else:
                done += 1
            status = "ok" if result else "skip"
        except Exception as e:
            failed += 1
            status = f"FAIL {e}"
        if not quiet:
            print(f"[{i}/{len(files)}] {status}  {path}", file=sys.stderr)
        if on_track:
            on_track(i, len(files))
    elapsed = time.time() - t0
    elapsed = max(elapsed, 1e-6)
    return {"done": done, "skipped": skipped, "failed": failed,
            "elapsed_s": elapsed, "files": len(files)}
