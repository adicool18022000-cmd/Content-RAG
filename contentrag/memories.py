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
import threading
from collections import Counter
from pathlib import Path

from .config import Config
from .db import jloads
from .usage import collection_hidden, hidden_ranges, hidden_sets, photo_has_hidden_person
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
    out = []
    for ev in _events(cfg, conn):
        mem = event_memory(ev, memories)
        out.append({"id": ev[0]["id"], "date": ev[0]["taken_at"][:10], "time": ev[0]["taken_at"][11:16],
                    "end": ev[-1]["taken_at"][:16].replace("T", " "), "place": _city(_place(ev)) or "",
                    "title": _headline(ev), "items": len(ev),
                    "videos": sum(1 for m in ev if m["kind"] == "video"),
                    "done": bool(mem), "importance": mem["importance"] if mem else 0,
                    "content": mem["content"] if mem else True})
    return {"events": out, "done": sum(1 for e in out if e["done"]), "feelings": FEELINGS}


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


def event_detail(cfg: Config, conn, event_id: str, show_hidden_people: bool = False) -> dict | None:
    ev = _find(cfg, conn, event_id)
    if ev is None:
        return None
    hidden = hidden_sets(conn)["person"]
    items, held_back = [], 0
    for m in ev:
        if hidden and not show_hidden_people and (
                (m["kind"] == "photo" and photo_has_hidden_person(conn, m["id"], hidden))
                or (m["kind"] == "video" and hidden_ranges(conn, m["id"], hidden))):
            held_back += 1
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
        })
    ids = [m["id"] for m in ev]
    marks = ",".join("?" * len(ids))
    people = [r["name"] for r in conn.execute(
        f"SELECT p.name, count(*) n FROM faces f JOIN people p ON p.id=f.person_id WHERE f.media_id IN ({marks}) "
        "AND p.name IS NOT NULL AND p.hidden=0 GROUP BY p.name ORDER BY n DESC LIMIT 12", ids)]
    tags = Counter(t for m in ev for t in jloads(m["tags"]))
    return {"id": ev[0]["id"], "date": ev[0]["taken_at"][:10], "time": ev[0]["taken_at"][11:16],
            "end": ev[-1]["taken_at"][:16].replace("T", " "), "place": _place(ev), "title": _headline(ev),
            "tags": [t for t, _ in tags.most_common(8)], "items": items, "held_back": held_back,
            "people_seen": people, "memory": load_memory(cfg, ev), "eras": eras(cfg)}


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
    return {"meaning": told(sec.get("What it meant", "")), "story": told(sec.get("What happened", "")),
            "angle": sec.get("Content angle", ""), "feeling": fm.get("feeling", ""), "importance": importance,
            "people": people, "era": fm.get("era", ""), "date": fm.get("date", ""),
            "content": (fm.get("content") or "yes").lower() not in ("no", "false"),
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
            "era": next((n["era"] for n in notes if n["era"]), ""), "content": any(n["content"] for n in notes)}


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
    people = [p.strip() for p in (data.get("people") or []) if str(p).strip()]
    try:
        importance = max(0, min(5, int(data.get("importance") or 0)))
    except (TypeError, ValueError):
        importance = 0
    lines = ["---", "type: memory", f"event_id: {ev[0]['id']}", f"date: {ev[0]['taken_at'][:10]}",
             f"era: {_q(clean(data.get('era')))}", f"people: {_yaml_list(people)}",
             f"feeling: {_q(clean(data.get('feeling')))}", f"importance: {importance or ''}",
             f"content: {'no' if data.get('content') is False else 'yes'}",
             "source: memories tab", "---", f"# {title}", "",
             "## What it meant", clean(data.get("meaning")) or "(not told yet)", "",
             "## What happened", clean(data.get("story")) or "(not told yet)", ""]
    if clean(data.get("angle")):
        lines += ["## Content angle", clean(data["angle"]), ""]
    for h, b in keep:
        lines += [f"## {h}", b, ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    for old in paths[1:]:  # their text is now in this note; the old files are kept aside, never deleted
        dest = cfg.vault_dir / "Memories" / "_merged" / old.relative_to(cfg.vault_dir / "Memories")
        dest.parent.mkdir(parents=True, exist_ok=True)
        old.replace(dest)
    sync_content_flags(cfg, conn)
    return {"ok": True, "note": path.relative_to(cfg.vault_dir).as_posix(), "merged": len(paths[1:])}


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

_whisper_lock = threading.Lock()


def transcribe_audio(cfg: Config, audio: bytes, suffix: str = ".webm", mode: str = "auto") -> str:
    """Speech to text on this Mac. mode: auto | hi (Hindi script) | en | translate (to English)."""
    try:
        import mlx_whisper  # type: ignore
    except ImportError:
        raise RuntimeError("Local Whisper isn't installed on this computer (pip install -e '.[mac]', Apple "
                           "Silicon). Meanwhile use macOS Dictation: click in the box and press Fn twice.")
    tmp = cfg.library_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp) as d:
        src, wav = Path(d) / f"voice{suffix}", Path(d) / "voice.wav"
        src.write_bytes(audio)
        r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src), "-ac", "1", "-ar", "16000",
                            str(wav)], capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"couldn't read the recording: {r.stderr.strip()[-200:]}")
        kw = {"task": "translate"} if mode == "translate" else {}
        language = {"hi": "hi", "en": "en"}.get(mode)
        with _whisper_lock:  # one at a time: the model is big
            res = mlx_whisper.transcribe(str(wav), path_or_hf_repo=cfg.whisper_model, language=language,
                                         condition_on_previous_text=False, verbose=None, **kw)
    from .transcribe import keep_segment

    return " ".join(s["text"].strip() for s in res.get("segments", []) if keep_segment(s)).strip()
