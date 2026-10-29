"""GraphQL API for the local fingerprint matcher (Strawberry + FastAPI).

Schema surface:

    query {
        tracks(search: String, first: Int, offset: Int): [Track!]!
        track(id: ID!): Track
        stats: FingerprintStats!
        identifyMatch(audio: Upload!): [TrackMatch!]!   # match a recording
    }
    mutation {
        addTrack(audio: Upload!, artist: String, title: String, album: String): Track!
        removeTrack(id: ID!): Boolean!
    }

Uploads use the GraphQL multipart request spec (strawberry's Upload scalar).
"""

import tempfile
from pathlib import Path

import strawberry
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from strawberry.fastapi import GraphQLRouter
from strawberry.file_uploads import Upload

from . import config as C
from .matcher import match
from .pipeline import ingest_file
from .store import open_store


def _store():
    return open_store(C.DB_URL)


@strawberry.type
class Track:
    id: strawberry.ID
    artist: str
    title: str
    album: str
    duration_seconds: float
    fingerprint_count: int


@strawberry.type
class TrackMatch:
    track: Track
    votes: int
    confidence: float
    offset_seconds: float
    verify_score: float


@strawberry.type
class FingerprintStats:
    tracks: int
    fingerprint_rows: int


def _to_track(d):
    return Track(
        id=strawberry.ID(str(d["id"])),
        artist=d["artist"],
        title=d["title"],
        album=d["album"],
        duration_seconds=float(d.get("duration_seconds") or 0),
        fingerprint_count=int(d.get("fingerprint_count") or 0),
    )


def _save_upload(upload, suffix=""):
    """Persist an Upload to a temp file; caller removes it."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    with open(fd, "wb") as f:
        f.write(upload.file.read())
    return path


@strawberry.type
class Query:
    @strawberry.field
    def tracks(self, search: str = "", first: int = 50, offset: int = 0) -> list[Track]:
        store = _store()
        rows = store.search_tracks(search, limit=min(first, 200), offset=offset)
        return [_to_track(r) for r in rows]

    @strawberry.field
    def track(self, id: strawberry.ID) -> Track | None:
        store = _store()
        row = store.get_track(int(id))
        return _to_track(row) if row else None

    @strawberry.field
    def stats(self) -> FingerprintStats:
        s = _store().stats()
        return FingerprintStats(tracks=s["tracks"], fingerprint_rows=s["fingerprint_rows"])

    @strawberry.field
    def identifyMatch(self, audio: Upload) -> list[TrackMatch]:
        path = _save_upload(audio, suffix=Path(audio.filename or "").suffix)
        try:
            from .pipeline import fingerprint_track

            _, peaks, hashes = fingerprint_track(path)
            if not hashes:
                return []
            return [
                TrackMatch(track=_to_track(m.track), votes=m.votes,
                           confidence=m.confidence, offset_seconds=m.offset_seconds,
                           verify_score=m.verify_score or 0.0)
                for m in match(_store(), hashes, query_peaks=peaks)
            ]
        finally:
            Path(path).unlink(missing_ok=True)


@strawberry.type
class Mutation:
    @strawberry.mutation
    def addTrack(
        self,
        audio: Upload,
        artist: str = "",
        title: str = "",
        album: str = "",
    ) -> Track:
        path = _save_upload(audio, suffix=Path(audio.filename or "").suffix)
        try:
            result = ingest_file(
                _store(), path,
                artist=artist or None, title=title or None, album=album or None,
            )
            if result is None:
                raise ValueError("track already ingested")
            track_id, _ = result
            return _to_track(_store().get_track(track_id))
        finally:
            Path(path).unlink(missing_ok=True)

    @strawberry.mutation
    def removeTrack(self, id: strawberry.ID) -> bool:
        _store().delete_track(int(id))
        return True


schema = strawberry.Schema(query=Query, mutation=Mutation)


def make_app() -> FastAPI:
    app = FastAPI(title="stream-sleuth fingerprint service")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # dev default; tighten for the station network
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(
        GraphQLRouter(schema, path="/graphql", multipart_uploads_enabled=True)
    )
    return app


app = make_app()
