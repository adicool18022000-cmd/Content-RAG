"""Memory lane: walk through your life event by event, see the photos and videos, and tell what
each moment meant (typed or by voice). Powers the dashboard's Memories tab.

Events are the same clusters as the vault's event notes (same ids), in date order. Answers are
saved as `LifeVault/Memories/<date> <title>.md`, which `crag vault` shows on the event and year
notes, and which Claude reads before writing anything about your life.
Voice is transcribed on this Mac with Whisper (mlx-whisper); audio never leaves the computer.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from .config import Config
from .db import jloads
from .usage import collection_hidden, hidden_ranges, hidden_sets, photo_has_hidden_person
from .faces import hidden_faces, media_faces, reviews_for
from .vault import _city, _q, _slug, _yaml_list, cluster_events, event_memory, read_memories

MANAGED = ("What it meant", "What happened", "Content angle")
FEELINGS = ("happy", "proud", "free", "grateful", "excited", "nervous", "lost", "sad", "angry", "nostalgic",
            "in love", "heartbroken", "peaceful", "funny")

_cache: dict = {}


def _events(cfg: Config, conn) -> list[list]:
    """Event clusters exactly as `crag vault` builds them (so event ids match the event notes)."""
    hidden = hidden_sets(conn)
    key = (tuple(conn.execute("SELECT count(*), max(taken_at) FROM media WHERE described=1").fetchone()),
           tuple(sorted(hidden["media"])), tuple(sorted(hidden["collection"])))
    if _cache.get("key") != key:
        media = [m for m in conn.execute(
            "SELECT * FROM media WHERE described=1 AND skip_reason IS NULL AND taken_at IS NOT NULL "
            "AND root_kind='archive' ORDER BY taken_at").fetchall()
            if m["id"] not in hidden["media"] and not collection_hidden(m["collection"], hidden["collection"])]
        _cache.update(key=key, events=cluster_events(media))
    return _cache["events"]


def _headline(ev: list) -> str:
    titles = [m["title"] for m in ev if m["title"]]
    return titles[0] if titles else "Untitled"


def _place(ev: list) -> str:
    places = Counter(m["place"] for m in ev if m["place"])
    return places.most_common(1)[0][0] if places else ""


def list_events(cfg: Config, conn) -> dict:
    """Every event, oldest first, with whether it already has a memory."""
    memories = read_memories(cfg.vault_dir)
    with_hidden = _media_with_hidden_people(conn)
    allowed = _media_with(conn, "allow")
    out = []
    for ev in _events(cfg, conn):
        mem = event_memory(ev, memories)
        out.append({"id": ev[0]["id"], "date": ev[0]["taken_at"][:10], "time": ev[0]["taken_at"][11:16],
                    "end": ev[-1]["taken_at"][:16].replace("T", " "), "place": _city(_place(ev)) or "",
                    "title": _headline(ev), "items": len(ev),
                    "videos": sum(1 for m in ev if m["kind"] == "video"),
                    "done": bool(mem), "importance": mem["importance"] if mem else 0,
                    "content": mem["content"] if mem else True,
                    "retell": bool(mem and mem["retell"]),  # voice-to-text was too damaged: tell it again
                    "hidden": sum(1 for m in ev if m["id"] in with_hidden),  # photos/videos with people you hid
                    "allowed": sum(1 for m in ev if m["id"] in allowed)})  # ...that you allowed in this memory
    return {"events": out, "done": sum(1 for e in out if e["done"]), "feelings": FEELINGS,
            "with_hidden": sum(1 for e in out if e["hidden"]), "retell": sum(1 for e in out if e["retell"])}


def _media_with_hidden_people(conn) -> set[str]:
    hidden = hidden_sets(conn)["person"]
    if not hidden:
        return set()
    marks = ",".join("?" * len(hidden))
    found = {r[0] for r in conn.execute(f"SELECT DISTINCT media_id FROM faces WHERE person_id IN ({marks})",
                                        [int(x) for x in hidden])}
    for mid in found & _media_with(conn, "allow"):  # allowed there: only counts if someone else hidden is in it
        if not photo_has_hidden_person(conn, mid, hidden):
            found.discard(mid)
    for mid in _media_with(conn, "present") - found:  # tagged by hand (face not found)
        if photo_has_hidden_person(conn, mid, hidden):
            found.add(mid)
    return found


def _media_with(conn, decision: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT media_id FROM face_reviews WHERE decision=? AND undone=0",
                                       (decision,))}


def _find(cfg: Config, conn, event_id: str) -> list | None:
    for ev in _events(cfg, conn):
        if any(m["id"] == event_id for m in ev):
            return ev
    return None


def _frames(conn, media_id: str, n: int) -> list[str]:
    rows = conn.execute("SELECT path FROM frames WHERE media_id=? ORDER BY t", (media_id,)).fetchall()
    if len(rows) <= n:
        return [r["path"] for r in rows]
    return [rows[round(i * (len(rows) - 1) / (n - 1))]["path"] for i in range(n)]


def _names_in(conn, m, hidden: set[str]) -> list[str]:
    from .usage import hidden_people_in

    return hidden_people_in(conn, m["id"], 0, (m["duration"] or 0) + 1, hidden) or ["someone you hid"]


def event_detail(cfg: Config, conn, event_id: str, show_hidden_people: bool = False) -> dict | None:
    ev = _find(cfg, conn, event_id)
    if ev is None:
        return None
    hidden = hidden_sets(conn)["person"]
    items, held_back = [], 0
    for m in ev:
        is_hidden = bool(hidden) and (
            (m["kind"] == "photo" and photo_has_hidden_person(conn, m["id"], hidden))
            or (m["kind"] == "video" and bool(hidden_ranges(conn, m["id"], hidden))))
        held_back += is_hidden
        if is_hidden and not show_hidden_people:
            continue
        moments = conn.execute("SELECT description, speech_en FROM moments WHERE media_id=? ORDER BY start",
                               (m["id"],)).fetchall()
        frames = _frames(conn, m["id"], 6 if m["kind"] == "video" else 1)
        items.append({
            "id": m["id"], "kind": m["kind"], "time": m["taken_at"][11:16], "title": m["title"] or "",
            "summary": m["summary"] or "", "duration": m["duration"], "orientation": m["orientation"],
            "frames": frames, "thumb": frames[len(frames) // 2] if frames else None,
            "said": [x["speech_en"] for x in moments if x["speech_en"]][:4],
            "seen": [x["description"] for x in moments if x["description"]][:4],
            "hidden_people": _names_in(conn, m, hidden) if is_hidden else [],
            "hidden_faces": hidden_faces(conn, m["id"]) if is_hidden else [],  # who was recognised, to check
            "reviews": reviews_for(conn, m["id"]),  # decisions about hidden tags here (undoable)
            "faces": media_faces(conn, m["id"]),    # who's in it, to name or fix
        })
    ids = [m["id"] for m in ev]
    marks = ",".join("?" * len(ids))
    people = [r["name"] for r in conn.execute(
        f"SELECT p.name, count(*) n FROM faces f JOIN people p ON p.id=f.person_id WHERE f.media_id IN ({marks}) "
        "AND p.name IS NOT NULL AND p.hidden=0 GROUP BY p.name ORDER BY n DESC LIMIT 12", ids)]
    tags = Counter(t for m in ev for t in jloads(m["tags"]))
    memory = load_memory(cfg, ev)
    suggest = None
    if memory and "story" in (memory.get("source") or ""):  # told in Story mode: maybe on the wrong slide
        suggest = suggest_move(cfg, conn, ev[0]["id"], f"{memory['story']}\n{memory['told']}")
    return {"id": ev[0]["id"], "date": ev[0]["taken_at"][:10], "time": ev[0]["taken_at"][11:16],
            "end": ev[-1]["taken_at"][:16].replace("T", " "), "place": _place(ev), "title": _headline(ev),
            "tags": [t for t, _ in tags.most_common(8)], "items": items, "held_back": held_back,
            "hidden_shown": show_hidden_people,
            "people_seen": people, "memory": memory, "eras": eras(cfg),
            "suggest_move": suggest, "nearby": nearby_events(cfg, conn, ev[0]["id"]) if memory else []}


# ---------------------------------------------------------------- full-size photos

FULL_SIDE = 2400
WEB_IMAGE = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def full_photo(cfg: Config, conn, media_id: str) -> Path | None:
    """A browser-viewable, full-size version of a photo for zooming in (originals that browsers can show
    are served as they are; HEIC/RAW are converted once into library/cache/full). Falls back to the
    small preview when the drive isn't plugged in."""
    from . import probe  # noqa: F401  (registers the HEIC opener)
    from .util import source_path

    src = source_path(cfg, conn, media_id)
    if src is not None and src.suffix.lower() in WEB_IMAGE and src.stat().st_size < 40_000_000:
        return src
    cache = cfg.library_dir / "cache" / "full" / f"{media_id}.jpg"
    if cache.exists():
        return cache
    if src is not None:
        try:
            from PIL import Image, ImageOps

            cache.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((FULL_SIDE, FULL_SIDE))
                im.save(cache, "JPEG", quality=88)
            return cache
        except Exception:  # noqa: BLE001 - try macOS sips, then the preview
            import shutil as _sh

            if _sh.which("sips"):
                subprocess.run(["sips", "-s", "format", "jpeg", "-Z", str(FULL_SIDE), str(src), "--out", str(cache)],
                               capture_output=True, timeout=120)
                if cache.exists():
                    return cache
    row = conn.execute("SELECT path FROM frames WHERE media_id=? ORDER BY t LIMIT 1", (media_id,)).fetchone()
    return cfg.library_dir / row["path"] if row else None


