"""Storage adapters for tracks + fingerprint rows.

Two implementations share one interface:
- SqliteStore: local/dev/test, zero setup.
- PostgresStore: production on beachyhead.

Fingerprints live in two tables so each hash family gets the index shape it
needs:
- fingerprints_pair (f1, f2, dt, track_id, time_offset): tolerance-aware
  voting queries small +/-1-bin / +/-2-frame neighborhoods, so
  resampler-induced jitter (the thing that breaks exact-hash matching) is
  absorbed. Range-partitioned on f1 in Postgres so neighborhood queries
  prune to a few partitions.
- fingerprints_quad (hash, track_id, time_offset): exact equality lookups,
  hash-partitioned in Postgres.

Bulk loads go through COPY (Postgres) / executemany (SQLite) inside a
per-track transaction, so an interrupted ingest leaves full tracks, never
half tracks; the ingest_log table makes runs resumable.
"""

import hashlib
import os
import sqlite3

from . import config as C
from .hashgen import unpack_pair

PARTITIONS = 16
PAIR_PARTITIONS = 32
BATCH = 400  # SQL parameter budget per IN query (sqlite limits ~999)


class TrackMeta:
    def __init__(self, artist, title, album, source_path, file_md5, duration):
        self.artist = artist or ""
        self.title = title or ""
        self.album = album or ""
        self.source_path = source_path
        self.file_md5 = file_md5
        self.duration = duration

    def to_dict(self):
        return {
            "id": getattr(self, "id", None),
            "artist": self.artist,
            "title": self.title,
            "album": self.album,
            "source_path": self.source_path,
            "duration_seconds": self.duration,
        }


