"""Benchmark: ingest ./test_tracks, match degraded clips, report real numbers.

Produces the per-track fingerprint density, ingest rate, and match-latency
figures that the 300-400k-track projections are based on, plus accuracy on
realistic radio degradations (gain, bitrate, pitch shift, silence).

Run from the repo root:
    SLEUTH_DB_URL=sqlite:////tmp/sleuth-bench.db python scripts/benchmark.py

Output: markdown-ready table on stdout; JSON at
~/codetools/friendly-harness/artifacts/stream-sleuth/benchmark.json
"""

import json
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from fingerprints import config as C
from fingerprints.audio import decode
from fingerprints.hashgen import fingerprint_peaks
from fingerprints.matcher import match
from fingerprints.pipeline import ingest_library
from fingerprints.spectrum import find_peaks, stft
from fingerprints.store import open_store

TEST_DIR = Path("test_tracks")
ARTIFACTS = Path.home() / "codetools/friendly-harness/artifacts/stream-sleuth"


def degraded(path, kind, seconds=8, rng=None):
    """Encode a degraded copy and return its path (caller deletes)."""
    rng = rng or random.Random(1)
    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    start = rng.randint(0, 40)
    if kind == "gain":
        filt = f"volume=0.25,atrim=start={start},asetpts=PTS-STARTPTS"
    elif kind == "bitrate":
        filt = f"atrim=start={start},asetpts=PTS-STARTPTS"
        args = ["ffmpeg", "-y", "-v", "error", "-i", str(path), "-t", f"{seconds + 2}",
                "-af", filt, "-ac", "1", "-ar", "8000", tmp]
        subprocess.run(args, check=True)
        return tmp
    elif kind == "pitch":
        filt = f"asetrate=16000*0.97,aresample=16000,atempo=1/0.97,atrim=start={start},asetpts=PTS-STARTPTS"
    else:
        raise ValueError(kind)
    args = ["ffmpeg", "-y", "-v", "error", "-i", str(path), "-t", f"{seconds + 2}",
            "-af", filt, "-ac", "1", "-ar", str(C.SR), tmp]
    subprocess.run(args, check=True)
    return tmp


def main():
    files = sorted(TEST_DIR.iterdir())
    if not files:
        print("test_tracks/ is empty; nothing to benchmark")
        return 1
    url = os.environ.get("SLEUTH_DB_URL", f"sqlite:///{ARTIFACTS}/bench.db")
    store = open_store(url)
    rng = random.Random(7)

    print(f"== ingest {len(files)} test tracks (hash mode: {C.HASH_MODE}) ==")
    t0 = time.time()
    res = ingest_library(store, str(TEST_DIR), force=True, quiet=True)
    ingest_s = time.time() - t0
    stats = store.stats()
    rows = stats["fingerprint_rows"]
    print(f"  {res['done']} tracks in {ingest_s:.1f}s "
          f"({len(files) / ingest_s:.2f} tracks/s; {rows / max(ingest_s, 1e-6):,.0f} rows/s)")
    rows_per_track = rows / max(1, res["done"])
    print(f"  rows/track: {rows_per_track:,.0f}   (projected 400k tracks: "
          f"{rows_per_track * 400_000 / 1e9:.1f}B rows)")

    print("\n== identity probes: 6s and 12s clips at random song offsets ==")
    results = {"mode": C.HASH_MODE, "rows_per_track": rows_per_track,
               "ingest_tracks_per_s": len(files) / ingest_s}
    for clip_len in (6, 12):
        ok = tot = 0
        latencies = []
        for path in files:
            samples = decode(str(path))
            for _ in range(3):
                start = rng.randint(0, max(0, len(samples) - clip_len * C.SR))
                q = samples[start : start + clip_len * C.SR]
                qp = find_peaks(stft(q))
                qh = fingerprint_peaks(qp)
                t1 = time.perf_counter()
                m = match(store, qh, top_k=1, query_peaks=qp)  # verified path
                latencies.append((time.perf_counter() - t1) * 1000)
                tot += 1
                ok += bool(m) and m[0].track["source_path"].endswith(path.name)
        pct = 100.0 * ok / tot
        med = sorted(latencies)[len(latencies) // 2]
        print(f"  {clip_len}s clips: {ok}/{tot} = {pct:.1f}%  (median {med:.0f}ms lookup)")
        results[f"id_{clip_len}s"] = {"ok": ok, "total": tot, "median_ms": med}

    print("\n== degradation probes (8s clips) ==")
    for kind in ("gain", "bitrate", "pitch"):
        ok = tot = 0
        for path in files[:15]:
            tmp = degraded(str(path), kind, rng=rng)
            try:
                qp = find_peaks(stft(decode(tmp, seconds=8)))
                qh = fingerprint_peaks(qp)
                m = match(store, qh, top_k=1, query_peaks=qp)
                tot += 1
                ok += bool(m) and m[0].track["source_path"].endswith(path.name)
            finally:
                Path(tmp).unlink(missing_ok=True)
        pct = 100.0 * ok / tot
        print(f"  {kind:8s}: {ok}/{tot} = {pct:.1f}%")
        results[f"degraded_{kind}"] = {"ok": ok, "total": tot}

    print("\n== negative control: silence + pink noise ==")
    for label, filt in (("silence", "anullsrc"), ("pink-noise", "anoisesrc=color=pink")):
        fd, tmp = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", filt,
                        "-t", "8", "-ac", "1", "-ar", str(C.SR), tmp], check=True)
        qp = find_peaks(stft(decode(tmp)))
        qh = fingerprint_peaks(qp)
        Path(tmp).unlink(missing_ok=True)
        m = match(store, qh, top_k=1, query_peaks=qp)
        verdict = "no match (good)" if not m else f"FALSE POSITIVE: {m[0].track}"
        print(f"  {label:12s}: {verdict}")
        results[f"negative_{label.replace('-', '_')}"] = bool(m)

    store.close()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out = ARTIFACTS / "benchmark.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\nJSON: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