# ---------------------------------------------------------------- memory notes

def _sections(text: str) -> tuple[dict, list[tuple[str, str]], str]:
    """(frontmatter dict, [(heading, body)], title line)."""
    from .vault import _frontmatter

    fm = _frontmatter(text)
    body = text.split("\n---", 1)[1].split("\n", 1)[1] if text.startswith("---") and "\n---" in text else text
    title = next((ln[2:].strip() for ln in body.splitlines() if ln.startswith("# ")), "")
    parts = re.split(r"^## +(.+)$", body, flags=re.M)
    return fm, [(parts[i].strip(), parts[i + 1].strip()) for i in range(1, len(parts) - 1, 2)], title


def _memory_paths(cfg: Config, ev: list) -> list[Path]:
    """Every memory note of this event, oldest first (older notes may come from finer-split events)."""
    memories = read_memories(cfg.vault_dir)
    names = []
    for m in ev:
        mem = memories.get(m["id"])
        if mem and mem["name"] not in names:
            names.append(mem["name"])
    return [p for p in (cfg.vault_dir / f"{n}.md" for n in names) if p.exists()]


def _read(path: Path) -> dict:
    fm, sections, title = _sections(path.read_text(encoding="utf-8", errors="ignore"))
    sec = dict(sections)
    people = [p.strip().strip('"').strip("'") for p in fm.get("people", "").strip("[]").split(",") if p.strip()]
    try:
        importance = int(fm.get("importance") or 0)
    except ValueError:
        importance = 0
    told = lambda x: "" if x.strip() == "(not told yet)" else x.strip()  # noqa: E731
    yes = lambda k: (fm.get(k) or "").lower() in ("yes", "true")  # noqa: E731
    return {"meaning": told(sec.get("What it meant", "")), "story": told(sec.get("What happened", "")),
            "angle": sec.get("Content angle", ""), "feeling": fm.get("feeling", ""), "importance": importance,
            "people": people, "era": fm.get("era", ""), "date": fm.get("date", ""),
            "content": (fm.get("content") or "yes").lower() not in ("no", "false"),
            "source": fm.get("source", ""), "retell": yes("needs_retelling"), "repaired": fm.get("repaired", ""),
            "told": sec.get("As told (voice)", ""),
            "extra": [(h, b) for h, b in sections if h not in MANAGED], "title": title}