def md5_file(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


class SqliteStore:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path)
        # The tolerant IN-lists need ~8k parameters per query.
        self.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 100000)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")

    def close(self):
        self.conn.close()

    def init_schema(self):
        c = self.conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS tracks(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artist TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                album TEXT NOT NULL DEFAULT '',
                source_path TEXT NOT NULL UNIQUE,
                file_md5 TEXT NOT NULL,
                duration_seconds REAL NOT NULL DEFAULT 0,
                fingerprint_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS fingerprints(
                hash INTEGER NOT NULL,
                f1 INTEGER NOT NULL,
                f2 INTEGER NOT NULL,
                dt INTEGER NOT NULL,
                track_id INTEGER NOT NULL,
                time_offset INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_fp_hash
                ON fingerprints(hash, track_id, time_offset);
            CREATE INDEX IF NOT EXISTS idx_fp_triple
                ON fingerprints(f1, f2, dt);
            CREATE TABLE IF NOT EXISTS peaks(
                track_id INTEGER NOT NULL,
                time_offset INTEGER NOT NULL,
                freq_bin INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_peaks_track
                ON peaks(track_id, time_offset);
            CREATE TABLE IF NOT EXISTS ingest_log(
                path TEXT PRIMARY KEY,
                track_id INTEGER NOT NULL,
                fingerprinted_at TEXT NOT NULL DEFAULT (datetime('now')));
            """
        )
        self.conn.commit()

    def upsert_track(self, meta):
        c = self.conn
        cur = c.execute(
            "SELECT id, file_md5 FROM tracks WHERE source_path = ?", (meta.source_path,)
        )
        row = cur.fetchone()
        if row and row[1] == meta.file_md5:
            c.execute(
                "UPDATE tracks SET artist=?, title=?, album=?, duration_seconds=? WHERE id=?",
                (meta.artist, meta.title, meta.album, meta.duration, row[0]),
            )
            self.conn.commit()
            return row[0]
        if row:
            c.execute("DELETE FROM fingerprints WHERE track_id = ?", (row[0],))
            c.execute(
                "UPDATE tracks SET artist=?, title=?, album=?, file_md5=?, duration_seconds=? WHERE id=?",
                (meta.artist, meta.title, meta.album, meta.file_md5, meta.duration, row[0]),
            )
            track_id = row[0]
        else:
            cur = c.execute(
                "INSERT INTO tracks(artist, title, album, source_path, file_md5, duration_seconds)"
                " VALUES(?, ?, ?, ?, ?, ?)",
                (meta.artist, meta.title, meta.album, meta.source_path, meta.file_md5, meta.duration),
            )
            track_id = cur.lastrowid
        self.conn.commit()
        return track_id

    def write_fingerprints(self, track_id, hashes, peaks=None):
        c = self.conn
        c.execute("DELETE FROM fingerprints WHERE track_id = ?", (track_id,))
        data = []
        for h, off in hashes:
            f1, f2, dt = unpack_pair(h)
            data.append((h, f1, f2, dt, track_id, off))
        c.executemany(
            "INSERT INTO fingerprints(hash, f1, f2, dt, track_id, time_offset)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            data,
        )
        if peaks is not None:
            c.execute("DELETE FROM peaks WHERE track_id = ?", (track_id,))
            c.executemany(
                "INSERT INTO peaks(track_id, time_offset, freq_bin) VALUES(?, ?, ?)",
                [(track_id, t, b) for t, b in peaks],
            )
        c.execute(
            "UPDATE tracks SET fingerprint_count = ? WHERE id = ?", (len(data), track_id)
        )
        self.conn.commit()

    def delete_track(self, track_id):
        c = self.conn
        c.execute("DELETE FROM fingerprints WHERE track_id = ?", (track_id,))
        c.execute("DELETE FROM peaks WHERE track_id = ?", (track_id,))
        c.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        c.execute("DELETE FROM ingest_log WHERE track_id = ?", (track_id,))
        self.conn.commit()

    def mark_ingested(self, path, track_id):
        self.conn.execute(
            "INSERT OR REPLACE INTO ingest_log(path, track_id) VALUES(?, ?)",
            (path, track_id),
        )
        self.conn.commit()

    def is_ingested(self, path):
        return self.conn.execute(
            "SELECT 1 FROM ingest_log WHERE path = ?", (path,)
        ).fetchone() is not None

    def find_matches(self, hashes):
        """Exact: [(hash, track_id, time_offset)] for the given query hashes."""
        out = []
        c = self.conn
        for i in range(0, len(hashes), BATCH):
            chunk = hashes[i : i + BATCH]
            marks = ",".join("?" for _ in chunk)
            rows = c.execute(
                f"SELECT hash, track_id, time_offset FROM fingerprints WHERE hash IN ({marks})",
                chunk,
            ).fetchall()
            out.extend(rows)
        return out

    def find_matches_tolerant(self, keys):
        """Neighborhood: [(key_idx, hash, track_id, time_offset)] for keys.

        Each key expands to the 45 combos of f1 +/- 1, f2 +/- 1, dt +/- 2.
        Combos are deduped within a batch and mapped back to key indices in
        Python (no per-combo columns in the SELECT - they would multiply
        every returned row by the batch size).
        """
        out = []
        c = self.conn
        for i in range(0, len(keys), BATCH // 9):
            batch = keys[i : i + BATCH // 9]
            combo_of = {}
            combos = []
            for k, (f1, f2, dt) in enumerate(batch):
                for a in range(max(0, f1 - 1), f1 + 2):
                    for b in range(max(0, f2 - 1), f2 + 2):
                        for d in range(max(0, dt - 2), dt + 3):
                            tup = (a, b, d)
                            if tup not in combo_of:
                                combo_of[tup] = k
                                combos.append(tup)
            marks = ",".join("(?, ?, ?)" for _ in combos)
            params = [v for t in combos for v in t]
            rows = c.execute(
                f"SELECT f1, f2, dt, hash, track_id, time_offset"
                f" FROM fingerprints WHERE (f1, f2, dt) IN ({marks})",
                params,
            ).fetchall()
            out.extend(
                (i + combo_of[(r[0], r[1], r[2])], r[3], r[4], r[5]) for r in rows
            )
        return out

    def get_track(self, track_id):
        row = self.conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        return dict(zip([d[0] for d in self.conn.execute("SELECT * FROM tracks LIMIT 0").description], row)) if row else None

    def search_tracks(self, q, limit=50, offset=0):
        like = f"%{q}%"
        rows = self.conn.execute(
            "SELECT * FROM tracks WHERE artist LIKE ? OR title LIKE ? OR album LIKE ?"
            " ORDER BY artist, title LIMIT ? OFFSET ?",
            (like, like, like, limit, offset),
        ).fetchall()
        cols = [d[0] for d in self.conn.execute("SELECT * FROM tracks LIMIT 0").description]
        return [dict(zip(cols, r)) for r in rows]

    def get_peaks(self, track_id):
        return self.conn.execute(
            "SELECT time_offset, freq_bin FROM peaks WHERE track_id = ?"
            " ORDER BY time_offset",
            (track_id,),
        ).fetchall()

    def stats(self):
        tracks = self.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        rows = self.conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
        hashes = self.conn.execute("SELECT SUM(fingerprint_count) FROM tracks").fetchone()[0] or 0
        return {"tracks": tracks, "fingerprint_rows": rows, "stored_hashes": hashes,
                "db_bytes": os.path.getsize(self.path)}


class PostgresStore:
    def __init__(self, url):
        import psycopg

        self.url = url
        self.conn = psycopg.connect(url)

    def close(self):
        self.conn.close()

    def init_schema(self):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tracks(
                    id BIGSERIAL PRIMARY KEY,
                    artist TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    album TEXT NOT NULL DEFAULT '',
                    source_path TEXT NOT NULL UNIQUE,
                    file_md5 TEXT NOT NULL,
                    duration_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
                    fingerprint_count BIGINT NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now())
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingest_log(
                    path TEXT PRIMARY KEY,
                    track_id BIGINT NOT NULL,
                    fingerprinted_at TIMESTAMPTZ NOT NULL DEFAULT now())
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS peaks(
                    track_id BIGINT NOT NULL,
                    time_offset INTEGER NOT NULL,
                    freq_bin INTEGER NOT NULL)
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_peaks_track ON peaks(track_id, time_offset)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS fingerprints_pair(
                    f1 INTEGER NOT NULL,
                    f2 INTEGER NOT NULL,
                    dt INTEGER NOT NULL,
                    track_id BIGINT NOT NULL,
                    time_offset INTEGER NOT NULL)
                PARTITION BY RANGE (f1)
                """
            )
            for i in range(PAIR_PARTITIONS):
                lo = i * (65536 // PAIR_PARTITIONS)
                hi = (i + 1) * (65536 // PAIR_PARTITIONS) - 1
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS fingerprints_pair_p%d PARTITION OF"
                    " fingerprints_pair FOR VALUES FROM (%d) TO (%d)" % (i, lo, hi)
                )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_fp_triple ON fingerprints_pair"
                " (f1, f2, dt, track_id, time_offset)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS fingerprints_quad(
                    hash BIGINT NOT NULL,
                    track_id BIGINT NOT NULL,
                    time_offset INTEGER NOT NULL)
                PARTITION BY HASH (hash)
                """
            )
            for i in range(PARTITIONS):
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS fingerprints_quad_p%d PARTITION OF"
                    " fingerprints_quad FOR VALUES WITH (MODULUS %d, REMAINDER %d)"
                    % (i, PARTITIONS, i)
                )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_fp_hash ON fingerprints_quad"
                " (hash, track_id, time_offset)"
            )
        self.conn.commit()

    def upsert_track(self, meta):
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id, file_md5 FROM tracks WHERE source_path = %s", (meta.source_path,)
            )
            row = cur.fetchone()
            if row and row[1] == meta.file_md5:
                cur.execute(
                    "UPDATE tracks SET artist=%s, title=%s, album=%s, duration_seconds=%s,"
                    " updated_at=now() WHERE id=%s",
                    (meta.artist, meta.title, meta.album, meta.duration, row[0]),
                )
                self.conn.commit()
                return row[0]
            if row:
                cur.execute("DELETE FROM fingerprints_pair WHERE track_id = %s", (row[0],))
                cur.execute("DELETE FROM fingerprints_quad WHERE track_id = %s", (row[0],))
                cur.execute(
                    "UPDATE tracks SET artist=%s, title=%s, album=%s, file_md5=%s,"
                    " duration_seconds=%s, updated_at=now() WHERE id=%s",
                    (meta.artist, meta.title, meta.album, meta.file_md5, meta.duration, row[0]),
                )
                track_id = row[0]
            else:
                cur.execute(
                    "INSERT INTO tracks(artist, title, album, source_path, file_md5, duration_seconds)"
                    " VALUES(%s, %s, %s, %s, %s, %s) RETURNING id",
                    (meta.artist, meta.title, meta.album, meta.source_path, meta.file_md5, meta.duration),
                )
                track_id = cur.fetchone()[0]
        self.conn.commit()
        return track_id

    def write_fingerprints(self, track_id, hashes, peaks=None):
        with self.conn.cursor() as cur:
            if C.HASH_MODE == "quad":
                cur.execute("DELETE FROM fingerprints_quad WHERE track_id = %s", (track_id,))
                data = "".join(f"{h}\t{track_id}\t{off}\n" for h, off in hashes)
                with cur.copy(
                    "COPY fingerprints_quad(hash, track_id, time_offset) FROM STDIN"
                ) as cp:
                    cp.write(data.encode())
            else:
                cur.execute("DELETE FROM fingerprints_pair WHERE track_id = %s", (track_id,))
                data = "".join(
                    f"{h >> 48}\t{(h >> 32) & 0xFFFF}\t{h & 0xFFFFFFFF}\t{track_id}\t{off}\n"
                    for h, off in hashes
                )
                with cur.copy(
                    "COPY fingerprints_pair(f1, f2, dt, track_id, time_offset) FROM STDIN"
                ) as cp:
                    cp.write(data.encode())
            if peaks is not None:
                cur.execute("DELETE FROM peaks WHERE track_id = %s", (track_id,))
                pdata = "".join(f"{track_id}\t{t}\t{b}\n" for t, b in peaks)
                with cur.copy("COPY peaks(track_id, time_offset, freq_bin) FROM STDIN") as cp:
                    cp.write(pdata.encode())
            cur.execute(
                "UPDATE tracks SET fingerprint_count = %s, updated_at = now() WHERE id = %s",
                (len(hashes), track_id),
            )
        self.conn.commit()

    def delete_track(self, track_id):
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM fingerprints_pair WHERE track_id = %s", (track_id,))
            cur.execute("DELETE FROM fingerprints_quad WHERE track_id = %s", (track_id,))
            cur.execute("DELETE FROM peaks WHERE track_id = %s", (track_id,))
            cur.execute("DELETE FROM tracks WHERE id = %s", (track_id,))
            cur.execute("DELETE FROM ingest_log WHERE track_id = %s", (track_id,))
        self.conn.commit()

    def mark_ingested(self, path, track_id):
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ingest_log(path, track_id) VALUES(%s, %s)"
                " ON CONFLICT (path) DO UPDATE SET track_id = EXCLUDED.track_id",
                (path, track_id),
            )
        self.conn.commit()

    def is_ingested(self, path):
        with self.conn.cursor() as cur:
            cur.execute("SELECT 1 FROM ingest_log WHERE path = %s", (path,))
            return cur.fetchone() is not None

    def find_matches(self, hashes):
        out = []
        with self.conn.cursor() as cur:
            for i in range(0, len(hashes), BATCH):
                chunk = hashes[i : i + BATCH]
                marks = ",".join("%s" for _ in chunk)
                cur.execute(
                    f"SELECT hash, track_id, time_offset FROM fingerprints_quad"
                    f" WHERE hash IN ({marks})",
                    chunk,
                )
                out.extend(cur.fetchall())
        return out

    def find_matches_tolerant(self, keys):
        out = []
        with self.conn.cursor() as cur:
            for i in range(0, len(keys), BATCH // 9):
                batch = keys[i : i + BATCH // 9]
                combo_of = {}
                combos = []
                for k, (f1, f2, dt) in enumerate(batch):
                    for a in range(max(0, f1 - 1), f1 + 2):
                        for b in range(max(0, f2 - 1), f2 + 2):
                            for d in range(max(0, dt - 2), dt + 3):
                                tup = (a, b, d)
                                if tup not in combo_of:
                                    combo_of[tup] = k
                                    combos.append(tup)
                marks = ",".join("(%s, %s, %s)" for _ in combos)
                params = [v for t in combos for v in t]
                cur.execute(
                    f"SELECT f1, f2, dt, hash, track_id, time_offset"
                    f" FROM fingerprints_pair WHERE (f1, f2, dt) IN ({marks})",
                    params,
                )
                rows = cur.fetchall()
                out.extend(
                    (i + combo_of[(r[0], r[1], r[2])], r[3], r[4], r[5])
                    for r in rows
                )
        return out

    def get_track(self, track_id):
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM tracks WHERE id = %s", (track_id,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d.name for d in cur.description]
            return dict(zip(cols, row))

    def search_tracks(self, q, limit=50, offset=0):
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM tracks WHERE artist ILIKE %s OR title ILIKE %s OR album ILIKE %s"
                " ORDER BY artist, title LIMIT %s OFFSET %s",
                (f"%{q}%", f"%{q}%", f"%{q}%", limit, offset),
            )
            rows = cur.fetchall()
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]

    def get_peaks(self, track_id):
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT time_offset, freq_bin FROM peaks WHERE track_id = %s"
                " ORDER BY time_offset",
                (track_id,),
            )
            return cur.fetchall()

    def stats(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM tracks")
            tracks = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM fingerprints_pair")
            pair = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM fingerprints_quad")
            quad = cur.fetchone()[0]
            return {"tracks": tracks, "fingerprint_rows": pair + quad,
                    "pair_rows": pair, "quad_rows": quad}


def open_store(url=None):
    """Open the store named by SLEUTH_DB_URL (or given url), schema ensured."""
    url = url or C.DB_URL
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///") :]
        store = SqliteStore(path)
    elif url.startswith("postgresql://"):
        store = PostgresStore(url)
    else:
        raise ValueError(f"unsupported SLEUTH_DB_URL scheme: {url}")
    store.init_schema()
    return store
