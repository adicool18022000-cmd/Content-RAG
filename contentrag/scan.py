"""Step 1: find every photo/video on the mounted roots, dedupe, read metadata."""

from __future__ import annotations

import os
from pathlib import Path

from .config import Config
from .probe import fingerprint, media_kind, probe

SKIP_DIRS = {".Trashes", ".Spotlight-V100", ".fseventsd", ".TemporaryItems", "@eaDir", "$RECYCLE.BIN"}


def iter_media(root: Path, exclude: Path | None):
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        dirnames[:] = [
            n for n in dirnames
            if not n.startswith(".") and n not in SKIP_DIRS
            and not (exclude and (d / n).resolve() == exclude)
            and not n.endswith((".prproj", ".fcpbundle", ".photoslibrary"))  # app bundles
        ]
        for name in filenames:
            if name.startswith("."):  # includes macOS "._" AppleDouble files on exFAT
                continue
            p = d / name
            kind = media_kind(p)
            if kind:
                yield p, kind


def scan(cfg: Config, conn, log=print) -> dict:
    stats = {"new": 0, "duplicate": 0, "known": 0, "failed": 0, "skipped_roots": []}
    library = cfg.library_dir.resolve() if cfg.library_dir.exists() else None
    for root in cfg.roots:
        if not root.mounted:
            stats["skipped_roots"].append(root.name)
            log(f"[scan] {root.name}: not mounted at {root.path}, skipping")
            continue
        log(f"[scan] {root.name}: {root.path}")
        for path, kind in iter_media(root.path, library):
            rel = path.relative_to(root.path).as_posix()
            if conn.execute("SELECT 1 FROM locations WHERE root=? AND relpath=?", (root.name, rel)).fetchone():
                stats["known"] += 1
                continue
            try:
                fid = fingerprint(path)
                if conn.execute("SELECT 1 FROM media WHERE id=?", (fid,)).fetchone():
                    stats["duplicate"] += 1
                else:
                    meta = probe(path, kind, cfg.timezone)
                    if kind == "video" and not meta.duration:
                        raise ValueError("no video stream / zero duration")
                    conn.execute(
                        """INSERT INTO media (id, kind, root, relpath, root_kind, size, duration, width, height,
                           fps, orientation, has_audio, taken_at, date_source, lat, lon, camera)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (fid, kind, root.name, rel, root.kind, path.stat().st_size, meta.duration,
                         meta.width, meta.height, meta.fps, meta.orientation, int(meta.has_audio),
                         meta.taken_at, meta.date_source, meta.lat, meta.lon, meta.camera),
                    )
                    stats["new"] += 1
                conn.execute("INSERT OR IGNORE INTO locations VALUES (?,?,?)", (root.name, rel, fid))
            except Exception as e:  # corrupt files, permission errors
                stats["failed"] += 1
                log(f"[scan]   failed {rel}: {e}")
            if (stats["new"] + stats["duplicate"]) % 200 == 0:
                conn.commit()
        conn.commit()
    geocode(conn, log)
    return stats


def geocode(conn, log=print) -> None:
    """Offline reverse geocoding (city level) via the optional reverse_geocoder package."""
    rows = conn.execute("SELECT id, lat, lon FROM media WHERE lat IS NOT NULL AND place IS NULL").fetchall()
    if not rows:
        return
    try:
        import reverse_geocoder  # type: ignore
    except ImportError:
        log("[scan] install reverse_geocoder to turn GPS into place names (pip install '.[mac]')")
        return
    results = reverse_geocoder.search([(r["lat"], r["lon"]) for r in rows], mode=1)
    for r, g in zip(rows, results):
        parts = [g.get("name"), g.get("admin1"), g.get("cc")]
        conn.execute("UPDATE media SET place=? WHERE id=?", (", ".join(p for p in parts if p), r["id"]))
    conn.commit()