def load_memory(cfg: Config, ev: list) -> dict | None:
    """The event's memory; several older notes for one event are shown together (saved as one)."""
    paths = _memory_paths(cfg, ev)
    if not paths:
        return None
    notes = [_read(p) for p in paths]
    join = lambda key: "\n\n".join(n[key] for n in notes if n[key].strip())  # noqa: E731
    feelings = []
    for n in notes:
        feelings += [f.strip() for f in n["feeling"].split(",") if f.strip() and f.strip() not in feelings]
    people = []
    for n in notes:
        people += [x for x in n["people"] if x not in people]
    return {"note": paths[0].relative_to(cfg.vault_dir).as_posix(), "merged": len(paths),
            "meaning": join("meaning"), "story": join("story"), "angle": join("angle"),
            "feeling": ", ".join(feelings), "importance": max(n["importance"] for n in notes), "people": people,
            "era": next((n["era"] for n in notes if n["era"]), ""), "content": any(n["content"] for n in notes),
            "retell": any(n["retell"] for n in notes), "told": join("told"),
            "source": next((n["source"] for n in notes if n["source"]), ""),
            "questions": [ln[2:].strip() for n in notes for h, b in n["extra"] if h == "Open questions"
                          for ln in b.splitlines() if ln.startswith("- ")]}


