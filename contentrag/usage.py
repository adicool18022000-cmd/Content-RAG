"""Usage tracking (which part of which clip went into which video) and the hide list (privacy).

Re-using a clip is allowed - different parts of one clip, or the hook shown again later in the
same reel - but every use is recorded so search can prefer fresh footage and nothing gets
over-used across reels without anyone noticing.
"""

from __future__ import annotations

import json
from datetime import datetime

HIDE_KINDS = ("moment", "media", "collection", "person")
MIN_SAFE_SECONDS = 1.5  # shorter leftovers after removing hidden parts aren't worth showing
FACE_PAD_SECONDS = 1.5  # a face seen at t is assumed on screen for t ± this (frames are sampled)


# ---------------------------------------------------------------- videos + usage

def record_video(conn, name: str, page: str | None = None, style: str | None = None,
                 uses: list[dict] | None = None) -> None:
    """uses: [{media_id, start, end, moment_id?, role?}]. Re-recording a video replaces its uses."""
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("INSERT INTO videos(name, created_at, page, style) VALUES (?,?,?,?) "
                 "ON CONFLICT(name) DO UPDATE SET page=coalesce(excluded.page, page), "
                 "style=coalesce(excluded.style, style)", (name, now, page, style))
    if uses is not None:
        conn.execute("DELETE FROM usage WHERE video=?", (name,))
        conn.executemany(
            "INSERT INTO usage(video, media_id, start, end, moment_id, role, at) VALUES (?,?,?,?,?,?,?)",
            [(name, u["media_id"], u.get("start"), u.get("end"), u.get("moment_id"), u.get("role"), now)
             for u in uses])
    conn.commit()


def set_posted(conn, name: str, metrics: dict | None = None, notes: str | None = None) -> None:
    row = conn.execute("SELECT metrics FROM videos WHERE name=?", (name,)).fetchone()
    if row is None:
        raise SystemExit(f"No video called '{name}' (see `crag videos`)")
    merged = {**json.loads(row["metrics"] or "{}"), **(metrics or {})}
    conn.execute("UPDATE videos SET status='posted', posted_at=coalesce(posted_at, ?), metrics=?, "
                 "notes=coalesce(?, notes) WHERE name=?",
                 (datetime.now().isoformat(timespec="seconds"), json.dumps(merged), notes, name))
    conn.commit()


def uses_of(conn, media_id: str, start: float | None = None, end: float | None = None) -> list[dict]:
    """Uses of this clip (overlapping start-end if given)."""
    rows = conn.execute(
        "SELECT u.*, v.status FROM usage u LEFT JOIN videos v ON v.name=u.video WHERE u.media_id=?",
        (media_id,)).fetchall()
    out = []
    for r in rows:
        if start is not None and r["start"] is not None and (r["end"] <= start or r["start"] >= end):
            continue
        out.append(dict(r))
    return out


def videos(conn) -> list[dict]:
    return [dict(r) | {"clips": r["clips"]} for r in conn.execute(
        "SELECT v.*, (SELECT count(*) FROM usage u WHERE u.video=v.name) AS clips FROM videos v "
        "ORDER BY created_at DESC")]


# ---------------------------------------------------------------- hide list

def hide(conn, kind: str, ref: str, reason: str | None = None) -> None:
    if kind not in HIDE_KINDS:
        raise SystemExit(f"kind must be one of {', '.join(HIDE_KINDS)}")
    ref = ref.lstrip("mM") if kind == "moment" else ref
    conn.execute("INSERT OR REPLACE INTO hidden VALUES (?,?,?)", (kind, ref, reason))
    if kind == "person":
        conn.execute("UPDATE people SET hidden=1 WHERE id=?", (int(ref),))
    conn.commit()


def unhide(conn, kind: str, ref: str) -> None:
    ref = ref.lstrip("mM") if kind == "moment" else ref
    conn.execute("DELETE FROM hidden WHERE kind=? AND ref=?", (kind, ref))
    if kind == "person":
        conn.execute("UPDATE people SET hidden=0 WHERE id=?", (int(ref),))
    conn.commit()


def hidden_sets(conn) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {k: set() for k in HIDE_KINDS}
    for r in conn.execute("SELECT kind, ref FROM hidden"):
        out[r["kind"]].add(r["ref"])
    for r in conn.execute("SELECT id FROM people WHERE hidden=1"):
        out["person"].add(str(r["id"]))
    return out


def collection_hidden(collection: str | None, hidden_collections: set[str]) -> bool:
    if not collection:
        return False
    return any(collection == h or collection.startswith(h + " / ") for h in hidden_collections)


