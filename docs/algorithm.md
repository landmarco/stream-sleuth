# How the local fingerprint matcher works

The `fingerprints/` package turns audio into searchable hashes and matches
recordings against them. This document explains the signal chain, the
hashing schemes and where they come from, the database layout, the matching
and verification logic, and the measured behavior. It is the design record
behind the benchmark numbers in the README.

## Goals

- Identify an 8-12 s recording against a personal library, at the correct
  track _and_ offset, fast enough to answer every ~25 s.
- Survive radio degradations: gain differences, re-encodes, resampling.
- Reject what is not in the library (silence, noise, talk) instead of
  guessing.
- Scale toward the real library: ~300-400k tracks, ~3 TB.

The design constraint at every step: **hashes are stored, never
recomputed** at query time, so matching is a small number of indexed SQL
lookups.

## Signal chain

All audio is normalized before analysis; nothing downstream depends on
sample rate, channels, or codec.

1. `ffmpeg` decodes any container (`audio.py`) to mono 16 kHz signed
   16-bit PCM - the standard for audio fingerprinting (Shazam works at
   comparable rates; Wang's paper uses 16 kHz and dejavu does too).
2. A magnitude spectrogram (`spectrum.py`): FFT size 4096 (~186 ms window
   at 16 kHz), hop 1024 (64 ms between frames), analysed bins 15-1800
   (~58 Hz - 7.0 kHz). The bottom 15 bins are dropped - that is mostly
   rumble and room tone - and the top end is cut where few musical peaks
   live.
3. Peak picking per frame: keep bins whose magnitude is a local max over a
   +/-4-bin radius and at least 15% of the frame maximum, strongest 4 per
   frame. A frame with no energy above `1e-4` is skipped entirely.
   Result: an ordered list of `(frame, freq_bin)` landmarks, up to 4
   peaks per 64 ms frame.

Peaks, not spectrogram slices, because a peak (a time, a frequency) is
invariant under most non-destructive transformations: gain scales the whole
frame so the 15%-of-max rule and local maxima survive; a re-encode barely
moves a spectral peak; and the (time, frequency) pair anchors every hash
below.

## Fingerprints

A fingerprint hash ties peaks together so that a short query - which
contains a different _window_ of the song - still matches: the hashes are
made of relative structure, not absolute positions, and the song position
is recovered by voting.

### Pair hashing (Wang 2003, `hashgen.py::hash_pair`)

The reference algorithm from Avery Wang's "Industrial-Strength Audio
Search Algorithm" (the original Shazam paper; dejavu is the well-known
MIT-licensed implementation of the same idea).

For every anchor peak, look at the next `FANOUT` (12) peaks. Each pair
(anchor frequency f1, target frequency f2, time gap dt) becomes a 64-bit
hash:

```
(f1 << 48) | (f2 << 32) | dt
```

The packed value is stored together with the anchor's frame index as the
time offset. The frequency fields make the hash selective; the time gap -
not absolute time - gives transposition tolerance (a clip starting 40 s in
produces the same hashes, just shifted offsets).

Density is the price: combinatorial pairing emits ~108k rows per average
track (see benchmark), which is why the storage layer matters (below).

### Quad hashing (Sonnleitner & Widmer 2016, `hashgen.py::hash_quad`)

Sonnleitner & Widmer's "Quad-Based Audio Fingerprinting" (IEEE/ACM
Transactions on Audio, Speech, and Language Processing) showed that
hashing a _quad_ of peaks by the frequency _ratios_ inside it - instead of
absolute frequencies - makes the hash invariant under uniform pitch
scaling, and time _fractions_ make it invariant under tempo scaling.

`hash_quad` is a compact "quad-lite" take: each quad is an anchor peak plus
the next three peaks at least `QUAD_GAP` frames apart within `QUAD_WINDOW`
frames. The 33-bit hash packs:

- three 6-bit frequency ratios `(f2-f1)/f1`, `(f3-f2)/f2`, `(f4-f3)/f3`
- two 4-bit time fractions (when members 2 and 3 fall inside the span)
- a 7-bit total span

No absolute frequency at all. Emits ~7k rows per track - 15x denser
reduction than pairs - and the benchmark shows it matches identity clips
at 3-4x lower latency. The trade-off: 33-bit hashes collide, so the
matcher's offset voting must absorb them (they scatter across delta bins),
and the ratios are quantized to 6 bits, which makes them fragile to the
+/-1-bin jitter of real resampler chains - the source of the measured
pitch weakness discussed below.

### Why not the other OSS options