def eras(cfg: Config) -> list[str]:
    names = {p.stem for p in (cfg.vault_dir / "Eras").glob("*.md") if not p.name.startswith("_")}
    names |= {m["era"] for m in read_memories(cfg.vault_dir).values() if m["era"]}
    return sorted(names)


def save_memory(cfg: Config, conn, event_id: str, data: dict) -> dict:
    """Write (or update) the Memories note for an event. Other sections the note has are kept."""
    ev = _find(cfg, conn, event_id)
    if ev is None:
        raise ValueError("unknown event")
    paths = _memory_paths(cfg, ev)
    keep: list[tuple[str, str]] = []
    title = f"{ev[0]['taken_at'][:10]} · {_city(_place(ev)) or 'Unknown place'} · {_headline(ev)}"
    if paths:
        notes = [_read(p) for p in paths]
        keep = [x for n in notes for x in n["extra"]]
        title = notes[0]["title"] or title
        path = paths[0]
    else:
        name = _slug(f"{ev[0]['taken_at'][:10]} {_city(_place(ev)) or ''} {_headline(ev)}", 80)
        path = cfg.vault_dir / "Memories" / ev[0]["taken_at"][:4] / f"{name}.md"
        k = 2
        while path.exists():
            path = path.with_name(f"{name} ({k}).md")
            k += 1
    clean = lambda s: (s or "").strip().replace("\r\n", "\n")  # noqa: E731
    retell = any(n["retell"] for n in notes) if paths else False
    repaired = next((n["repaired"] for n in notes if n["repaired"]), "") if paths else ""
    if data.get("retold"):  # told again: the damaged earlier version is kept aside, never used as the story
        retell = False
        old_story = "\n\n".join(n["story"] for n in notes if n["story"]) if paths else ""
        old_told = next((b for h, b in keep if h == "As told (voice)"), "")
        keep = [(h, b) for h, b in keep if h != "As told (voice)"]
        earlier = "\n\n".join(x for x in (f"**Earlier draft:**\n{old_story}" if old_story else "",
                                           f"**Voice-to-text then:**\n{old_told}" if old_told else "") if x)
        if earlier:
            keep = _add_section(keep, EARLIER, earlier)
    if "needs_retelling" in data:
        retell = bool(data["needs_retelling"])
    for h, b in data.get("extra_sections") or []:  # sections of notes merged into this one (move_memory)
        keep = _add_section(keep, h, b)
    people = [p.strip() for p in (data.get("people") or []) if str(p).strip()]
    try:
        importance = max(0, min(5, int(data.get("importance") or 0)))
    except (TypeError, ValueError):
        importance = 0
    lines = ["---", "type: memory", f"event_id: {ev[0]['id']}", f"date: {ev[0]['taken_at'][:10]}",
             f"era: {_q(clean(data.get('era')))}", f"people: {_yaml_list(people)}",
             f"feeling: {_q(clean(data.get('feeling')))}", f"importance: {importance or ''}",
             f"content: {'no' if data.get('content') is False else 'yes'}",
             f"source: {clean(data.get('source')) or 'memories tab'}"]
    lines += ["needs_retelling: yes"] if retell else []
    lines += [f"repaired: {repaired}"] if repaired else []
    lines += ["---", f"# {title}", "",
             "## What it meant", clean(data.get("meaning")) or "(not told yet)", "",
             "## What happened", clean(data.get("story")) or "(not told yet)", ""]
    if clean(data.get("angle")):
        lines += ["## Content angle", clean(data["angle"]), ""]
    told = clean(data.get("told"))
    if told:  # the raw voice transcript (Story mode): kept next to the cleaned version, added to what's there
        old = next((b for h, b in keep if h == "As told (voice)"), "")
        keep = [(h, b) for h, b in keep if h != "As told (voice)"] + [
            ("As told (voice)", f"{old}\n\n{told}".strip() if told not in old else old)]
    photos = [x for x in (data.get("photos") or []) if x.get("said")]
    if photos:  # what was said about single photos (Story mode, photo opened full screen): for finding them later
        old = next((b for h, b in keep if h == "About the photos"), "")
        new = [f"- photo {x.get('n') or '?'} (`{x['media_id']}`): {clean(x['said'])}" for x in photos]
        rows = old.splitlines() + [x for x in new if x not in old]
        keep = [(h, b) for h, b in keep if h != "About the photos"] + [("About the photos", "\n".join(rows).strip())]
    questions = [clean(q) for q in (data.get("questions") or []) if clean(q)]
    if questions:  # what the AI couldn't make out: for you to answer (or Claude to ask) later
        keep = _add_section(keep, "Open questions", "\n".join(f"- {q}" for q in questions))
    rec = data.get("recording") or {}
    if rec.get("file") and rec.get("spans"):  # where in the kept recording this was said (to listen / redo)
        spans = ", ".join(f"{_clock(a)}–{_clock(b)}" for a, b in rec["spans"])
        keep = _add_section(keep, "Recordings", f"- `{rec['file']}` at {spans}")
    for h, b in keep:
        lines += [f"## {h}", b, ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    absorb = [Path(x) for x in data.get("absorb") or []]
    for old in paths[1:] + [x for x in absorb if x.exists() and x != path]:
        _set_aside(cfg, old)  # their text is now in this note; the old files are kept aside, never deleted
    sync_content_flags(cfg, conn)
    return {"ok": True, "note": path.relative_to(cfg.vault_dir).as_posix(), "merged": len(paths[1:]) + len(absorb)}


EARLIER = "Earlier versions (voice-to-text errors)"


def _clock(t: float) -> str:
    t = int(t or 0)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def _add_section(keep: list[tuple[str, str]], heading: str, body: str) -> list[tuple[str, str]]:
    """Add text to a section (lines already there aren't repeated); the section is created if missing."""
    body = body.strip()
    old = next((b for h, b in keep if h == heading), None)
    if old is None:
        return keep + [(heading, body)]
    if body in old:
        return keep
    if all(ln.startswith("- ") for ln in body.splitlines() if ln.strip()):
        new = "\n".join([old] + [ln for ln in body.splitlines() if ln.strip() and ln not in old.splitlines()])
    else:
        new = f"{old}\n\n{body}"
    return [(h, new if h == heading else b) for h, b in keep]


def _set_aside(cfg: Config, note: Path) -> None:
    root = cfg.vault_dir / "Memories"
    dest = root / "_merged" / note.relative_to(root)
    k = 2
    while dest.exists():
        dest = dest.with_name(f"{note.stem} ({k}).md")
        k += 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    note.replace(dest)


# ---------------------------------------------------------------- the right event

def nearby_events(cfg: Config, conn, event_id: str, n: int = 6) -> list[dict]:
    """The events just before and after (to move a story told on the wrong one)."""
    evs = _events(cfg, conn)
    i = next((k for k, ev in enumerate(evs) if any(m["id"] == event_id for m in ev)), None)
    if i is None:
        return []
    memories = read_memories(cfg.vault_dir)
    return [{"id": ev[0]["id"], "date": ev[0]["taken_at"][:10], "end": ev[-1]["taken_at"][:10],
             "place": _city(_place(ev)) or "", "title": _headline(ev), "items": len(ev),
             "done": bool(event_memory(ev, memories)), "this": k == i}
            for k, ev in enumerate(evs[max(0, i - n):i + n + 1], start=max(0, i - n))]


_STOP = set("""the and was were with that this then there they them from have had for but not you your our are his
her she him its what when where which who will would could should about after before into over just very really
also because went came come going said told like some more most much many time day days night today photo photos
video videos picture pictures selfie selfies group friends friend people person place home house room shot image
images taken outside inside front near view hai tha thi the aur bhi nahi kya kuch bahut phir woh yeh main mein hum
log gaye gaya gayi tab jab unclear""".split())


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", (text or "").lower()) if w not in _STOP}


def _event_words(ev: list) -> dict[str, float]:
    """What an event is about: places and folders count double, titles and tags once."""
    out: dict[str, float] = {}
    for m in ev:
        for w in _words(" ".join(filter(None, [m["place"], m["collection"]]))):
            out[w] = 2.0
        for w in _words(" ".join([m["title"] or ""] + list(jloads(m["tags"])))):
            out.setdefault(w, 1.0)
    return out


def suggest_move(cfg: Config, conn, event_id: str, text: str, n: int = 6) -> dict | None:
    """If a story names the place / occasion of a nearby event much more than its own, that event.
    (Story mode puts words on the event that was on screen; a long story can start one slide early or late.)"""
    import math

    evs = _events(cfg, conn)
    i = next((k for k, ev in enumerate(evs) if any(m["id"] == event_id for m in ev)), None)
    said = _words(text)
    if i is None or not said:
        return None
    day = lambda ev: ev[0]["taken_at"][:10]  # noqa: E731
    from datetime import date

    d0 = date.fromisoformat(day(evs[i]))
    cands = [k for k in range(max(0, i - n), min(len(evs), i + n + 1))
             if abs((date.fromisoformat(day(evs[k])) - d0).days) <= 10]
    desc = {k: _event_words(evs[k]) for k in cands}
    df = Counter(w for k in cands for w in desc[k])

    def score(k):
        hits = {w: desc[k][w] * math.log(1 + len(cands) / df[w]) for w in said & desc[k].keys()}
        return sum(hits.values()), hits

    own, _ = score(i)
    best = max((k for k in cands if k != i), key=lambda k: score(k)[0], default=None)
    if best is None:
        return None
    s, hits = score(best)
    strong = [w for w in hits if desc[best][w] >= 2.0 or df[w] == 1]
    if s < max(1.5, 2 * own) or not strong:
        return None
    ev = evs[best]
    why = sorted(hits, key=lambda w: -hits[w])[:3]
    return {"event_id": ev[0]["id"], "date": day(ev), "place": _city(_place(ev)) or "", "title": _headline(ev),
            "items": len(ev), "why": [w.title() for w in why]}


def move_memory(cfg: Config, conn, from_id: str, to_id: str) -> dict:
    """Move an event's memory to another event (told while the wrong event was on screen). If that event
    already has a memory, the two are combined. The old note is kept in Memories/_merged."""
    src_ev, dst_ev = _find(cfg, conn, from_id), _find(cfg, conn, to_id)
    if src_ev is None or dst_ev is None:
        raise ValueError("unknown event")
    if src_ev[0]["id"] == dst_ev[0]["id"]:
        raise ValueError("that's the same event")
    src_paths = _memory_paths(cfg, src_ev)
    if not src_paths:
        raise ValueError("this event has no memory to move")
    src = [_read(p) for p in src_paths]
    dst = load_memory(cfg, dst_ev)
    notes = ([dst] if dst else []) + src
    join = lambda key: "\n\n".join(x[key] for x in notes if (x.get(key) or "").strip())  # noqa: E731
    feelings, people = [], []
    for x in notes:
        feelings += [f.strip() for f in (x.get("feeling") or "").split(",") if f.strip() and f.strip() not in feelings]
        people += [p for p in x.get("people") or [] if p not in people]
    moved = "\n".join(f"- from {x['date'] or '?'} · {x['title'] or 'a nearby event'}" for x in src)
    return save_memory(cfg, conn, dst_ev[0]["id"], {
        "story": join("story"), "meaning": join("meaning"), "angle": join("angle"), "feeling": ", ".join(feelings),
        "importance": max(x.get("importance") or 0 for x in notes), "people": people,
        "era": next((x["era"] for x in notes if x.get("era")), ""), "content": any(x.get("content") for x in notes),
        "source": next((x["source"] for x in src if x.get("source")), "memories tab"),
        "needs_retelling": any(x.get("retell") for x in notes),
        "extra_sections": [e for x in src for e in x["extra"]] + [("Moved here", moved)], "absorb": src_paths})


def sync_content_flags(cfg: Config, conn) -> int:
    """Clips of events whose memory says `content: no` are never suggested for videos (table content_off).
    Rebuilt from the memory notes, so editing a note (by hand or by Claude) is enough."""
    from .vault import event_memory

    memories = read_memories(cfg.vault_dir)
    off = []
    if any(not m["content"] for m in memories.values()):
        for ev in _events(cfg, conn):
            mem = event_memory(ev, memories)
            if mem and not mem["content"]:
                off += [(m["id"], mem["name"]) for m in ev]
    conn.execute("DELETE FROM content_off")
    conn.executemany("INSERT OR REPLACE INTO content_off VALUES (?, ?)", off)
    conn.commit()
    return len(off)


# ---------------------------------------------------------------- voice

EMPTY_RECORDING = ("The recording came through empty. Check that the right microphone is picked (next to "
                   "Speak), that the browser may use it, and speak for a few seconds before pressing Stop.")
SILENT_RECORDING = ("I couldn't hear anything in that recording. Pick another microphone next to Speak (or check "
                    "System Settings > Sound > Input), then try again.")
SILENCE_DB = -45.0  # loudest moment quieter than this = the mic heard nothing


def event_terms(cfg: Config, conn, event_id: str | None) -> list[str]:
    """Names Whisper should expect while this event is on screen: its places, folders and the people in
    it (also the ones you've hidden: you still talk about them)."""
    ev = _find(cfg, conn, event_id) if event_id else None
    if not ev:
        return []
    terms = []
    for place, _ in Counter(m["place"] for m in ev if m["place"]).most_common(3):
        terms += [x.strip() for x in place.split(",")[:3] if x.strip() and not re.search(r"\d", x)]
    for col, _ in Counter(m["collection"] for m in ev if m["collection"]).most_common(2):
        terms.append(re.sub(r"[_\-]+", " ", str(col)).strip())
    ids = [m["id"] for m in ev]
    marks = ",".join("?" * len(ids))
    terms += [r[0] for r in conn.execute(
        f"SELECT p.name, count(*) n FROM faces f JOIN people p ON p.id=f.person_id WHERE f.media_id IN ({marks}) "
        "AND p.name IS NOT NULL GROUP BY lower(p.name) ORDER BY n DESC LIMIT 12", ids)]
    return terms


def whisper_segments(cfg: Config, wav: Path, mode: str = "mixed", conn=None, event_id: str | None = None) -> list[dict]:
    """[{start, end, text}] for a 16 kHz wav: language set (never guessed), your names as vocabulary,
    invented lines / loops / wrong-script lines removed (see voice.py)."""
    from . import voice

    vocab = voice.vocabulary(cfg, conn)
    extra = event_terms(cfg, conn, event_id) if conn is not None else []
    return voice.transcribe_samples(cfg, voice.load_wav(wav), mode, voice.prompt_for(vocab, extra),
                                    vocab_terms_=voice.vocab_terms(vocab))


def transcribe_audio(cfg: Config, audio: bytes, suffix: str = ".webm", mode: str = "mixed",
                     event_id: str | None = None) -> str:
    """Speech to text on this Mac. mode: mixed (Hindi + English) | en | translate (Hindi -> English text).
    The recording stays in a temporary folder on this computer and is deleted right after."""
    from . import voice

    try:
        import mlx_whisper  # type: ignore  # noqa: F401
    except ImportError:
        raise RuntimeError("Local Whisper isn't installed on this computer (pip install -e '.[mac]', Apple "
                           "Silicon). Meanwhile use macOS Dictation: click in the box and press Fn twice.")
    if len(audio) < 1500:
        raise RuntimeError(EMPTY_RECORDING)
    with tempfile.TemporaryDirectory(prefix="crag-voice-") as d:
        src, wav = Path(d) / f"voice{suffix}", Path(d) / "voice.wav"
        src.write_bytes(audio)
        r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                            str(wav)], capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not wav.exists() or wav.stat().st_size < 3200:
            raise RuntimeError(EMPTY_RECORDING)
        if voice.loudest_db(voice.load_wav(wav)) < SILENCE_DB:
            raise RuntimeError(SILENT_RECORDING)
        conn = None
        if cfg.db_path.exists():
            from .db import connect

            conn = connect(cfg.db_path, threads=True)
        try:
            segs = whisper_segments(cfg, wav, mode, conn, event_id)
        finally:
            if conn is not None:
                conn.close()
    text = " ".join(x["text"] for x in segs).strip()
    if not text or text == voice.UNCLEAR:
        raise RuntimeError(SILENT_RECORDING)
    return text
