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
    for sub in ("Events", "Broll", "Years", "Places", "Themes"):
        (gen / sub).mkdir(parents=True)
    _scaffold(vault)

    media = conn.execute(
        "SELECT * FROM media WHERE described=1 AND skip_reason IS NULL AND taken_at IS NOT NULL "
        "ORDER BY taken_at").fetchall()
    moments_by_media: dict[str, list] = {}
    for mo in conn.execute("SELECT * FROM moments ORDER BY media_id, start"):
        moments_by_media.setdefault(mo["media_id"], []).append(mo)
    media_by_id = {m["id"]: m for m in media}

    archive = [m for m in media if m["root_kind"] == "archive"]
    events = cluster_events(archive)
    used: set[str] = set()
    event_of: dict[str, str] = {}  # media id -> event note name
    summaries = []
    for ev in events:
        info = _write_event(cfg, conn, vault, gen / "Events", ev, moments_by_media, used)
        summaries.append(info)
        for m in ev:
            event_of[m["id"]] = info["name"]

    broll = [m for m in media if m["root_kind"] == "brand"]
    for m in broll:
        event_of[m["id"]] = _write_broll(cfg, conn, vault, gen / "Broll", m, moments_by_media.get(m["id"], []))

    years = _write_years(gen, summaries)
    places = _write_places(gen, summaries)
    themes = _write_themes(gen, media_by_id, moments_by_media, event_of)
    _write_timeline(gen, summaries)
    _write_index(gen, media, summaries, years, places, themes, len(broll))
    _write_base(vault)
    log(f"[vault] {len(events)} events, {len(broll)} B-roll notes, {len(themes)} themes -> {vault}")
    return {"events": len(events), "broll": len(broll), "themes": len(themes), "vault": str(vault)}


def _moment_line(mo, m, link: str | None = None) -> str:
    """One searchable, pullable line per moment: id, in/out, orientation, score, what happens."""
    where = f" · [[{link}]]" if link else ""
    speech = f" — “{mo['speech_en']}”" if mo["speech_en"] else ""
    span = f"{fmt_ts(mo['start'])}–{fmt_ts(mo['end'])}" if m["kind"] == "video" else "photo"
    return (f"- `m{mo['id']}` {span} · {m['orientation'] or '?'} · B{mo['broll_score']} · "
            f"{mo['description']}{speech}{where}")


