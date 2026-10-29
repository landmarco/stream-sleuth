"""stream-sleuth fingerprint CLI.

    python -m fingerprints.cli ingest --library <dir> [--force] [--limit N]
    python -m fingerprints.cli identify <file>
    python -m fingerprints.cli serve [--port N]
    python -m fingerprints.cli stats

All fingerprinting knobs live in environment (see fingerprints/config.py and
the .env.example). SLEUTH_DB_URL selects the store: sqlite:///./sleuth.db
(default, dev) or postgresql://user:pass@host:5432/db (production).
"""

import argparse
import json
import sys
import time

from . import config as C
from .matcher import match
from .pipeline import fingerprint_track, ingest_library
from .store import open_store

DESC = "local audio fingerprinting for stream-sleuth"


def cmd_ingest(args):
    store = open_store(args.url)
    try:
        t0 = time.time()
        res = ingest_library(
            store, args.library, force=args.force, limit=args.limit, quiet=args.quiet,
        )
        rate = res["done"] / res["elapsed_s"] if res["elapsed_s"] else 0
        print(
            f"ingested {res['done']} new, {res['skipped']} already present, "
            f"{res['failed']} failed in {res['elapsed_s']:.1f}s "
            f"({rate:.2f} tracks/s)"
        )
        s = store.stats()
        print(f"tracks: {s['tracks']}  fingerprint rows: {s['fingerprint_rows']:,}")
    finally:
        store.close()


def cmd_identify(args):
    store = open_store(args.url)
    try:
        _, peaks, hashes = fingerprint_track(args.file)
        if not hashes:
            print("no peaks found - silence or broken decode?", file=sys.stderr)
            return 2
        t0 = time.time()
        matches = match(store, hashes, top_k=args.top, query_peaks=peaks)
        elapsed = (time.time() - t0) * 1000
        for m in matches:
            print(
                f"{m.track['artist']} - {m.track['title']}"
                f"  (votes {m.votes}, verify {m.verify_score:.2f}, "
                f"+{m.offset_seconds:.1f}s into track, {elapsed:.0f}ms)"
            )
        if not matches:
            print("no match", file=sys.stderr)
            return 1
    finally:
        store.close()


def cmd_serve(args):
    import uvicorn

    from .graphql_api import app

    uvicorn.run(app, host=args.host, port=args.port)


def cmd_stats(args):
    store = open_store(args.url)
    try:
        s = store.stats()
        print(json.dumps(s, indent=2, default=str))
    finally:
        store.close()


def main(argv=None):
    p = argparse.ArgumentParser(description=DESC)
    p.add_argument("--url", default=None, help="SLEUTH_DB_URL override")
    sub = p.add_subparsers(dest="cmd", required=True)

    gi = sub.add_parser("ingest", help="fingerprint a music library (nested dirs)")
    gi.add_argument("--library", required=True, type=str)
    gi.add_argument("--force", action="store_true", help="re-fingerprint already-ingested files")
    gi.add_argument("--limit", type=int, default=None)
    gi.add_argument("--quiet", action="store_true")
    gi.set_defaults(fn=cmd_ingest)

    idf = sub.add_parser("identify", help="match one audio file against the DB")
    idf.add_argument("file", type=str)
    idf.add_argument("--top", type=int, default=5)
    idf.set_defaults(fn=cmd_identify)

    sv = sub.add_parser("serve", help="run the GraphQL service")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(fn=cmd_serve)

    st = sub.add_parser("stats", help="DB contents")
    st.set_defaults(fn=cmd_stats)

    args = p.parse_args(argv)
    if args.url is None:
        args.url = C.DB_URL
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
