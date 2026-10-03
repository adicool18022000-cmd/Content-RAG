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
from .usage import collection_hidden, hidden_ranges, hidden_sets, safe_parts
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
    for sub in ("Events", "Broll", "Years", "Places", "Themes", "Collections"):
        (gen / sub).mkdir(parents=True)
    _scaffold(vault)

    hidden = hidden_sets(conn)
    media = [m for m in conn.execute(
        "SELECT * FROM media WHERE described=1 AND skip_reason IS NULL AND taken_at IS NOT NULL "
        "ORDER BY taken_at").fetchall()
        if m["id"] not in hidden["media"] and not collection_hidden(m["collection"], hidden["collection"])]
    media_by_id = {m["id"]: m for m in media}
    use_counts = _use_counts(conn)
    moments_by_media: dict[str, list] = {}
    for mo in conn.execute("SELECT * FROM moments ORDER BY media_id, start"):
        m = media_by_id.get(mo["media_id"])
        if m is None or str(mo["id"]) in hidden["moment"]:
            continue
        mo = dict(mo)
        mo["uses"] = use_counts.get(mo["id"], 0)
        if hidden["person"] and m["kind"] == "video":
            blocked = hidden_ranges(conn, mo["media_id"], hidden["person"])
            if blocked:
                parts = safe_parts(mo["start"], mo["end"], blocked)
                if not parts:
                    continue  # a hidden person is on screen the whole time
                mo["start"], mo["end"] = max(parts, key=lambda p: p[1] - p[0])
        moments_by_media.setdefault(mo["media_id"], []).append(mo)

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
    collections = _write_collections(gen, media, moments_by_media, event_of, conn)
    _write_ideas(gen, media_by_id, moments_by_media, event_of, collections)
    _write_people(cfg, conn, vault, gen)
    _write_videos(conn, gen)
    _write_timeline(gen, summaries)
    _write_index(gen, media, summaries, years, places, themes, len(broll), collections)
    _write_base(vault)
    log(f"[vault] {len(events)} events, {len(collections)} collections, {len(broll)} B-roll notes, "
        f"{len(themes)} themes -> {vault}")
    return {"events": len(events), "collections": len(collections), "broll": len(broll), "themes": len(themes),
            "vault": str(vault)}


def _use_counts(conn) -> dict[int, int]:
    return {r["moment_id"]: r["n"] for r in conn.execute(
        "SELECT moment_id, count(*) n FROM usage WHERE moment_id IS NOT NULL GROUP BY moment_id")}