Surveyed before writing any code: `worldveil/dejavu` (Wang, small-scale,
MySQL focus), `AddictedCS/soundfingerprinting` (.NET, excellent but the
wrong language for this service), `acoustid/chromaprint` (LGPL, but real
matching is a hosted web service, not a library), `dpwe/audfprint` and
`sfluor/musig` (research code). We implement the Wang and quad schemes
directly on our own pipeline (numpy STFT, SQLite/PG storage) so the two
hash modes share one code path and one benchmark.

## Storage (`store.py`)

Four tables, identical shape across backends:

| table                                                                  | purpose                                                                                                                    |
| ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `tracks`                                                               | one row per library file: tags (mutagen, filename fallback), `source_path` UNIQUE, `file_md5`, duration, fingerprint count |
| `fingerprints` (pair) / `fingerprints_pair` + `fingerprints_quad` (PG) | the hashes: `hash, f1, f2, dt, track_id, time_offset`                                                                      |
| `peaks`                                                                | `(track_id, time_offset, freq_bin)` - the landmarks, kept for verification                                                 |
| `ingest_log`                                                           | `path` → `track_id`, the resumability diary                                                                                |

**SQLite** (dev / small libraries): indexes on `hash` (covering, with
track and offset) and `(f1, f2, dt)` for tolerance-aware lookups.
`find_matches` chunks the query's hashes into `BATCH`-sized IN lists
(400; SQLite's parameter limit is raised to 100 000 for the tolerant
lookup).

**Postgres** (production, beachyhead): the pair table is
**range-partitioned on `f1` into 32 partitions** (each covers 2048 of the
65536 possible f1 values) with a covering index
`(f1, f2, dt, track_id, time_offset)` - a query's IN-list then touches
only the partitions its f1 ranges land in. The quad table is
**hash-partitioned into 16 partitions** on the 33-bit hash. Loads use
bounded `COPY` batches inside a per-track transaction, so an interrupted
ingest leaves the track-row atomically absent and `ingest_log` clean for a
resume.

The $400k-scale arithmetic that forced partitioning:

- pair: 107 791 rows/track x 400 000 = **43.1B rows** (not viable as a
  single table; partition pruning + the covering index is what makes the
  question healthy)
- quad: 6 968 x 400 000 = **2.8B rows** - big but tractable
- neural embeddings (Phase 2, `docs/phase2-neural.md`): ~100-200M vectors,
  5-15 GB in a Faiss index - the scale answer for the full library

## Ingest (`pipeline.py`)

`scan_library` walks nested directories for `.mp3/.wav/.m4a/.flac/.ogg`
and hands each file to a transactional, resumable pipeline: decode ->
spectrogram -> peaks -> hashes -> one transaction (insert track, COPY the
rows, mark `ingest_log`) -> commit. A re-run skips everything in
`ingest_log`; `--force` re-fingerprints. Measured: pair mode ~0.4
tracks/s, quad mode ~2.2 tracks/s on a laptop - so a 400k library is an
ingest-time problem measured in days, which is why it runs as a batch job
on the server, not interactively.

## Matching (`matcher.py`)

The query clip runs through the same signal chain. Then:

1. **Lookup**: exact IN-lookups of the query hashes against the
   fingerprints table, in `BATCH`-sized chunks.
2. **Voting**: for every hit, `delta = db_time_offset - query_offset`
   estimates where the query sits in the database track. Votes accumulate
   per `(track, delta // VOTE_BIN)`: a real match concentrates most hashes
   in one 3-frame delta bin; coincidental hits scatter.
3. **Ranking**: each track's best bin. `MIN_VOTES` (20) floors noise.
4. **Verification** - the actual gate (see below).
5. **Pitch rescue** (pass B): only when pass A finds no verified match,
   re-hash the query peaks rotated by each of 0.97/1.03/0.94/1.06 and
   re-vote. Rotation is what restores bin equality for a pitch-shifted
   recording (the DJ vinyl use-case).

### Verification, and the bug that forced it

Hash voting alone has a statistical hole that showed up in the benchmark:
on a small dense library (a handful of tracks, hundreds of peaks per
frame) the same `(f1, f2, dt)` neighborhoods recur across tracks, so a
pink-noise clip collects thousands of "votes" and even a `winner /
second >= 1.5` gap gate lets one through (the false positive in the first
benchmark run).

