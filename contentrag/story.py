"""Story mode: talk through your life while a slideshow of your events plays, one recording per session.

The page shows one event at a time (date, place, its photos/videos) and moves on by itself when you
stop talking. It records continuously and uploads the audio every few seconds plus the timeline of
which event was on screen when; a crash loses at most a few seconds and the session can be finished
later. When you press Finish:

1. Whisper (on this Mac) transcribes the recording with timestamps,
2. each sentence goes to the event that was on screen while you said it,
3. an AI turns each event's words into a memory draft (story, what it meant, feeling, importance,
   people, chapter, for content or not): Claude through your Claude Code login if available, else
   Gemini, else your words are kept as they are,
4. you review the drafts on one screen and save them all as Memories notes.

Sessions live in library_dir/story/<id>.json (+ .audio until saved).
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .config import Config

BATCH = 20
SKIP_WORDS = ("not important", "skip", "nothing", "don't use", "do not use", "personal", "kuch nahi",
              "important nahi", "mat daalna", "chhodo", "chodo")

SYSTEM = """You turn a person's spoken memories into memory notes for their private life archive.
They looked at photos/videos of one event at a time and talked about it (voice-to-text: Hindi, Hinglish or
English, with mistakes, filler words and broken sentences). For every event you get the date, place, what the
footage shows, people recognised in it, and what they said.

Write, per event:
- story: what happened, in clear first-person English ("I ..."), from what they said. Keep a Hindi/Hinglish
  phrase only when the exact words matter. Fix obvious voice-to-text mistakes. Use the footage only to make
  sense of their words. Never invent facts, names, feelings or reasons that they didn't say.
- meaning: one or two first-person lines on what it meant to them, only if they expressed it; else "".
- feeling: 0-3 words from what they said (e.g. happy, proud, free, nervous, nostalgic, sad).
- importance 1-5 (1 just a day, 3 meaningful, 5 changed my life), from what and how much they said.
- people: names they mentioned (use the spelling from the known-people list when it is clearly the same person).
- era: the chapter of life it belongs to: reuse one of the known chapters when it fits; otherwise a short new
  name only if they made it clear (e.g. "School in Patna"), else "".
