"""Step 6: write the Obsidian "Life Vault".

Everything under `_generated/` is rebuilt on each run. Everything else (Eras, People, Places,
Stories, Me.md, Interviews) is yours and Claude's to write; it is created once and never
overwritten.
"""

from __future__ import annotations

import re
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

from .config import Config
from .db import jloads
from .util import fmt_ts

EVENT_GAP_HOURS = 4


def _slug(text: str, n: int = 60) -> str:
    text = re.sub(r"[\\/:*?\"<>|#^\[\]]", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()[:n].strip() or "untitled"


def _dt(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _city(place: str | None) -> str | None:
    return place.split(",")[0].strip() if place else None


def cluster_events(media: list) -> list[list]:
    """Group media into events: split when there is a gap of more than a few hours
    or the (known) city changes."""
    events: list[list] = []
    last_dt, last_city = None, None
    for m in media:
        dt, city = _dt(m["taken_at"]), _city(m["place"])
        new = (
            not events
            or dt is None or last_dt is None
            or (dt - last_dt).total_seconds() > EVENT_GAP_HOURS * 3600
            or (city and last_city and city != last_city)
        )
        if new:
            events.append([])
        events[-1].append(m)
        last_dt = dt or last_dt
        last_city = city or last_city
    return events


def _thumb(cfg: Config, conn, vault: Path, media_id: str) -> str | None:
    """Copy one representative frame into the vault so Obsidian can embed it."""
    frames = conn.execute("SELECT path FROM frames WHERE media_id=? ORDER BY t", (media_id,)).fetchall()
    if not frames:
        return None
    src = cfg.library_dir / frames[len(frames) // 2]["path"]
    rel = f"_assets/thumbs/{media_id}.jpg"
    dst = vault / rel
    if not dst.exists() and src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    return rel


def _yaml_list(values) -> str:
    return "[" + ", ".join('"' + str(v).replace('"', "'") + '"' for v in values) + "]"


def _q(value) -> str:
    return '"' + str(value or "").replace('"', "'") + '"'


def build_vault(cfg: Config, conn, log=print) -> dict:
    vault = cfg.vault_dir
    gen = vault / "_generated"
    if gen.exists():
        shutil.rmtree(gen)
    (gen / "Events").mkdir(parents=True)
    (gen / "Broll").mkdir(parents=True)
    _scaffold(vault)

    media = conn.execute(
        "SELECT * FROM media WHERE described=1 AND taken_at IS NOT NULL ORDER BY taken_at").fetchall()
    moments_by_media: dict[str, list] = {}
    for mo in conn.execute("SELECT * FROM moments ORDER BY media_id, start"):
        moments_by_media.setdefault(mo["media_id"], []).append(mo)

    archive = [m for m in media if m["root_kind"] == "archive"]
    events = cluster_events(archive)
    timeline: dict[str, list[str]] = {}
    for ev in events:
        name = _write_event(cfg, conn, vault, gen / "Events", ev, moments_by_media)
        timeline.setdefault(ev[0]["taken_at"][:7], []).append(name)

    broll = [m for m in media if m["root_kind"] == "brand"]
    for m in broll:
        _write_broll(cfg, conn, vault, gen / "Broll", m, moments_by_media.get(m["id"], []))

    _write_timeline(gen, timeline)
    _write_base(vault)
    log(f"[vault] {len(events)} events, {len(broll)} B-roll notes -> {vault}")
    return {"events": len(events), "broll": len(broll), "vault": str(vault)}


def _write_event(cfg, conn, vault, folder, ev, moments_by_media) -> str:
    first, last = ev[0], ev[-1]
    places = Counter(m["place"] for m in ev if m["place"])
    place = places.most_common(1)[0][0] if places else ""
    tags = Counter(t for m in ev for t in jloads(m["tags"]))
    titles = [m["title"] for m in ev if m["title"]]
    headline = titles[0] if titles else "Untitled"
    name = _slug(f"{first['taken_at'][:10]} {_city(place) or ''} - {headline}", 90)
    video_min = sum((m["duration"] or 0) for m in ev if m["kind"] == "video") / 60
    best = max(ev, key=lambda m: max((mo["broll_score"] or 0 for mo in moments_by_media.get(m["id"], [])), default=0))
    cover = _thumb(cfg, conn, vault, best["id"])

    lines = [
        "---",
        "type: event",
        f"date: {first['taken_at'][:10]}",
        f"end: {last['taken_at'][:10]}",
        f"time: {_q(first['taken_at'][11:16])}",
        f"place: {_q(place)}",
        f"media: {len(ev)}",
        f"videos: {sum(1 for m in ev if m['kind'] == 'video')}",
        f"photos: {sum(1 for m in ev if m['kind'] == 'photo')}",
        f"video_minutes: {video_min:.1f}",
        f"tags: {_yaml_list([t for t, _ in tags.most_common(12)])}",
        f"cover: {_q(f'[[{cover}]]') if cover else _q('')}",
        "era: ",
        "---",
        f"# {first['taken_at'][:10]} · {place or 'Unknown place'} · {headline}",
        "",
        f"*{len(ev)} items, {first['taken_at'][11:16]}–{last['taken_at'][11:16]}"
        + (f" (until {last['taken_at'][:10]})" if last["taken_at"][:10] != first["taken_at"][:10] else "")
        + ". Generated from footage; add the story behind it in an Era or Story note.*",
        "",
    ]
    for m in ev:
        thumb = _thumb(cfg, conn, vault, m["id"])
        meta = f"{m['kind']}, {m['orientation'] or '?'}" + (f", {fmt_ts(m['duration'])}" if m["kind"] == "video" else "")
        lines += [f"## {m['taken_at'][11:16]} · {m['title'] or 'Untitled'} ({meta})"]
        if thumb:
            lines.append(f"![[{thumb}|320]]")
        if m["summary"]:
            lines.append(m["summary"].strip())
        if m["kind"] == "video":
            for mo in moments_by_media.get(m["id"], []):
                speech = f" — “{mo['speech_en']}”" if mo["speech_en"] else ""
                lines.append(f"- `{fmt_ts(mo['start'])}–{fmt_ts(mo['end'])}` {mo['description']}"
                             f" *(B-roll {mo['broll_score']}/5)*{speech}")
        lines += [f"`{m['root']}:{m['relpath']}` · id `{m['id']}`", ""]
    (folder / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")
    return name


def _write_broll(cfg, conn, vault, folder, m, moments) -> None:
    thumb = _thumb(cfg, conn, vault, m["id"])
    best = max((mo["broll_score"] or 0 for mo in moments), default=0)
    uses = Counter(u for mo in moments for u in jloads(mo["content_uses"]))
    name = _slug(f"{m['title'] or 'Untitled'} ({m['id'][:6]})")
    lines = [
        "---",
        "type: broll",
        f"thumbnail: {_q(f'[[{thumb}]]') if thumb else _q('')}",
        f"taken: {m['taken_at'][:10]}",
        f"orientation: {m['orientation'] or ''}",
        f"duration: {round(m['duration'] or 0, 1)}",
        f"best_broll: {best}",
        f"uses: {_yaml_list([u for u, _ in uses.most_common(6)])}",
        f"tags: {_yaml_list(jloads(m['tags'])[:12])}",
        f"file: {_q(m['root'] + ':' + m['relpath'])}",
        f"media_id: {m['id']}",
        "---",
        f"# {m['title'] or 'Untitled'}",
    ]
    if thumb:
        lines.append(f"![[{thumb}|480]]")
    if m["summary"]:
        lines += ["", m["summary"].strip()]
    lines.append("")
    for mo in moments:
        lines.append(f"- `{fmt_ts(mo['start'])}–{fmt_ts(mo['end'])}` {mo['description']} "
                     f"*({mo['shot_type']}, {mo['camera_motion']}, B-roll {mo['broll_score']}/5)*")
    (folder / f"{name}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_timeline(gen: Path, timeline: dict[str, list[str]]) -> None:
    lines = ["# Timeline", "", "*Every event in the archive, by month. Generated.*", ""]
    year = None
    for month in sorted(timeline):
        if month[:4] != year:
            year = month[:4]
            lines += [f"## {year}", ""]
        lines.append(f"### {month}")
        lines += [f"- [[{n}]]" for n in timeline[month]]
        lines.append("")
    (gen / "Timeline.md").write_text("\n".join(lines), encoding="utf-8")


def _write_base(vault: Path) -> None:
    (vault / "_generated" / "Library.base").write_text("""filters:
  or:
    - file.inFolder("_generated/Broll")
    - file.inFolder("_generated/Events")
views:
  - type: cards
    name: "Brand B-roll"
    image: note.thumbnail
    filters:
      and:
        - 'type == "broll"'
    order:
      - thumbnail
      - file.name
      - best_broll
      - orientation
      - uses
  - type: table
    name: "Events"
    filters:
      and:
        - 'type == "event"'
    order:
      - file.name
      - date
      - place
      - media
      - video_minutes
      - era
  - type: cards
    name: "Events gallery"
    image: note.cover
    filters:
      and:
        - 'type == "event"'
    order:
      - cover
      - file.name
      - place
""", encoding="utf-8")


SCAFFOLD = {
    "Me.md": """---
type: profile
---
# Me

*Who I am, what I'm building, what I want my content to be known for. Claude reads this first.*

## Now
- City: Bangalore
- Building: (co-founder of ...)
- Content pillars:

## Story so far
- (Claude fills this in from the Eras after the interview passes.)
""",
    "Eras/_Era template.md": """---
type: era
start:
end:
place:
---
# Era name (e.g. College Year 1)

## What life looked like

## Key events
- [[_generated/Events/...]]

## Turning points / lessons

## People
- [[People/...]]

## Content angles
""",
    "Stories/_Story template.md": """---
type: story
era:
status: idea   # idea | scripted | posted
---
# Story title

## What happened

## Why it matters / the lesson

## Footage
- (moment ids or event links from search)

## Hook ideas
""",
    "People/.keep": "",
    "Places/.keep": "",
    "Interviews/.keep": "",
}


def _scaffold(vault: Path) -> None:
    for rel, text in SCAFFOLD.items():
        p = vault / rel
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
    claude_md = vault / "CLAUDE.md"
    src = Path(__file__).with_name("vault_CLAUDE.md")
    if not claude_md.exists() and src.exists():
        shutil.copyfile(src, claude_md)
