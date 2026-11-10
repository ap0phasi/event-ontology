"""BlobStore protocol and the local filesystem implementation, plus
attach() for writing evidence rows."""

from __future__ import annotations

import hashlib
import mimetypes
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Protocol

import duckdb

from .model import (
    ValidationError,
    compute_evidence_id,
    parse_any_timestamp,
    utcnow,
)
from .store import default_actor, event_exists

CHUNK_SIZE = 1024 * 1024


class BlobStore(Protocol):
    def put(self, stream: BinaryIO) -> tuple[str, int]:
        """Stream bytes in, return (sha256 hex, size). Idempotent."""

    def open(self, hash_: str) -> BinaryIO:
        ...

    def exists(self, hash_: str) -> bool:
        ...


def sha256_stream(stream: BinaryIO) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(CHUNK_SIZE)
        if not chunk:
            break
        h.update(chunk)
        size += len(chunk)
    return h.hexdigest(), size


class LocalBlobStore:
    """Stores blob bytes under <root>/<hash[:2]>/<hash>."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path_for(self, hash_: str) -> Path:
        return self.root / hash_[:2] / hash_

    def put(self, stream: BinaryIO) -> tuple[str, int]:
        # Buffer to a temp file while hashing, then move into place keyed
        # by hash (so re-putting identical bytes is a no-op).
        import tempfile

        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=self.root, delete=False
        ) as tmp:
            h = hashlib.sha256()
            size = 0
            while True:
                chunk = stream.read(CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
                size += len(chunk)
                tmp.write(chunk)
            tmp_path = Path(tmp.name)
        digest = h.hexdigest()
        dest = self._path_for(digest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            tmp_path.unlink(missing_ok=True)
        else:
            shutil.move(str(tmp_path), str(dest))
        return digest, size

    def open(self, hash_: str) -> BinaryIO:
        return open(self._path_for(hash_), "rb")

    def exists(self, hash_: str) -> bool:
        return self._path_for(hash_).exists()

    def location_uri(self, hash_: str) -> str:
        rel = f"{hash_[:2]}/{hash_}"
        return f"local:{rel}"


def blob_store_for_db(db_path: Path) -> LocalBlobStore:
    ledger_dir = Path(db_path).parent
    return LocalBlobStore(ledger_dir / "blobs")


# --- Catalogue / evidence writes ----------------------------------------------


def blob_exists_in_catalog(con: duckdb.DuckDBPyConnection, hash_: str) -> bool:
    return con.execute(
        "SELECT 1 FROM blobs WHERE hash = ?", [hash_]
    ).fetchone() is not None


def register_blob(
    con: duckdb.DuckDBPyConnection,
    hash_: str,
    media_type: str,
    size: int,
    location_uri: str,
    now: datetime | None = None,
) -> None:
    now = now or utcnow()
    if not blob_exists_in_catalog(con, hash_):
        con.execute(
            "INSERT INTO blobs (hash, media_type, size, recorded_at) VALUES (?, ?, ?, ?)",
            [hash_, media_type, size, now],
        )
    existing_loc = con.execute(
        "SELECT 1 FROM blob_locations WHERE hash = ? AND uri = ?",
        [hash_, location_uri],
    ).fetchone()
    if existing_loc is None:
        con.execute(
            "INSERT INTO blob_locations (hash, uri, recorded_at) VALUES (?, ?, ?)",
            [hash_, location_uri, now],
        )


def relocate_blob(
    con: duckdb.DuckDBPyConnection,
    hash_: str,
    uri: str,
    now: datetime | None = None,
) -> None:
    if not blob_exists_in_catalog(con, hash_):
        raise ValidationError(f"blob {hash_[:8]} is not in the catalogue")
    now = now or utcnow()
    con.execute(
        "INSERT INTO blob_locations (hash, uri, recorded_at) VALUES (?, ?, ?)",
        [hash_, uri, now],
    )


def attach_file(
    con: duckdb.DuckDBPyConnection,
    blob_store: LocalBlobStore,
    event_id: str,
    file_path: Path,
    note: str = "",
    actor: str | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Reads file_path, stores bytes, appends catalogue+evidence rows.
    Returns (blob_hash, evidence_id)."""
    file_path = Path(file_path)
    if not file_path.exists():
        raise ValidationError(f"file not found: {file_path}")
    now = now or utcnow()
    actor = actor or default_actor()

    if not event_exists(con, event_id):
        raise ValidationError(f"event {event_id[:8]} does not exist")

    with open(file_path, "rb") as fh:
        hash_, size = blob_store.put(fh)

    media_type, _enc = mimetypes.guess_type(str(file_path))
    if media_type is None:
        media_type = "application/octet-stream"

    location_uri = blob_store.location_uri(hash_)
    register_blob(con, hash_, media_type, size, location_uri, now=now)

    return attach_evidence_row(
        con, event_id=event_id, blob_hash=hash_, note=note, actor=actor, now=now
    )


def attach_evidence_row(
    con: duckdb.DuckDBPyConnection,
    *,
    event_id: str,
    blob_hash: str,
    note: str = "",
    actor: str | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Appends one evidence row. Validates event exists and blob exists
    in catalogue. Returns (blob_hash, evidence_id)."""
    now = now or utcnow()
    actor = actor or default_actor()

    if not event_exists(con, event_id):
        raise ValidationError(f"event {event_id[:8]} does not exist")
    if not blob_exists_in_catalog(con, blob_hash):
        raise ValidationError(f"blob {blob_hash[:8]} is not in the catalogue")

    event_recorded_at = con.execute(
        "SELECT recorded_at FROM events WHERE id = ?", [event_id]
    ).fetchone()[0]
    event_recorded_at = parse_any_timestamp(event_recorded_at)
    recorded_at = max(now, event_recorded_at)

    evidence_id = compute_evidence_id(
        actor=actor,
        blob_hash=blob_hash,
        event_id=event_id,
        note=note,
        recorded_at=recorded_at,
    )

    existing = con.execute(
        "SELECT id FROM evidence WHERE id = ?", [evidence_id]
    ).fetchone()
    if existing is not None:
        return blob_hash, evidence_id

    con.execute(
        "INSERT INTO evidence (id, event_id, blob_hash, note, actor, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [evidence_id, event_id, blob_hash, note, actor, recorded_at],
    )
    return blob_hash, evidence_id


def list_evidence_for_event(
    con: duckdb.DuckDBPyConnection, event_id: str
) -> list[dict]:
    rows = con.execute(
        """
        SELECT ev.id, ev.event_id, ev.blob_hash, b.size, b.media_type,
               ev.note, ev.actor, ev.recorded_at, ev.seq
        FROM evidence ev
        JOIN blobs b ON b.hash = ev.blob_hash
        WHERE ev.event_id = ?
        ORDER BY ev.seq
        """,
        [event_id],
    ).fetchall()
    results = []
    for (
        id_,
        event_id_,
        blob_hash,
        size,
        media_type,
        note,
        actor,
        recorded_at,
        seq,
    ) in rows:
        loc_row = con.execute(
            "SELECT uri FROM blob_locations WHERE hash = ? ORDER BY recorded_at DESC LIMIT 1",
            [blob_hash],
        ).fetchone()
        location = loc_row[0] if loc_row else None
        results.append(
            {
                "id": id_,
                "event_id": event_id_,
                "blob_hash": blob_hash,
                "size": size,
                "media_type": media_type,
                "note": note,
                "actor": actor,
                "recorded_at": recorded_at,
                "seq": seq,
                "location": location,
            }
        )
    return results
