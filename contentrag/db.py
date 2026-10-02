"""SQLite index: one file holds every media item, moment, and transcript."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id           TEXT PRIMARY KEY,          -- content fingerprint, stable across moves
    kind         TEXT NOT NULL,             -- video | photo
    root         TEXT NOT NULL,             -- config root name of the primary copy
    relpath      TEXT NOT NULL,             -- path inside the root
    root_kind    TEXT NOT NULL,             -- archive | brand
    size         INTEGER,
    duration     REAL,
    width        INTEGER,
    height       INTEGER,
    fps          REAL,
    orientation  TEXT,                      -- vertical | horizontal | square
    has_audio    INTEGER DEFAULT 0,
    taken_at     TEXT,                      -- local ISO datetime
    date_source  TEXT,                      -- metadata | filename | mtime
    lat          REAL,
    lon          REAL,
    place        TEXT,
    camera       TEXT,
    title        TEXT,                      -- from Claude
    summary      TEXT,                      -- from Claude
    tags         TEXT,                      -- JSON list, from Claude
    prepped      INTEGER DEFAULT 0,
    transcribed  INTEGER DEFAULT 0,
    described    INTEGER DEFAULT 0,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS media_taken ON media(taken_at);

CREATE TABLE IF NOT EXISTS locations (      -- every copy of a file (duplicates)
    root    TEXT NOT NULL,
    relpath TEXT NOT NULL,
    media_id TEXT NOT NULL REFERENCES media(id),
    PRIMARY KEY (root, relpath)
);

CREATE TABLE IF NOT EXISTS frames (
    media_id TEXT NOT NULL,
    t        REAL NOT NULL,                 -- seconds
    path     TEXT NOT NULL,                 -- relative to library_dir
    PRIMARY KEY (media_id, t)
);

CREATE TABLE IF NOT EXISTS cuts (           -- scene-change timestamps
    media_id TEXT NOT NULL,
    t        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS transcript (
    media_id TEXT NOT NULL,
    start    REAL NOT NULL,
    end      REAL NOT NULL,
    text     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS transcript_media ON transcript(media_id);

CREATE TABLE IF NOT EXISTS moments (
    id           INTEGER PRIMARY KEY,
    media_id     TEXT NOT NULL,
    start        REAL NOT NULL,
    end          REAL NOT NULL,
    description  TEXT,
    action       TEXT,
    setting      TEXT,
    shot_type    TEXT,
    camera_motion TEXT,
    people_count INTEGER,
    mood         TEXT,
    energy       TEXT,
    quality_issues TEXT,                    -- JSON list
    broll_score  INTEGER,                   -- 1..5
    content_uses TEXT,                      -- JSON list
    tags         TEXT,                      -- JSON list
    speech_en    TEXT
);
CREATE INDEX IF NOT EXISTS moments_media ON moments(media_id);

CREATE VIRTUAL TABLE IF NOT EXISTS moments_fts USING fts5(
    description, action, setting, tags, content_uses, speech, place, title,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS requests (       -- one Claude request = one video window or photo group
    custom_id  TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,               -- video | photos
    payload    TEXT NOT NULL,               -- JSON: media ids / window
    batch_id   TEXT,
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending|submitted|done|refused|error
    model      TEXT,
    error      TEXT,
    in_tokens  INTEGER,
    out_tokens INTEGER
);
"""


def connect(path: Path, threads: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=not threads, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(SCHEMA)
    _add_columns(conn, "requests", {"in_tokens": "INTEGER", "out_tokens": "INTEGER",
                                    "attempts": "INTEGER DEFAULT 0"})
    _add_columns(conn, "media", {"attempts": "INTEGER DEFAULT 0", "skip_reason": "TEXT"})
    conn.commit()
    return conn


# Errors are retried this many times (across runs) before an item is given up on.
MAX_ATTEMPTS = 3


def _add_columns(conn, table: str, columns: dict[str, str]) -> None:
    """Columns added after the first release; existing databases get them on open."""
    have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for col, decl in columns.items():
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def jloads(value: str | None, default=None):
    if not value:
        return [] if default is None else default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return [] if default is None else default


def reindex_fts(conn: sqlite3.Connection, media_id: str | None = None) -> None:
    """Rebuild full-text rows for one media item (or all)."""
    where, args = ("WHERE m.media_id = ?", (media_id,)) if media_id else ("", ())
    if media_id:
        conn.execute(
            "DELETE FROM moments_fts WHERE rowid IN (SELECT id FROM moments WHERE media_id = ?)",
            (media_id,),
        )
    else:
        conn.execute("DELETE FROM moments_fts")
    rows = conn.execute(
        f"""SELECT m.*, md.place, md.title FROM moments m JOIN media md ON md.id = m.media_id {where}""",
        args,
    ).fetchall()
    conn.executemany(
        "INSERT INTO moments_fts(rowid, description, action, setting, tags, content_uses, speech, place, title)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                r["id"],
                r["description"] or "",
                r["action"] or "",
                r["setting"] or "",
                " ".join(jloads(r["tags"])),
                " ".join(jloads(r["content_uses"])),
                r["speech_en"] or "",
                r["place"] or "",
                r["title"] or "",
            )
            for r in rows
        ],
    )


def failures(conn, limit: int = 50) -> list[dict]:
    """Failed / blocked AI requests with the file they belong to and the reason."""
    out = []
    for r in conn.execute(
        "SELECT custom_id, kind, payload, status, error, attempts FROM requests WHERE status IN ('error','refused') "
        "ORDER BY custom_id LIMIT ?", (limit,)):
        payload = jloads(r["payload"], {})
        mid = payload.get("media_id") or (payload.get("media_ids") or [None])[0]
        m = conn.execute("SELECT relpath, duration, size FROM media WHERE id=?", (mid,)).fetchone() if mid else None
        out.append({
            "request": r["custom_id"], "error": r["error"] or "(no message recorded)",
            "status": "gave up" if r["status"] == "error" and (r["attempts"] or 0) >= MAX_ATTEMPTS else r["status"],
            "file": m["relpath"] if m else None,
            "duration_s": round(m["duration"] or 0) if m else None,
            "size_mb": round((m["size"] or 0) / 1e6) if m else None,
        })
    for m in conn.execute("SELECT relpath, error, attempts FROM media WHERE error IS NOT NULL LIMIT ?", (limit,)):
        out.append({"request": None, "status": "file gave up" if (m["attempts"] or 0) >= MAX_ATTEMPTS else "file",
                    "error": m["error"], "file": m["relpath"],
                    "duration_s": None, "size_mb": None})
    return out