def hidden_ranges(conn, media_id: str, people: set[str] | None = None) -> list[tuple[float, float]]:
    """Time ranges of a clip where a hidden person is on screen (from face grouping)."""
    people = hidden_sets(conn)["person"] if people is None else people
    if not people:
        return []
    marks = ",".join("?" * len(people))
    rows = conn.execute(
        f"SELECT t FROM faces WHERE media_id=? AND person_id IN ({marks}) ORDER BY t",
        (media_id, *[int(p) for p in people])).fetchall()
    return merge_ranges([(max(0.0, r["t"] - FACE_PAD_SECONDS), r["t"] + FACE_PAD_SECONDS) for r in rows])


BLUR_PAD_SECONDS = 1.0     # a face found at t is blurred from t-1 s to t+1 s (faces are checked once a second)
BLUR_BOX_PAD = 0.45        # and the box is grown by this much on every side (heads move between checks)
FACE_FRAME_LONG_SIDE = 640  # faces are found on frames scaled to this long side (prep + dense recheck)


def face_boxes(conn, media_id: str, start: float, end: float, people: set[str] | None = None) -> list[tuple]:
    """Where hidden people's faces are between start and end of a clip:
    [(t, x, y, w, h)] with t in source seconds and x/y/w/h as fractions of the picture."""
    people = hidden_sets(conn)["person"] if people is None else people
    if not people:
        return []
    m = conn.execute("SELECT width, height FROM media WHERE id=?", (media_id,)).fetchone()
    W, H = (m["width"] or 16, m["height"] or 9) if m else (16, 9)
    fw, fh = (FACE_FRAME_LONG_SIDE, FACE_FRAME_LONG_SIDE * H / W) if W >= H else \
        (FACE_FRAME_LONG_SIDE * W / H, FACE_FRAME_LONG_SIDE)
    marks = ",".join("?" * len(people))
    rows = conn.execute(
        f"SELECT t, x, y, w, h FROM faces WHERE media_id=? AND person_id IN ({marks}) AND t BETWEEN ? AND ? "
        "ORDER BY t", (media_id, *[int(p) for p in people], start - BLUR_PAD_SECONDS, end + BLUR_PAD_SECONDS)).fetchall()
    out = []
    for r in rows:
        x, y, w, h = r["x"] / fw, r["y"] / fh, r["w"] / fw, r["h"] / fh
        x0, y0 = max(0.0, x - w * BLUR_BOX_PAD), max(0.0, y - h * BLUR_BOX_PAD)
        x1, y1 = min(1.0, x + w * (1 + BLUR_BOX_PAD)), min(1.0, y + h * (1 + BLUR_BOX_PAD))
        if x1 > x0 and y1 > y0:
            out.append((round(r["t"], 3), round(x0, 4), round(y0, 4), round(x1 - x0, 4), round(y1 - y0, 4)))
    return out


def photo_has_hidden_person(conn, media_id: str, people: set[str] | None = None) -> bool:
    people = hidden_sets(conn)["person"] if people is None else people
    if not people:
        return False
    marks = ",".join("?" * len(people))
    return conn.execute(f"SELECT 1 FROM faces WHERE media_id=? AND person_id IN ({marks}) LIMIT 1",
                        (media_id, *[int(p) for p in people])).fetchone() is not None


def merge_ranges(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for s, e in sorted(ranges):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def safe_parts(start: float, end: float, hidden: list[tuple[float, float]],
               min_len: float = MIN_SAFE_SECONDS) -> list[tuple[float, float]]:
    """Parts of [start, end] that don't overlap hidden ranges (and are long enough to use)."""
    parts, cur = [], start
    for hs, he in hidden:
        if he <= cur or hs >= end:
            continue
        if hs > cur:
            parts.append((cur, min(hs, end)))
        cur = max(cur, he)
    if cur < end:
        parts.append((cur, end))
    return [(round(s, 2), round(e, 2)) for s, e in parts if e - s >= min_len]


METRICS = ("views", "likes", "saves", "shares", "comments", "avg_watch_pct")


def by_style(conn) -> list[dict]:
    """Average results of posted videos per style (and page) - which editing styles work."""
    groups: dict[tuple, list[dict]] = {}
    for v in conn.execute("SELECT style, page, metrics FROM videos WHERE status='posted'"):
        groups.setdefault((v["style"] or "-", v["page"] or "-"), []).append(json.loads(v["metrics"] or "{}"))
    out = []
    for (style, page), ms in sorted(groups.items()):
        row = {"style": style, "page": page, "videos": len(ms)}
        for k in METRICS:
            vals = [m[k] for m in ms if isinstance(m.get(k), (int, float))]
            if vals:
                row[f"avg_{k}"] = round(sum(vals) / len(vals), 1)
        out.append(row)
    return out
