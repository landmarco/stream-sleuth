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
stream-sleuth: sampling https://stream.wxdu.art/wxdu192.mp3 every 25s -> https://wxdu.art/api/shazam
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
poll interval, capture length. The tool posts a track only when it *changes*, so
the DB stays a clean log of distinct songs rather than a duplicate every cycle.

## Notes

- Only ASCII/UTF-8 metadata is sent; the API stores it in a `utf8mb4` table.
- The shared secret is the only auth on the ingest endpoint — keep `.env` and the
  plist readable only by the service account. Optionally also restrict the API's
  `/api/shazam` route to this iMac's static IP at the Apache/nginx layer.