- content: false if they said it's not important, private, or not to be used, or said only "skip"; else true.
- angle: a one-line reel idea only if the story clearly has one; else "".
The footage list is numbered as on their screen; they may say "photo 3 is..." or "first picture...". about_photo
is what they said while that numbered photo was open full screen. Use both to understand which picture is which.
If an event has already_told (what they said about it before), write one story that combines it with the new words.
Return one entry per event_id you were given."""

SCHEMA = {
    "type": "object",
    "properties": {"memories": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "event_id": {"type": "string"}, "story": {"type": "string"}, "meaning": {"type": "string"},
            "feeling": {"type": "array", "items": {"type": "string"}},
            "importance": {"type": "integer"}, "people": {"type": "array", "items": {"type": "string"}},
            "era": {"type": "string"}, "content": {"type": "boolean"}, "angle": {"type": "string"},
        },
        "required": ["event_id", "story", "meaning", "feeling", "importance", "people", "era", "content", "angle"],
        "additionalProperties": False}}},
    "required": ["memories"], "additionalProperties": False,
}

_lock = threading.Lock()
_running: dict[str, threading.Thread] = {}


def _dir(cfg: Config) -> Path:
    d = cfg.library_dir / "story"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(cfg: Config, sid: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", sid or ""):
        raise ValueError("bad session id")
    return _dir(cfg) / f"{sid}.json"


def load(cfg: Config, sid: str) -> dict:
    p = _path(cfg, sid)
    if not p.exists():
        raise ValueError("no such session")
    return json.loads(p.read_text(encoding="utf-8"))


def _save(cfg: Config, s: dict) -> None:
    p = _path(cfg, s["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def start(cfg: Config, mime: str = "audio/webm") -> dict:
    sid = uuid.uuid4().hex[:12]
    s = {"id": sid, "created": time.strftime("%Y-%m-%d %H:%M"), "state": "recording", "mime": mime,
         "chunks": 0, "bytes": 0, "timeline": [], "marks": {}, "message": "", "drafts": [], "ai": None}
    _save(cfg, s)
    return s


def append_chunk(cfg: Config, sid: str, seq: int, data: bytes) -> dict:
    with _lock:
        s = load(cfg, sid)
        if s["state"] != "recording":
            raise ValueError("this session is already finished")
        if seq < s["chunks"]:
            return {"ok": True, "chunks": s["chunks"]}  # sent twice (retry): already have it
        if seq > s["chunks"]:
            raise ValueError(f"missing part {s['chunks']} of the recording")
        with open(_dir(cfg) / f"{sid}.audio", "ab") as f:
            f.write(data)
        s["chunks"] += 1
        s["bytes"] += len(data)
        _save(cfg, s)
        return {"ok": True, "chunks": s["chunks"]}


def update(cfg: Config, sid: str, timeline: list, marks: dict) -> None:
    with _lock:
        s = load(cfg, sid)
        s["timeline"] = [{"event_id": str(t["event_id"]), "start": float(t["start"]), "end": float(t["end"])}
                         | ({"item": str(t["item"])} if t.get("item") else {})  # a photo opened full screen
                         for t in timeline if t.get("event_id")]
        s["marks"] = {str(k): str(v) for k, v in (marks or {}).items()}
        _save(cfg, s)


def sessions(cfg: Config) -> list[dict]:
    """Sessions not saved yet (recording interrupted, being processed, waiting for review)."""
    out = []
    for p in sorted(_dir(cfg).glob("*.json"), reverse=True):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if s.get("state") not in ("saved", "discarded"):
            out.append({k: s.get(k) for k in ("id", "created", "state", "message", "bytes")}
                       | {"events": len({t["event_id"] for t in s.get("timeline", [])}),
                          "running": s["id"] in _running and _running[s["id"]].is_alive()})
    return out


def discard(cfg: Config, sid: str) -> None:
    s = load(cfg, sid)
    s["state"] = "discarded"
    _save(cfg, s)
    (_dir(cfg) / f"{sid}.audio").unlink(missing_ok=True)


# ---------------------------------------------------------------- processing

def _entry(seg: dict, timeline: list[dict]) -> dict:
    """The timeline entry (event, or one photo of it opened full screen) on screen for most of a segment."""
    a, b = seg["start"], max(seg["end"], seg["start"] + 0.01)
    best = max(timeline, key=lambda t: (min(b, t["end"]) - max(a, t["start"]),
                                        -abs((t["start"] + t["end"]) / 2 - (a + b) / 2)))
    if min(b, best["end"]) - max(a, best["start"]) <= 0:  # said in a gap: what was shown just before
        before = [t for t in timeline if t["start"] <= a]
        best = max(before, key=lambda t: t["start"]) if before else timeline[0]
    return best


def assign(segments: list[dict], timeline: list[dict]) -> dict[str, list[str]]:
    """Each spoken segment goes to the event that was on screen for most of it."""
    out: dict[str, list[str]] = {}
    for seg in segments if timeline else []:
        out.setdefault(_entry(seg, timeline)["event_id"], []).append(seg["text"])
    return out


def assign_photos(segments: list[dict], timeline: list[dict]) -> dict[str, dict[str, list[str]]]:
    """{event_id: {media_id: [what was said while that photo was open full screen]}}."""
    out: dict[str, dict[str, list[str]]] = {}
    for seg in segments if timeline else []:
        e = _entry(seg, timeline)
        if e.get("item"):
            out.setdefault(e["event_id"], {}).setdefault(e["item"], []).append(seg["text"])
    return out


def _fallback(item: dict) -> dict:
    """A draft without AI: their words as they are."""
    text = item["said"]
    words = len(text.split())
    low = text.lower()
    skip = item.get("mark") == "skip" or (words < 12 and any(w in low for w in SKIP_WORDS))
    return {"event_id": item["event_id"], "story": text, "meaning": "", "feeling": [],
            "importance": 1 if skip or words < 15 else 2 if words < 60 else 3 if words < 150 else 4,
            "people": [p for p in item["people_seen"] if p.lower() in low], "era": "",
            "content": not skip, "angle": ""}


def ai_name(cfg: Config) -> str | None:
    from . import claude_code

    if claude_code.auth_status().get("logged_in"):
        return "claude-code"
    try:
        from . import gemini

        gemini.api_key(cfg)
        return "gemini"
    except Exception:  # noqa: BLE001 - no key: drafts are made without AI
        return None


def _draft_batch(cfg: Config, ai: str, items: list[dict], context: dict) -> list[dict]:
    prompt = json.dumps({"known_chapters": context["eras"], "known_people": context["people"],
                         "events": [{k: v for k, v in it.items() if k not in ("mark", "thumb", "photos")}
                                    for it in items]},
                        ensure_ascii=False, indent=1)
    if ai == "claude-code":
        from . import claude_code

        data, _ = claude_code.ask(cfg, [{"type": "text", "text": prompt}], SCHEMA, SYSTEM)
    else:
        from . import gemini

        data = gemini.ask_json(cfg, [prompt], SCHEMA, system=SYSTEM)
    by_id = {m.get("event_id"): m for m in (data or {}).get("memories", [])}
    out = []
    for it in items:
        m = by_id.get(it["event_id"])
        if not m:
            out.append(_fallback(it))
            continue
        m["importance"] = max(1, min(5, int(m.get("importance") or 1)))
        if it.get("mark") == "skip":
            m["content"] = False
        out.append(m)
    return out


def _wav(src: Path, dest: Path) -> None:
    r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                        str(dest)], capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or not dest.exists():
        raise RuntimeError(f"couldn't read the recording: {r.stderr.strip()[-200:]}")


def process(cfg: Config, sid: str, mode: str = "auto") -> dict:
    """Transcribe -> split by event -> drafts. Runs in a background thread (see finish)."""
    from .db import connect
    from .memories import eras, event_detail, whisper_segments

    s = load(cfg, sid)

    def status(state: str, message: str) -> None:
        s.update(state=state, message=message)
        _save(cfg, s)

    audio = _dir(cfg) / f"{sid}.audio"
    conn = connect(cfg.db_path, threads=True)
    try:
        segments: list[dict] = s.get("segments") or []
        if not segments and audio.exists() and audio.stat().st_size > 1500:
            status("processing", "Turning your voice into text (Whisper, on this Mac)…")
            with tempfile.TemporaryDirectory(prefix="crag-story-") as d:
                wav = Path(d) / "story.wav"
                _wav(audio, wav)
                segments = whisper_segments(cfg, wav, mode)
            s["segments"] = segments
        said = assign(segments, s["timeline"])
        by_photo = assign_photos(segments, s["timeline"])
        ids = list(dict.fromkeys([t["event_id"] for t in s["timeline"]]))
        items = []
        for eid in ids:
            text, mark = " ".join(said.get(eid, [])).strip(), s["marks"].get(eid)
            if not text and mark != "skip":
                continue  # shown but nothing said: stays untold
            ev = event_detail(cfg, conn, eid)
            if ev is None:
                continue
            number = {it["id"]: k + 1 for k, it in enumerate(ev["items"])}
            photos = [{"media_id": mid, "n": number.get(mid), "said": " ".join(t).strip()}
                      for mid, t in by_photo.get(eid, {}).items() if mid in number]
            items.append({"event_id": eid, "date": ev["date"], "time": ev["time"], "place": ev["place"],
                          "footage": [{"n": k + 1, "kind": it["kind"], "time": it["time"], "what": it["title"]}
                                      for k, it in enumerate(ev["items"][:40])],
                          "about_photo": [{"n": x["n"], "said": x["said"]} for x in photos],
                          "photos": photos,
                          "people_seen": ev["people_seen"], "said": text or "(pressed: not important)",
                          "already_told": (ev["memory"] or {}).get("story", ""), "mark": mark,
                          "thumb": next((i["thumb"] for i in ev["items"] if i["thumb"]), None)})
        ai = ai_name(cfg)
        s["ai"] = ai
        people = [r[0] for r in conn.execute("SELECT DISTINCT name FROM people WHERE name IS NOT NULL AND hidden=0 "
                                             "ORDER BY name LIMIT 300")]
        context = {"eras": eras(cfg), "people": people}
        drafts, failed = [], None
        for k in range(0, len(items), BATCH):
            batch = items[k:k + BATCH]
            if ai and not failed:
                status("processing", f"Writing your memories with {'Claude' if ai == 'claude-code' else 'Gemini'} "
                                     f"({min(k + BATCH, len(items))} of {len(items)})…")
                try:
                    drafts += _draft_batch(cfg, ai, batch, context)
                    continue
                except Exception as e:  # noqa: BLE001 - keep their words; the AI can tidy them later
                    failed = str(e)[:300]
            drafts += [_fallback(it) for it in batch]
        for d, it in zip(drafts, items):
            d.update(told=it["said"] if it["said"] != "(pressed: not important)" else "", date=it["date"],
                     time=it["time"], place=it["place"], thumb=it["thumb"], photos=it["photos"])
            d["feeling"] = ", ".join(d.get("feeling") or []) if isinstance(d.get("feeling"), list) else d.get("feeling", "")
        s["drafts"] = drafts
        msg = f"{len(drafts)} memories ready to check" + (f" (AI unavailable, your words kept as said: {failed})"
                                                           if failed else "" if ai else " (no AI set up: your words kept as said)")
        status("review", msg)
        return s
    except Exception as e:  # noqa: BLE001 - shown in the page; the session can be retried
        status("error", f"{e}")
        raise
    finally:
        conn.close()


def finish(cfg: Config, sid: str, timeline: list, marks: dict, mode: str = "auto") -> dict:
    """Stop recording and process in the background (the page polls the session)."""
    update(cfg, sid, timeline, marks)
    with _lock:
        s = load(cfg, sid)
        if s["id"] in _running and _running[s["id"]].is_alive():
            return {"ok": True, "state": "processing"}
        s.update(state="processing", message="Starting…")
        _save(cfg, s)

    def run():
        try:
            process(cfg, sid, mode)
        except Exception:  # noqa: BLE001 - state 'error' is saved for the page
            pass

    t = threading.Thread(target=run, daemon=True)
    _running[sid] = t
    t.start()
    return {"ok": True, "state": "processing"}


def approve(cfg: Config, conn, sid: str, drafts: list[dict]) -> dict:
    """Save the (possibly edited) drafts as Memories notes."""
    from .memories import save_memory

    s = load(cfg, sid)
    saved, errors = 0, []
    for d in drafts:
        if not d.get("save", True):
            continue
        try:
            feeling = d.get("feeling") or ""
            save_memory(cfg, conn, d["event_id"], {
                "story": d.get("story", ""), "meaning": d.get("meaning", ""), "angle": d.get("angle", ""),
                "feeling": ", ".join(feeling) if isinstance(feeling, list) else feeling,
                "importance": d.get("importance") or 0, "people": d.get("people") or [], "era": d.get("era", ""),
                "content": d.get("content", True) is not False, "told": d.get("told", ""), "source": "story mode",
                "photos": d.get("photos") or []})
            saved += 1
        except ValueError as e:
            errors.append(f"{d.get('event_id')}: {e}")
    s.update(state="saved", message=f"{saved} memories saved")
    _save(cfg, s)
    (_dir(cfg) / f"{sid}.audio").unlink(missing_ok=True)  # the words are in the notes now
    return {"ok": True, "saved": saved, "errors": errors}
