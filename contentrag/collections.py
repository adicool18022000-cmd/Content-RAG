"""Collections: the creator's own folder names ("Thailand Trip", "Summer Stay Hostel", "Sem 5")
are the best labels in the archive. A clip's collection is the meaningful part of its folder
path, with generic folders (DCIM, Camera, WhatsApp, 2023, New folder...) left out.
"""

from __future__ import annotations

import re

_GENERIC = {
    "dcim", "camera", "camera roll", "photos", "photo", "pictures", "pics", "images", "videos", "video", "movies",
    "media", "clips", "footage", "raw", "originals", "export", "exports", "exported", "import", "imports",
    "downloads", "download", "new folder", "untitled folder", "untitled", "misc", "random", "other", "others",
    "all", "backup", "backups", "copy", "iphone", "android", "phone", "mobile", "google photos", "takeout",
    "whatsapp", "whatsapp video", "whatsapp images", "whatsapp video sent", "sent", "private", "screenshots",
    "screen recordings", "archive", "brand_broll", "brand broll", "content", "temp", "tmp", "live photos",
    "snapchat", "instagram", "telegram", "airdrop", "icloud photos",
    "mobile files", "mobile", "phone files", "iphone files", "android files", "internal", "internal storage",
    "internal shared storage", "sdcard", "new photo", "new photos", "new video", "new videos", "m4root", "clip",
    "clips", "avchd", "bdmv", "stream", "private", "dcim", "media files", "camera uploads",
    "ssd", "hdd", "external", "external drive", "hard disk", "hard drive", "pendrive", "usb", "sd card", "memory card",
}
_GENERIC_RE = re.compile(
    r"^(\d{3}apple|\d{3}[a-z]{4,5}|\d{3,4}|\d{4}[-_ .]\d{1,2}([-_ .]\d{1,2})?|img|vid|dsc|new folder \(\d+\)|"
    r"copy of .*|.* copy|camera\d*|dcim\d*|\d+_?(photos|videos)?|(local )?(disk|drive|volume) ?[a-z0-9]?|"
    r"[a-z] ?drive|(ssd|hdd) ?\d*)$")


def is_generic(name: str) -> bool:
    n = name.strip().lower()
    return not n or n in _GENERIC or bool(_GENERIC_RE.match(n)) or n.startswith("whatsapp")


_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}(-\d{2})?\s+")


def collection_of(relpath: str) -> str | None:
    """'College/Sem 5/DCIM/IMG_1.MOV' -> 'College / Sem 5'.
    Organised folders ('2025/2025-11 Thailand Trip/x.mov') give 'Thailand Trip'."""
    parts = []
    for p in relpath.replace("\\", "/").split("/")[:-1]:
        if p == "_Duplicates":
            continue
        p = _DATE_PREFIX.sub("", p)
        if not is_generic(p):
            parts.append(p)
    return " / ".join(parts) or None


def backfill_collections(conn, full: bool = False) -> None:
    """Fill in missing collections; full=True (on scan) recomputes all, so better naming rules reach old items."""
    rows = conn.execute("SELECT id, relpath, collection FROM media"
                        + ("" if full else " WHERE collection IS NULL")).fetchall()
    for r in rows:
        c = collection_of(r["relpath"])
        if c != r["collection"]:
            conn.execute("UPDATE media SET collection=? WHERE id=?", (c, r["id"]))


def all_collections(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT collection AS name, count(*) AS items, min(taken_at) AS first, max(taken_at) AS last "
        "FROM media WHERE collection IS NOT NULL GROUP BY collection ORDER BY first")]
