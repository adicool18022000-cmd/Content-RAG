"""Step 1: find every photo/video on the mounted roots, dedupe, read metadata."""

from __future__ import annotations

import os
import time
from pathlib import Path

from .config import Config
from .probe import fingerprint, media_kind, probe

# Files changed this recently are probably still being copied; they're picked up next scan.
SETTLE_SECONDS = 120
# Videos this short carry nothing worth describing.
MIN_VIDEO_SECONDS = 1.5
# iPhone Live Photos store a ~3 s video next to the photo with the same name.
LIVE_PHOTO_MAX_SECONDS = 4.0

SKIP_DIRS = {".Trashes", ".Spotlight-V100", ".fseventsd", ".TemporaryItems", "@eaDir", "$RECYCLE.BIN",
             "System Volume Information", "Recovered files"}


def iter_media(root: Path, exclude: Path | None, skip: list[str] | tuple = ()):
    """Media files under root, minus system folders, the library itself and the root's `skip` folders."""
    skipped = {(root / s).resolve() for s in skip}
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        dirnames[:] = [
            n for n in dirnames
            if not n.startswith(".") and n not in SKIP_DIRS
            and not (exclude and (d / n).resolve() == exclude)
            and (d / n).resolve() not in skipped
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
    stats = {"new": 0, "duplicate": 0, "known": 0, "replaced": 0, "still_copying": 0, "empty": 0,
             "failed": 0, "skipped_roots": []}
    library = cfg.library_dir.resolve() if cfg.library_dir.exists() else None
    now = time.time()
    for root in cfg.roots:
        if not root.mounted:
            stats["skipped_roots"].append(root.name)
            log(f"[scan] {root.name}: not mounted at {root.path}, skipping")
            continue
        log(f"[scan] {root.name}: {root.path}")
        for path, kind in iter_media(root.path, library, root.skip):
            rel = path.relative_to(root.path).as_posix()
            try:
                st = path.stat()
                if st.st_size == 0:
                    stats["empty"] += 1
                    continue
                if now - st.st_mtime < SETTLE_SECONDS:
                    stats["still_copying"] += 1
                    continue
                known = conn.execute(
                    "SELECT l.media_id, m.size FROM locations l JOIN media m ON m.id = l.media_id "
                    "WHERE l.root=? AND l.relpath=?", (root.name, rel)).fetchone()
                if known and known["size"] == st.st_size:
                    stats["known"] += 1
                    continue
                if known:  # same path, different file (re-copied / replaced): forget the old link
                    conn.execute("DELETE FROM locations WHERE root=? AND relpath=?", (root.name, rel))
                    stats["replaced"] += 1
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
                        (fid, kind, root.name, rel, root.kind, st.st_size, meta.duration,
                         meta.width, meta.height, meta.fps, meta.orientation, int(meta.has_audio),
                         meta.taken_at, meta.date_source, meta.lat, meta.lon, meta.camera),
                    )
                    stats["new"] += 1
                conn.execute("INSERT OR IGNORE INTO locations VALUES (?,?,?)", (root.name, rel, fid))
            except Exception as e:  # corrupt files, permission errors, drive unplugged mid-scan
                stats["failed"] += 1
                log(f"[scan]   failed {rel}: {e}")
                if not root.mounted:
                    log(f"[scan] {root.name} was disconnected; stopping this drive")
                    break
            if (stats["new"] + stats["duplicate"]) % 200 == 0:
                conn.commit()
        conn.commit()
    if stats["still_copying"]:
        log(f"[scan] {stats['still_copying']} files changed in the last {SETTLE_SECONDS // 60} min "
            "(still copying?) - they will be picked up by the next scan")
    from .collections import backfill_collections

    backfill_collections(conn)
    stats["removed"] = prune_missing(cfg, conn, log)
    stats["skipped"] = mark_skips(conn)
    geocode(conn, log)
    return stats


def prune_missing(cfg: Config, conn, log=print) -> int:
    """Forget files that were deleted from a *mounted* drive (unplugged drives are left alone),
    and media items with no copy left anywhere."""
    gone = 0
    for root in cfg.roots:
        if not root.mounted:
            continue
        for loc in conn.execute("SELECT relpath FROM locations WHERE root=?", (root.name,)).fetchall():
            if not (root.path / loc["relpath"]).exists():
                conn.execute("DELETE FROM locations WHERE root=? AND relpath=?", (root.name, loc["relpath"]))
                gone += 1
    orphans = [r["id"] for r in conn.execute(
        "SELECT id FROM media WHERE id NOT IN (SELECT media_id FROM locations)")]
    for mid in orphans:
        conn.execute("DELETE FROM moments_fts WHERE rowid IN (SELECT id FROM moments WHERE media_id=?)", (mid,))
        for table in ("moments", "frames", "cuts", "transcript"):
            conn.execute(f"DELETE FROM {table} WHERE media_id=?", (mid,))
        conn.execute("DELETE FROM requests WHERE payload LIKE ?", (f'%"{mid}"%',))
        conn.execute("DELETE FROM media WHERE id=?", (mid,))
    conn.commit()
    if orphans:
        log(f"[scan] removed {len(orphans)} items whose files no longer exist")
    return len(orphans)


def mark_skips(conn) -> int:
    """Videos not worth sending to the AI: Live Photo companions and sub-second clips.
    They stay in the index (and in search by date/place) but cost nothing."""
    n = 0
    rows = conn.execute(
        "SELECT id, root, relpath, duration FROM media WHERE kind='video' AND skip_reason IS NULL "
        "AND described=0 AND duration < ?", (LIVE_PHOTO_MAX_SECONDS,)).fetchall()
    for r in rows:
        reason = None
        if (r["duration"] or 0) < MIN_VIDEO_SECONDS:
            reason = "too short"
        else:
            stem = r["relpath"].rsplit(".", 1)[0]
            twin = conn.execute(
                "SELECT 1 FROM locations l JOIN media m ON m.id=l.media_id WHERE m.kind='photo' AND l.root=? "
                "AND l.relpath LIKE ? ESCAPE '\\' LIMIT 1",
                (r["root"], stem.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ".%")).fetchone()
            if twin:
                reason = "live photo video"
        if reason:
            conn.execute("UPDATE media SET skip_reason=?, described=1, prepped=1, transcribed=1 WHERE id=?",
                         (reason, r["id"]))
            n += 1
    conn.commit()
    return n


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
