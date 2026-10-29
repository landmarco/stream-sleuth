# stream-sleuth

Continuously identifies what's playing on the WXDU live stream using Shazam, and
feeds the results to the playlist manager so live DJs get one-click "now playing
on the stream" suggestions.

Runs on the WXDU iMac (modern macOS). Every ~25s it samples a few seconds of the
stream, recognizes the track with [`shazamio`](https://github.com/shazamio/ShazamIO),
and — when the song changes — POSTs it to the wxdu API, which stores it in
`plmanager.shazamplaying`. Adrenalin's playlist entry page reads the 5 most
recent rows and renders them as soft-blue buttons.

```
recognizer.py  ──HTTPS POST /api/shazam (X-Ingest-Secret)──▶  wxdu API  ──▶  MySQL
                                                                              │
                                    adrenalin playlist page  ◀──SELECT last 5─┘
```

## Setup (macOS)

```bash
# 1. ffmpeg (used to capture the stream)
brew install ffmpeg

# 2. python deps in a venv
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 3. config
cp .env.example .env
#   - set WXDU_SHAZAM_SECRET to the SAME value as the API's SHAZAM_INGEST_SECRET
#     (generate one with: openssl rand -hex 32)

# 4. test by hand
./run.sh
```

You should see lines like:

```
stream-sleuth: sampling https://stream.wxdu.art/wxdu192.mp3 (hit: 6s cap / 23s pause, gap: 12s cap / 4s pause) -> https://api.wxdu.art/api/shazam
[14:03:21] posted (201): Björk - Hunter
```

`no match` cycles are normal (talk breaks, obscure/local releases not in Shazam's
database) — the tool just tries again next interval.

## Run it as a background service (launchd)

```bash
# edit com.wxdu.stream-sleuth.plist: set the two /PATH/TO paths and the secret
cp com.wxdu.stream-sleuth.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.wxdu.stream-sleuth.plist

# logs:
tail -f /tmp/stream-sleuth.out.log /tmp/stream-sleuth.err.log

# stop / reload:
launchctl unload ~/Library/LaunchAgents/com.wxdu.stream-sleuth.plist
```

`KeepAlive` restarts it if it ever exits; `RunAtLoad` starts it at login.

## Config

All via environment (see `.env.example`): stream URL, API URL, shared secret,
poll interval, capture length. The tool posts a track only when it _changes_, so
the DB stays a clean log of distinct songs rather than a duplicate every cycle.

## Notes

- Only ASCII/UTF-8 metadata is sent; the API stores it in a `utf8mb4` table.
- The shared secret is the only auth on the ingest endpoint — keep `.env` and the
  plist readable only by the service account. Optionally also restrict the API's
  `/api/shazam` route to this iMac's static IP at the Apache/nginx layer.

## Local fingerprinting ("Shazam for your own library")

The `fingerprints/` package identifies recordings against your own music
library without leaving the house: spectral-peak hashing (Wang 2003, the
Shazam paper family) stored per track, matched by offset-consistent hash
voting (dejavu lineage, MIT). A quad-hashing mode (Sonnleitner & Widmer 2016)
is implemented experimentally - see the benchmark for the density
trade-off. The neural matcher plan (Chang et al. 2021) lives in
`docs/phase2-neural.md`. The full design record - signal chain, hashing
schemes, database layout, matching+verification logic, config knobs, and
the papers behind each part - is in [`docs/algorithm.md`](docs/algorithm.md).

## Create the matching database and identify against it

### 1. Install

```bash
pip install -r requirements.txt   # numpy, mutagen, fastapi, strawberry, psycopg
# ffmpeg must be on PATH: it decodes every container to mono PCM
```

### 2. Pick a backend

Default is a single SQLite file (`SLEUTH_DB_URL=sqlite:///./sleuth.db`),
which is fine for evaluation and small libraries. Production uses Postgres;
the schema and partitions are created automatically on first open:

```bash
export SLEUTH_DB_URL=postgresql://sleuth:sekret@beachyhead/sleuth
```

### 3. Ingest a library

```bash
python -m fingerprints.cli ingest --library ~/Music
#   ingested 37 new, 0 already present, 0 failed in 84.0s (0.44 tracks/s)
#   tracks: 37  fingerprint rows: 3,988,255
```

Walks nested directories for `.mp3`/`.wav`/`.m4a`/`.flac`/`.ogg`. Metadata
tags come from mutagen with a filename fallback for untagged rips. Ingest
is **resumable**: each track lands in one transaction (with a row in
`ingest_log`), so re-running the same command skips what already finished
and picks up where a crash left off. `--force` re-fingerprints everything,
`--limit N` and `--quiet` help on the first big run.

Quad mode (`SLEUTH_HASH_MODE=quad`) stores ~15x fewer rows and matches
3-4x faster, at the cost of the measured pitch weakness - pick the hash
mode before the big ingest; switching later requires `--force` (see
`docs/algorithm.md` for the trade-offs).

### 4. Match a recording against the DB

```bash
python -m fingerprints.cli identify clip.wav
#   Flesh Lights - I'm A Monk  (votes 2016, verify 0.74, +60.0s into track, 184ms)
```

A clip of 8-12 s works well; `--top N` shows alternatives. The query goes
through the same signal chain as ingest, so any codec ffmpeg reads works.

### 5. Serve it over GraphQL

```bash
python -m fingerprints.cli serve --host 0.0.0.0 --port 8000
```

```bash
# everything: stats + search + identify (multipart upload per strawberry spec)
curl http://localhost:8000/graphql -H 'Content-Type: application/json' \
  -d '{"query":"{ stats { tracks fingerprintRows } }"}'
curl http://localhost:8000/graphql -H 'Content-Type: application/json' \
  -d '{"query":"{ tracks(search: \"monk\") { id title artist } }"}'
curl http://localhost:8000/graphql \
  -F 'operations={"query":"query($f: Upload!){ identifyMatch(audio: $f){ track { title } votes confidence offsetSeconds verifyScore } }","variables":{"f":null}};type=application/json' \
  -F 'map={"0":["variables.f"]};type=application/json' \
  -F '0=@clip.wav;type=audio/wav'

# mutations: addTrack(audio, artist?, title?, album?) / removeTrack(id)
```

### 6. Evaluate

```bash
python scripts/benchmark.py   # probes ./test_tracks; writes results to friendly-harness/artifacts
```

What the benchmark measures is in the table below - including the honest
pitch-shift gap and the plan for it.

### Benchmarked behavior (37 real tracks in test_tracks/)

| probe                | pair mode              | quad mode              |
| -------------------- | ---------------------- | ---------------------- |
| rows per track       | 107,791                | 6,968                  |
| 6s-clip identity     | 111/111 (100%, ~184ms) | 110/111 (99.1%, ~34ms) |
| 12s-clip identity    | 111/111 (100%, ~272ms) | 111/111 (100%, ~44ms)  |
| gain +18dB           | 100%                   | 100%                   |
| 96kbps re-encode     | 100%                   | 100%                   |
| pitch shift +-3..6%  | 0%                     | 0%                     |
| silence / pink noise | rejected               | rejected               |

Matching is two-pass: exact hash voting + tight spectral alignment
(verification) at factor 1.0, then a pitch-rescue pass that re-hashes the
query under +-6% bin rotations and verifies candidates. Exact-hash systems
are pitch-fragile by design; the resampler chain smears bins just enough to
break hash equality, and on real audio the rescue doesn't clear the
verification gate - the same failure the WXDU study documents for Shazam
itself (14-33% ID on 33-rpm vinyl). The known fix is toleranced quad-ratio
matching (match each 6-bit ratio within +-2 steps; the store's
`find_matches_tolerant` is the building block) or the Phase-2 neural
matcher (docs/phase2-neural.md), which is also the plan for the 400k-track
library: pair hashes project to ~43B rows there, quads to ~2.8B.