def _moment_line(mo, m, link: str | None = None) -> str:
    """One searchable, pullable line per moment: id, in/out, orientation, scores, what happens."""
    where = f" · [[{link}]]" if link else ""
    speech = f" — “{mo['speech_en']}”" if mo["speech_en"] else ""
    span = f"{fmt_ts(mo['start'])}–{fmt_ts(mo['end'])}" if m["kind"] == "video" else "photo"
    hook = f" · H{mo['hook_score']}" if (mo.get("hook_score") or 0) >= 3 else ""
    used = f" · used×{mo['uses']}" if mo.get("uses") else ""
    return (f"- `m{mo['id']}` {span} · {m['orientation'] or '?'} · B{mo['broll_score']}{hook}{used} · "
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


ROLE_SECTIONS = [
    ("hook", "🪝 Hooks (stop the scroll)"),
    ("spectacle", "🔥 Spectacle"),
    ("cinematic", "🎬 Cinematic"),
    ("story_to_camera", "🗣️ Told to camera"),
    ("funny", "😂 Funny"),
    ("emotional", "💛 Emotional"),
    ("friends", "👥 Friends / people"),
    ("establishing", "🏙️ Establishing shots"),
    ("transition", "🚶 Transitions"),
    ("action", "⚡ Action"),
    ("food", "🍜 Food"),
    ("calm", "🌊 Calm"),
    ("work", "💻 Work"),
]
PER_SECTION = 10


def _roles(mo) -> list[str]:
    return jloads(mo.get("roles"))


def _rank(mo) -> tuple:
    """Best first: strong hook/B-roll, fewer previous uses."""
    return (-(mo.get("hook_score") or 0) - (mo.get("broll_score") or 0) + 1.5 * (mo.get("uses") or 0),)


def _recipes(moments: list[dict], media_by_id: dict, n: int = 3) -> list[str]:
    """Ready-made reel outlines: hook -> story -> B-roll -> cinematic close, from different moments."""
    seeds = sorted((mo for mo in moments if mo.get("story_seed")), key=_rank)
    out = []
    for seed in seeds[:n]:
        taken = {seed["id"]}

        def pick(role, exclude=taken):
            cands = sorted((mo for mo in moments if role in _roles(mo) and mo["id"] not in exclude), key=_rank)
            if cands:
                exclude.add(cands[0]["id"])
                return cands[0]
            return None

        hook = pick("hook")
        broll = [x for x in (pick("establishing"), pick("cinematic"), pick("friends")) if x]
        close = pick("cinematic") or pick("calm")
        parts = []
        if hook:
            parts.append(f"hook `m{hook['id']}` ({hook['description'][:60]})")
        parts.append(f"story `m{seed['id']}`")
        if broll:
            parts.append("b-roll " + " ".join(f"`m{b['id']}`" for b in broll))
        if close:
            parts.append(f"close `m{close['id']}`")
        out.append(f"- **{seed['story_seed']}** → " + " → ".join(parts))
    return out


def _write_collections(gen: Path, media: list, moments_by_media: dict, event_of: dict, conn) -> list[str]:
    by_coll: dict[str, list] = {}
    for m in media:
        if m["collection"]:
            by_coll.setdefault(m["collection"], []).append(m)
    names = []
    for coll, items in sorted(by_coll.items(), key=lambda kv: kv[1][0]["taken_at"] or ""):
        media_by_id = {m["id"]: m for m in items}
        moments = [mo for m in items for mo in moments_by_media.get(m["id"], [])]
        if not moments:
            continue
        places = Counter(_city(m["place"]) for m in items if m["place"])
        role_counts = Counter(r for mo in moments for r in _roles(mo))
        used = Counter(r["video"] for r in conn.execute(
            f"SELECT video FROM usage WHERE media_id IN ({','.join('?' * len(items))})", [m["id"] for m in items]))
        first, last = items[0]["taken_at"][:10], items[-1]["taken_at"][:10]
        lines = ["---", "type: collection", f"collection: {_q(coll)}", f"items: {len(items)}",
                 f"first: {first}", f"last: {last}",
                 f"places: {_yaml_list([p for p, _ in places.most_common(6)])}",
                 f"hooks: {role_counts.get('hook', 0)}", f"story_seeds: {sum(1 for mo in moments if mo.get('story_seed'))}",
                 f"times_used: {sum(used.values())}", "---",
                 f"# {coll}", "",
                 f"*{len(items)} photos/videos, {first} → {last}"
                 + (f", in {', '.join(p for p, _ in places.most_common(4))}" if places else "") + ".*", ""]
        summaries = [m["summary"] for m in items if m["summary"]][:4]
        if summaries:
            lines += ["## What this was", *[f"- {x.strip()}" for x in summaries], ""]
        evs = sorted({event_of[m["id"]] for m in items if m["id"] in event_of})
        if len(evs) > 1:
            lines += ["## Parts (by date and place)", *[f"- [[{e}]]" for e in evs], ""]
        seeds = sorted((mo for mo in moments if mo.get("story_seed")), key=_rank)
        if seeds:
            lines += ["## Story seeds (things that happened - reel material)"]
            lines += [f"- **{mo['story_seed']}** · `m{mo['id']}` "
                      f"{fmt_ts(mo['start'])}–{fmt_ts(mo['end'])}"
                      + (" · told to camera" if "story_to_camera" in _roles(mo) else "")
                      + (f" · used×{mo['uses']}" if mo.get("uses") else "") for mo in seeds[:15]]
            lines.append("")
        for role, title in ROLE_SECTIONS:
            picks = sorted((mo for mo in moments if role in _roles(mo)), key=_rank)[:PER_SECTION]
            if picks:
                lines += [f"## {title}", *[_moment_line(mo, media_by_id[mo["media_id"]]) for mo in picks], ""]
        recipes = _recipes(moments, media_by_id)
        if recipes:
            lines += ["## Reel ideas", *recipes, ""]
        if used:
            lines += ["## Already used in", *[f"- {v} ({n} clips)" for v, n in used.most_common()], ""]
        (gen / "Collections" / f"{_slug(coll, 80)}.md").write_text("\n".join(lines), encoding="utf-8")
        names.append(coll)
    return names


def _write_ideas(gen: Path, media_by_id: dict, moments_by_media: dict, event_of: dict, collections: list[str]):
    moments = [mo for ms in moments_by_media.values() for mo in ms]
    seeds = sorted((mo for mo in moments if mo.get("story_seed")), key=_rank)
    gems = sorted((mo for mo in moments if not mo.get("uses") and
                   ((mo.get("hook_score") or 0) >= 4 or (mo.get("broll_score") or 0) >= 5)), key=_rank)
    lines = ["# Ideas", "",
             "*Story seeds and unused strong moments from the whole library, best first. Use them when "
             "there's no idea yet; each id can go straight into `crag pull`.*", "",
             "## Story seeds"]
    for mo in seeds[:80]:
        m = media_by_id[mo["media_id"]]
        where = m["collection"] or event_of.get(m["id"], "")
        lines.append(f"- **{mo['story_seed']}** · `m{mo['id']}` · {m['taken_at'][:10]} · {where}"
                     + (" · told to camera" if "story_to_camera" in _roles(mo) else "")
                     + (f" · used×{mo['uses']}" if mo.get("uses") else ""))
    lines += ["", "## Unused gems (strong hook or B-roll, never used yet)"]
    lines += [_moment_line(mo, media_by_id[mo["media_id"]], event_of.get(mo["media_id"])) for mo in gems[:60]]
    if collections:
        lines += ["", "## Collections with ideas", *[f"- [[{_slug(c, 80)}]]" for c in collections]]
    (gen / "Ideas.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_videos(conn, gen: Path) -> None:
    import json as _json

    from .usage import by_style

    rows = conn.execute("SELECT v.*, (SELECT count(*) FROM usage u WHERE u.video=v.name) AS clips FROM videos v "
                        "ORDER BY created_at DESC").fetchall()
    lines = ["# Videos", "", "*Every reel made from the library: style, page, clips used and results. "
             "Record results with `crag videos --posted <name> --views ... --saves ...`.*", ""]
    for v in rows:
        m = _json.loads(v["metrics"] or "{}")
        res = ", ".join(f"{k} {m[k]}" for k in ("views", "likes", "saves", "shares", "avg_watch_pct") if k in m)
        lines.append(f"- **{v['name']}** · {v['status']} · {(v['created_at'] or '')[:10]} · page {v['page'] or '-'} · "
                     f"style {v['style'] or '-'} · {v['clips']} clips" + (f" · {res}" if res else ""))
    stats = by_style(conn)
    if stats:
        lines += ["", "## Which styles work (posted videos, averages)"]
        lines += ["- " + " · ".join(f"{k} {val}" for k, val in r.items()) for r in stats]
    (gen / "Videos.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_people(cfg: Config, conn, vault: Path, gen: Path) -> None:
    rows = conn.execute(
        "SELECT p.*, (SELECT count(DISTINCT media_id) FROM faces f WHERE f.person_id=p.id) AS clips "
        "FROM people p WHERE p.faces >= 3 ORDER BY (p.name IS NULL), p.faces DESC LIMIT 200").fetchall()
    lines = ["# People", "",
             "*Face groups found on this Mac. Name one with `crag people name <id> <name>` (same name = same "
             "person), hide someone everywhere with `crag people hide <name>`.*", ""]
    for p in rows:
        img = ""
        if p["sample"]:
            src = cfg.library_dir / p["sample"]
            dst = vault / "_assets" / "people" / f"{p['id']}.jpg"
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                img = f" ![[_assets/people/{p['id']}.jpg|60]]"
        lines.append(f"- **#{p['id']} {p['name'] or '(unnamed)'}**{' · HIDDEN' if p['hidden'] else ''} · "
                     f"{p['faces']} faces in {p['clips']} clips{img}")
    (gen / "People.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_index(gen: Path, media: list, summaries: list[dict], years: list[str], places: list[str],
                 themes: list[str], broll: int, collections: list[str] | None = None) -> None:
    videos = [m for m in media if m["kind"] == "video"]
    lines = [
        "# Start here", "",
        "*The map of the footage archive. Generated by `crag vault`; read this first, then drill down.*", "",
        f"- **{len(videos)} videos** ({sum(m['duration'] or 0 for m in videos) / 3600:.1f} h) and "
        f"**{len(media) - len(videos)} photos**, grouped into **{len(summaries)} events**",
        f"- **{broll}** purpose-shot brand B-roll clips → `_generated/Broll/`, gallery in `Library.base`",
        f"- Covers {summaries[0]['date'] if summaries else '?'} to {summaries[-1]['date'] if summaries else '?'}",
        "", "## Collections (your folders, best clips by purpose)",
        *[f"- [[{_slug(c, 80)}]]" for c in (collections or [])],
        "- [[Ideas]]: story seeds and ready-made reel recipes from the whole library",
        "- [[People]]: face groups (named / hidden)",
        "- [[Videos]]: reels made so far, their styles and results",
        "", "## Years", *[f"- [[{y}]]" for y in years],
        "", "## Places (most events first)", *[f"- [[{_slug(p)}]]" for p in places[:30]],
        "", "## Themes (best moments per content theme)", *[f"- [[{_slug(t)}]]" for t in themes],
        "", "## How to use this for a video",
        "1. Read `Me.md` and the relevant `Eras/` + `Stories/` notes for the story.",
        "2. For each beat of the script, open the matching Theme page, Year/Event, or run",
        "   `crag search \"<what should be on screen>\" --vertical --min-broll 3 --json`.",
        "3. Moments are listed as `m<id>` with in/out times, orientation, B-roll quality (B1–B5),",
        "   hook strength (H3–H5) and how often they've been used (used×N). Prefer fresh ones.",
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
