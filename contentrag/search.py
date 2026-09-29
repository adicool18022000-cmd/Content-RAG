"""Hybrid clip search: keyword (SQLite FTS5) + meaning (vectors), fused, then filtered."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .db import jloads
from .util import fmt_ts, source_path

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
                      md.duration, md.root_kind
               FROM moments m JOIN media md ON md.id = m.media_id WHERE {where}"""
    if ranked_lists:
        scores = _rrf(*ranked_lists)
        ids = sorted(scores, key=scores.get, reverse=True)
        rows = []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            rows += conn.execute(f"{base} AND m.id IN ({','.join('?' * len(chunk))})", (*args, *chunk)).fetchall()
        rows.sort(key=lambda r: (-scores[r["id"]], -(r["broll_score"] or 0)))
    else:
        rows = conn.execute(f"{base} ORDER BY m.broll_score DESC, md.taken_at DESC LIMIT ?", (*args, limit)).fetchall()
    return [_result(cfg, conn, r) for r in rows[:limit]]


def _result(cfg: Config, conn, r) -> dict:
    mid = (r["start"] + r["end"]) / 2
    thumb = conn.execute(
        "SELECT path FROM frames WHERE media_id=? ORDER BY abs(t - ?) LIMIT 1", (r["media_id"], mid)).fetchone()
    src = source_path(cfg, conn, r["media_id"])
    return {
        "moment_id": r["id"],
        "media_id": r["media_id"],
        "file": str(src) if src else f"{r['root']}:{r['relpath']} (drive not mounted)",
        "kind": r["kind"],
        "start": round(r["start"], 2),
        "end": round(r["end"], 2),
        "in_out": f"{fmt_ts(r['start'])}-{fmt_ts(r['end'])}" if r["kind"] == "video" else "photo",
        "taken_at": r["taken_at"],
        "place": r["place"],
        "orientation": r["orientation"],
        "broll_score": r["broll_score"],
        "shot_type": r["shot_type"],
        "description": r["description"],
        "speech_en": r["speech_en"],
        "tags": jloads(r["tags"]),
        "quality_issues": jloads(r["quality_issues"]),
        "clip_title": r["title"],
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