def _write_event(cfg, conn, vault, folder, ev, moments_by_media, used: set[str]) -> dict:
    first, last = ev[0], ev[-1]
    places = Counter(m["place"] for m in ev if m["place"])
    place = places.most_common(1)[0][0] if places else ""
    tags = Counter(t for m in ev for t in jloads(m["tags"]))
    titles = [m["title"] for m in ev if m["title"]]
    headline = titles[0] if titles else "Untitled"
    name = _slug(f"{first['taken_at'][:10]} {_city(place) or ''} - {headline}", 90)
    base, k = name, 2
    while name in used:  # same day, place and title: keep both notes
        name, k = f"{base} ({k})", k + 1
    used.add(name)
    folder = folder / first["taken_at"][:4]
    folder.mkdir(exist_ok=True)
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
                lines.append(_moment_line(mo, m))
        lines += [f"`{m['root']}:{m['relpath']}` · id `{m['id']}`", ""]
    (folder / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")
    best_moments = sorted((mo for m in ev for mo in moments_by_media.get(m["id"], [])),
                          key=lambda mo: -(mo["broll_score"] or 0))[:3]
    return {"name": name, "date": first["taken_at"][:10], "place": place, "city": _city(place),
            "headline": headline, "items": len(ev), "video_min": video_min,
            "tags": [t for t, _ in tags.most_common(6)], "best": [mo["id"] for mo in best_moments]}


def _write_broll(cfg, conn, vault, folder, m, moments) -> str:
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
        lines.append(_moment_line(mo, m) + f" *({mo['shot_type']}, {mo['camera_motion']})*")
    lines += ["", f"`{m['root']}:{m['relpath']}` · id `{m['id']}`"]
    (folder / f"{name}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return name


def _write_timeline(gen: Path, summaries: list[dict]) -> None:
    lines = ["# Timeline", "", "*Every event in the archive, newest year first. Generated.*", ""]
    by_year: dict[str, list[dict]] = {}
    for e in summaries:
        by_year.setdefault(e["date"][:4], []).append(e)
    for year in sorted(by_year, reverse=True):
        lines.append(f"- [[{year}]]: {len(by_year[year])} events")
    (gen / "Timeline.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_years(gen: Path, summaries: list[dict]) -> list[str]:
    by_year: dict[str, list[dict]] = {}
    for e in summaries:
        by_year.setdefault(e["date"][:4], []).append(e)
    for year, evs in by_year.items():
        places = Counter(e["city"] for e in evs if e["city"])
        lines = ["---", "type: year", f"year: {year}", f"events: {len(evs)}",
                 f"places: {_yaml_list([p for p, _ in places.most_common(8)])}", "---",
                 f"# {year}", "",
                 f"*{len(evs)} events, {sum(e['items'] for e in evs)} photos/videos, "
                 f"{sum(e['video_min'] for e in evs):.0f} min of video. Main places: "
                 f"{', '.join(p for p, _ in places.most_common(5)) or 'unknown'}.*", ""]
        month = None
        for e in evs:
            if e["date"][:7] != month:
                month = e["date"][:7]
                lines += ["", f"## {datetime.strptime(month, '%Y-%m'):%B %Y}"]
            best = " ".join(f"`m{i}`" for i in e["best"])
            lines.append(f"- [[{e['name']}]] · {e['items']} items · {', '.join(e['tags'][:4])}"
                         + (f" · best: {best}" if best else ""))
        (gen / "Years" / f"{year}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sorted(by_year, reverse=True)


def _write_places(gen: Path, summaries: list[dict]) -> list[str]:
    by_city: dict[str, list[dict]] = {}
    for e in summaries:
        if e["city"]:
            by_city.setdefault(e["city"], []).append(e)
    for city, evs in by_city.items():
        lines = ["---", "type: place", f"place: {_q(evs[0]['place'])}", f"events: {len(evs)}",
                 f"first: {evs[0]['date']}", f"last: {evs[-1]['date']}", "---", f"# {city}", "",
                 f"*{len(evs)} events between {evs[0]['date']} and {evs[-1]['date']}.*", ""]
        lines += [f"- [[{e['name']}]] · {e['items']} items · {', '.join(e['tags'][:4])}" for e in evs]
        (gen / "Places" / f"{_slug(city)}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sorted(by_city, key=lambda c: -len(by_city[c]))


THEME_MIN_MOMENTS = 3
THEME_MAX = 60
THEME_MOMENTS = 80


def _write_themes(gen: Path, media_by_id: dict, moments_by_media: dict, event_of: dict) -> list[str]:
    """One page per content theme ("founder grind", "bike rides", ...) listing the best moments
    for it, so a script beat can be matched to footage by reading a single page."""
    by_theme: dict[str, list] = {}
    for mid, moments in moments_by_media.items():
        if mid not in media_by_id:
            continue
        for mo in moments:
            for theme in {t.strip().lower() for t in jloads(mo["content_uses"]) if t.strip()}:
                by_theme.setdefault(theme, []).append(mo)
    ranked = sorted((t for t in by_theme if len(by_theme[t]) >= THEME_MIN_MOMENTS),
                    key=lambda t: -len(by_theme[t]))[:THEME_MAX]
    for theme in ranked:
        moments = sorted(by_theme[theme], key=lambda mo: (-(mo["broll_score"] or 0),
                                                          media_by_id[mo["media_id"]]["taken_at"]))
        vertical = sum(1 for mo in moments if media_by_id[mo["media_id"]]["orientation"] == "vertical")
        lines = ["---", "type: theme", f"theme: {_q(theme)}", f"moments: {len(moments)}",
                 f"vertical: {vertical}", "---", f"# {theme.title()}", "",
                 f"*{len(moments)} moments ({vertical} vertical). Best B-roll first; "
                 f"showing up to {THEME_MOMENTS}. Pull clips with `crag pull m<id> ...`.*", ""]
        for mo in moments[:THEME_MOMENTS]:
            m = media_by_id[mo["media_id"]]
            lines.append(_moment_line(mo, m, event_of.get(m["id"])) + f" · {m['taken_at'][:10]}")
        (gen / "Themes" / f"{_slug(theme)}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ranked


def _write_index(gen: Path, media: list, summaries: list[dict], years: list[str], places: list[str],
                 themes: list[str], broll: int) -> None:
    videos = [m for m in media if m["kind"] == "video"]
    lines = [
        "# Start here", "",
        "*The map of the footage archive. Generated by `crag vault`; read this first, then drill down.*", "",
        f"- **{len(videos)} videos** ({sum(m['duration'] or 0 for m in videos) / 3600:.1f} h) and "
        f"**{len(media) - len(videos)} photos**, grouped into **{len(summaries)} events**",
        f"- **{broll}** purpose-shot brand B-roll clips → `_generated/Broll/`, gallery in `Library.base`",
        f"- Covers {summaries[0]['date'] if summaries else '?'} to {summaries[-1]['date'] if summaries else '?'}",
        "", "## Years", *[f"- [[{y}]]" for y in years],
        "", "## Places (most events first)", *[f"- [[{_slug(p)}]]" for p in places[:30]],
        "", "## Themes (best moments per content theme)", *[f"- [[{_slug(t)}]]" for t in themes],
        "", "## How to use this for a video",
        "1. Read `Me.md` and the relevant `Eras/` + `Stories/` notes for the story.",
        "2. For each beat of the script, open the matching Theme page, Year/Event, or run",
        "   `crag search \"<what should be on screen>\" --vertical --min-broll 3 --json`.",
        "3. Moments are listed as `m<id>` with in/out times, orientation and a B-roll score (B1–B5).",
        "4. `crag pull m12 m48 m7 --name my-reel` cuts those moments from the originals into",
        "   `library/exports/my-reel/` with a Premiere timeline (`timeline.xml`) and `selects.json`.",
    ]
    (gen / "Index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_base(vault: Path) -> None:
    (vault / "_generated" / "Library.base").write_text("""filters:
  or:
    - file.inFolder("_generated/Broll")
    - file.inFolder("_generated/Events")
    - file.inFolder("_generated/Themes")
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
    if src.exists() and (not claude_md.exists() or "<!-- custom -->" not in claude_md.read_text(encoding="utf-8")):
        shutil.copyfile(src, claude_md)  # keep the rules in sync with the vault layout