So every candidate passes `verify_match`: take the track's stored peaks
and count what fraction of the query's peaks align with them at the
voted offset - same frame (probing +/-1 frame for the vote bin's rounding
error) and bin within +/-2. Real matches score 0.69-0.98; the worst noise
measured ~0.4 on the densest track. The threshold is contextual: a clear
vote lead (>= 2x the runner-up) needs only 0.38 - a 10x-vote leader
scoring 0.42 is a match; a near-tie needs 0.55.

Two historical traps, both measured and fixed in the benchmark:

- a multi-factor, +/-6% tolerance sweep in verification made noise
  "verify" at 0.6-0.77 (five factors times a 12% window is a big slice of
  the bin space); verification is strict (factor 1.0, +/-2 bins) because
  pass B has already normalized the pitch.
- a +/-5-frame projection window (~200 candidate bins on dense tracks)
  was also too generous; the frame probe is +/-1.

## Measured behavior

From `scripts/benchmark.py` against the 37 real tracks in `test_tracks/`
(details in the README table): 100% identity at 6 s and 12 s clips in pair
mode, 99.1-100% in quad; 100% under +18 dB gain and 96 kbps re-encode;
silence and pink noise rejected. Median lookup 184-272 ms (pair) and
34-44 ms (quad).

The known weakness: **pitch shifts of +/-3-6% score 0/15 in both modes**.
Exact hash equality breaks the moment the resampler chain moves bins by a
couple of quantization steps, and on real audio the rotated rescue pass
does not produce enough verified structure. This is the same wall Shazam
hits (the WXDU accuracy study it anchors: 14-33% on 33-rpm vinyl). Two
documented remedies: toleranced quad-ratio matching (match each 6-bit
ratio within +/-2 steps - `find_matches_tolerant` in the store is the
building block) and the Phase 2 neural matcher (`docs/phase2-neural.md`,
Chang et al. 2021, Faiss/pgvector evaluation plan).

## Configuration

Every knob in `fingerprints/config.py`, environment-overridable:

| env var                                                                   | default                 | meaning                                                             |
| ------------------------------------------------------------------------- | ----------------------- | ------------------------------------------------------------------- |
| `SLEUTH_DB_URL`                                                           | `sqlite:///./sleuth.db` | `sqlite:///path` or `postgresql://user:pass@host/db`                |
| `SLEUTH_HASH_MODE`                                                        | `pair`                  | `pair` (Wang) or `quad` (Sonnleitner-Widmer lineage)                |
| `SLEUTH_SR` / `SLEUTH_FFT_SIZE` / `SLEUTH_HOP`                            | 16000 / 4096 / 1024     | decode rate, FFT window, hop                                        |
| `SLEUTH_MIN_BIN` / `SLEUTH_MAX_BIN`                                       | 15 / 1800               | analysed frequency range                                            |
| `SLEUTH_PEAK_RADIUS` / `SLEUTH_PEAKS_PER_FRAME` / `SLEUTH_MIN_PEAK_RATIO` | 4 / 4 / 0.15            | peak picking                                                        |
| `SLEUTH_FANOUT` / `SLEUTH_MAX_DT`                                         | 12 / 2047               | pair combinatorics                                                  |
| `SLEUTH_QUAD_WINDOW` / `SLEUTH_QUAD_GAP`                                  | 16 / 2                  | quad geometry                                                       |
| `SLEUTH_MIN_VOTES` / `SLEUTH_MIN_GAP` / `SLEUTH_VOTE_BIN`                 | 20 / 1.5 / 3            | voting                                                              |
| `SLEUTH_DB_URL` note                                                      |                         | changing `HASH_MODE` on an existing DB requires `--force` re-ingest |

`HASH_MODE` is a database-wide decision (pair and quad rows coexist in the
same SQLite table, keyed differently), so pick it before the big ingest.

## Sources

- A. Wang, _An Industrial-Strength Audio Search Algorithm_, ISMIR 2003 -
  pair hashing, offset voting, the entire matching architecture.
- A. Sonnleitner, G. Widmer, _Quad-Based Audio Fingerprinting_, TASLP 2016
  - ratio/fraction quads, pitch and tempo invariance.
- J. Chang et al., _Contrastive Learning of Musical Representations for
  Audio Fingerprinting_, ICASSP 2021 - the Phase 2 plan
  (`docs/phase2-neural.md`).
- Open-source references surveyed: dejavu (Wang implementation),
  soundfingerprinting (.NET), chromaprint (web-service matching),
  audfprint, musig.
- The gpt-6.1-sol design review informed the storage layer: PG
  partitioning strategy, COPY batching, per-track transactional ingest,
  and the recommendation to keep ingest and the DJ webUI workloads
  operationally separate.
