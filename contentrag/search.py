"""Hybrid clip search: keyword (SQLite FTS5) + meaning (vectors), fused, then filtered."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .db import jloads
from .usage import (collection_hidden, hidden_people_in, hidden_ranges, hidden_sets, photo_has_hidden_person,
                    safe_parts, uses_of)
from .util import fmt_ts, source_path

USE_PENALTY = 0.6  # each earlier use pushes a moment down; it never hides it

_WORD = re.compile(r"\w+", re.UNICODE)
_STOP = {"a", "an", "the", "of", "in", "on", "at", "me", "my", "i", "with", "and", "or", "to", "for", "is", "some", "clip", "clips", "video", "shot", "shots"}


@dataclass
class Filters:
    orientation: str | None = None      # vertical | horizontal | square
    min_broll: int | None = None
    date_from: str | None = None        # YYYY or YYYY-MM or YYYY-MM-DD
    date_to: str | None = None
    place: str | None = None
    root_kind: str | None = None        # archive | brand
    kind: str | None = None             # video | photo
    min_seconds: float | None = None
    exclude_issues: tuple[str, ...] = ()
    collection: str | None = None       # folder label, partial match: "thailand"
    roles: tuple[str, ...] = ()         # any of: hook, cinematic, spectacle, story_to_camera, ...
    min_hook: int | None = None
    min_motion: int | None = None
    max_motion: int | None = None
    fresh: bool = False                 # only moments never used in a video
    include_hidden: bool = False
    held_back: bool = False             # ONLY the moments left out because a hidden person is in them


def fts_query(text: str) -> str | None:
    words = [w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1]
    if not words:
        return None
    return " OR ".join(f'"{w}"*' for w in words)


def _rrf(*rankings: list[int], k: int = 60) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, mid in enumerate(ranking):
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank + 1)
    return scores


def _filter_sql(f: Filters) -> tuple[str, list]:
    where, args = [], []
    if f.orientation:
        where.append("md.orientation = ?")
        args.append(f.orientation)
    if f.min_broll:
        where.append("m.broll_score >= ?")
        args.append(f.min_broll)
    if f.date_from:
        where.append("md.taken_at >= ?")
        args.append(f.date_from)
    if f.date_to:
        where.append("md.taken_at < ?")
        args.append(_date_upper(f.date_to))
    if f.place:
        where.append("md.place LIKE ?")
        args.append(f"%{f.place}%")
    if f.root_kind:
        where.append("md.root_kind = ?")
        args.append(f.root_kind)
    if f.kind:
        where.append("md.kind = ?")
        args.append(f.kind)
    if f.min_seconds and f.kind != "photo":
        where.append("(md.kind = 'photo' OR m.end - m.start >= ?)")
        args.append(f.min_seconds)
    for issue in f.exclude_issues:
        where.append("m.quality_issues NOT LIKE ?")
        args.append(f'%"{issue}"%')
    if f.collection:
        where.append("md.collection LIKE ?")
        args.append(f"%{f.collection}%")
    if f.roles:
        where.append("(" + " OR ".join("m.roles LIKE ?" for _ in f.roles) + ")")
        args += [f'%"{r}"%' for r in f.roles]
    if f.min_hook:
        where.append("m.hook_score >= ?")
        args.append(f.min_hook)
    if f.min_motion:
        where.append("m.motion >= ?")
        args.append(f.min_motion)
    if f.max_motion:
        where.append("m.motion <= ?")
        args.append(f.max_motion)
    return (" AND ".join(where) or "1=1"), args


def _date_upper(value: str) -> str:
    """Exclusive upper bound for a YYYY / YYYY-MM / YYYY-MM-DD prefix."""
    parts = value.split("-")
    if len(parts) == 1:
        return f"{int(parts[0]) + 1:04d}"
    if len(parts) == 2:
        y, m = int(parts[0]), int(parts[1])
        return f"{y + (m == 12):04d}-{(m % 12) + 1:02d}"
    return value + "T99"


def search(cfg: Config, conn, query: str = "", filters: Filters | None = None, limit: int = 20,
           use_vectors: bool = True) -> list[dict]:
    filters = filters or Filters()
    ranked_lists: list[list[int]] = []
    q = fts_query(query) if query else None
    if q:
        ranked_lists.append([r[0] for r in conn.execute(
            "SELECT rowid FROM moments_fts WHERE moments_fts MATCH ? ORDER BY bm25(moments_fts) LIMIT 300", (q,))])
    if query and use_vectors:
        from .embed import vector_search

        ranked_lists.append([mid for mid, _ in vector_search(cfg, query)])
    where, args = _filter_sql(filters)
    base = f"""SELECT m.*, md.kind, md.root, md.relpath, md.orientation, md.taken_at, md.place, md.title,
                      md.duration, md.root_kind, md.collection
               FROM moments m JOIN media md ON md.id = m.media_id WHERE {where}"""
    if ranked_lists:
        scores = _rrf(*ranked_lists)
        ids = sorted(scores, key=scores.get, reverse=True)
        rows = []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            rows += conn.execute(f"{base} AND m.id IN ({','.join('?' * len(chunk))})", (*args, *chunk)).fetchall()
    else:
        scores = None
        rows = conn.execute(f"{base} ORDER BY m.hook_score DESC, m.broll_score DESC, md.taken_at DESC LIMIT ?",
                            (*args, max(limit * 5, 200))).fetchall()
    results = []
    hidden = hidden_sets(conn) if not filters.include_hidden else {k: set() for k in ("moment", "media",
                                                                                    "collection", "person")}
    ranges_cache: dict[str, list] = {}
    for r in rows:
        if str(r["id"]) in hidden["moment"] or r["media_id"] in hidden["media"] \
                or collection_hidden(r["collection"], hidden["collection"]):
            continue
        start, end, trimmed, names = r["start"], r["end"], False, []
        if hidden["person"] and r["kind"] == "photo":
            if filters.held_back or photo_has_hidden_person(conn, r["media_id"], hidden["person"]):
                continue  # photos with a hidden person are never suggested
        elif hidden["person"]:
            if r["media_id"] not in ranges_cache:
                ranges_cache[r["media_id"]] = hidden_ranges(conn, r["media_id"], hidden["person"])
            blocked = ranges_cache[r["media_id"]]
            if filters.held_back:
                # the whole moment, untouched: usable only if the creator asks for it, with faces blurred
                if not any(hs < end and he > start for hs, he in blocked):
                    continue
                names = hidden_people_in(conn, r["media_id"], start, end, hidden["person"])
            elif blocked:
                parts = safe_parts(start, end, blocked)
                if not parts:
                    continue  # a hidden person is on screen for the whole moment
                start, end = max(parts, key=lambda p: p[1] - p[0])
                trimmed = (start, end) != (r["start"], r["end"])
        elif filters.held_back:
            continue
        uses = uses_of(conn, r["media_id"], start, end)
        if filters.fresh and uses:
            continue
        rank = (scores[r["id"]] if scores else 1.0) / (1 + USE_PENALTY * len(uses))
        results.append((rank, -(r["hook_score"] or 0), -(r["broll_score"] or 0),
                        _result(cfg, conn, r, start, end, trimmed, uses)
                        | ({"held_back": True, "hidden_people": names} if filters.held_back else {})))
    results.sort(key=lambda x: (-x[0], x[1], x[2]))
    return [x[3] for x in results[:limit]]


def _result(cfg: Config, conn, r, start: float | None = None, end: float | None = None, trimmed: bool = False,
            uses: list | None = None) -> dict:
    start = r["start"] if start is None else start
    end = r["end"] if end is None else end
    mid = (start + end) / 2
    thumb = conn.execute(
        "SELECT path FROM frames WHERE media_id=? ORDER BY abs(t - ?) LIMIT 1", (r["media_id"], mid)).fetchone()
    src = source_path(cfg, conn, r["media_id"])
    uses = uses or []
    return {
        "moment_id": r["id"],
        "media_id": r["media_id"],
        "file": str(src) if src else f"{r['root']}:{r['relpath']} (drive not mounted)",
        "kind": r["kind"],
        "start": round(start, 2),
        "end": round(end, 2),
        "in_out": f"{fmt_ts(start)}-{fmt_ts(end)}" if r["kind"] == "video" else "photo",
        "trimmed_for_privacy": trimmed,
        "taken_at": r["taken_at"],
        "place": r["place"],
        "collection": r["collection"],
        "orientation": r["orientation"],
        "broll_score": r["broll_score"],
        "hook_score": r["hook_score"],
        "motion": r["motion"],
        "roles": jloads(r["roles"]),
        "story_seed": r["story_seed"] or "",
        "shot_type": r["shot_type"],
        "energy": r["energy"],
        "description": r["description"],
        "speech_en": r["speech_en"],
        "tags": jloads(r["tags"]),
        "quality_issues": jloads(r["quality_issues"]),
        "clip_title": r["title"],
        "uses": len(uses),
        "used_in": sorted({u["video"] for u in uses}),
        "thumbnail": str(cfg.library_dir / thumb["path"]) if thumb else None,
    }


def write_html(results: list[dict], query: str, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    cards = []
    for i, r in enumerate(results, 1):
        img = Path(r["thumbnail"]).as_uri() if r["thumbnail"] else ""
        cards.append(f"""<div class="c"><img src="{img}" loading="lazy"><div class="b">
<b>#{i} · {html.escape(r['in_out'])}</b> · B-roll {r['broll_score']}/5 · {html.escape(r['orientation'] or '')}<br>
<small>{html.escape((r['taken_at'] or '')[:16])} · {html.escape(r['place'] or '')}</small>
<p>{html.escape(r['description'] or '')}</p>
<code>{html.escape(r['file'])}</code></div></div>""")
    out.write_text(f"""<!doctype html><meta charset="utf-8"><title>{html.escape(query)}</title>
<style>body{{font:14px system-ui;margin:16px;background:#111;color:#eee}}
.g{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}}
.c{{background:#1c1c1c;border-radius:8px;overflow:hidden}}img{{width:100%;height:180px;object-fit:contain;background:#000}}
.b{{padding:8px}}code{{font-size:11px;word-break:break-all;color:#9ad}}p{{margin:6px 0}}</style>
<h2>{html.escape(query)} — {len(results)} results</h2><div class="g">{''.join(cards)}</div>""", encoding="utf-8")
    return out


def to_json(results: list[dict]) -> str:
    return json.dumps(results, ensure_ascii=False, indent=2)
