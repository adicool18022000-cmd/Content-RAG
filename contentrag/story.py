"""Story mode: talk through your life while a slideshow of your events plays, one recording per session.

The page shows one event at a time (date, place, its photos/videos) and moves on by itself when you
stop talking. It records continuously and uploads the audio every few seconds plus the timeline of
which event was on screen when; a crash loses at most a few seconds and the session can be finished
later. When you press Finish:

1. Whisper on this Mac turns the recording into text in pieces of ~28 s (silence skipped), with the
   language set (Hindi + English or English, never guessed) and the places and people of the events on
   screen as vocabulary (voice.py removes invented lines, loops and wrong scripts); each sentence goes to
   the event that was on screen while it was said,
2. an AI retells each event's words as a memory draft (the whole story, not a summary, plus what it
   meant, feeling, importance, people, chapter, for content or not, open questions): Claude through your
   Claude Code login if available, else Gemini, else your words are kept as they are,
3. you review the drafts on one screen (and can move a story to the event it really belongs to) and
   save them all as Memories notes.

Sessions live in library_dir/story/<id>.json (+ .audio while recording). Saved recordings are kept in
library_dir/story/audio/, so a memory can be transcribed again with a better model later.
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

VOICE_VERSION = 2   # sessions transcribed by an older version are transcribed again (if the audio is there)
BATCH_CHARS = 9000  # words said per AI call: long stories go alone, so nothing gets squeezed
BATCH_ITEMS = 8
SHORT = 0.35        # a story shorter than this share of the words said is a summary: asked again
SKIP_WORDS = ("not important", "skip", "nothing", "don't use", "do not use", "personal", "kuch nahi",
              "important nahi", "mat daalna", "chhodo", "chodo")

SYSTEM = """You turn a person's spoken memories into memory notes for their private life archive.
They looked at the photos/videos of one event at a time and talked about it. Their words ("said") come from
voice-to-text of Hindi, English or both mixed: expect wrong words, names written the way they sound, broken
sentences, and [unclear] where the audio couldn't be understood. For every event you also get the date, place,
what the footage shows and the people recognised in it, plus a vocabulary of the names, places and words of
their life (with known mishearings, "wrong": "right").

story ("What happened") is a faithful, complete retelling of everything they said. It is NOT a summary:
- first person ("I ..."), natural English, in the order they told it, in paragraphs.
- keep every detail: names, places, dates, numbers, who said what, side stories, jokes, small moments, what they
  felt and why. A long telling becomes a long story: ten minutes of talking is many paragraphs. Leave out only
  filler (um, matlab, you know, basically), repeats and voice-to-text garbage.
- translate Hindi/Hinglish into English; keep a short Hindi phrase in Roman letters (with its meaning) only when
  the exact words matter.
- fix a voice-to-text mistake only when the vocabulary, the known people/places or the footage make the right word
  clear (e.g. "Mania" -> Manya, "hatty" -> IIT, "Kurnia" -> Purnia when those are in the vocabulary). If you can't
  tell, keep their word in quotes or write [unclear]. Never guess.
- never add anything they didn't say: no new names, schools, colleges, cities, hostels, relationships, reasons or
  feelings. Never merge two names into one person. Never change a place or institution (IIT vs IIIT, which city).
- if they talk about another time (a flashback, what came later), keep it and say when it was.
questions: short questions to ask them later about what is unclear, contradictory or missing (a name that couldn't
  be read, which college or city, who someone is, an [unclear] part that matters). [] if nothing.
meaning: one or two first-person lines on what it meant to them, only if they expressed it; else "".
feeling: 0-3 words from what they said (e.g. happy, proud, free, nervous, nostalgic, sad).
importance 1-5 (1 just a day, 3 meaningful, 5 changed my life), from what and how much they said.
people: names they mentioned, spelled as in the known people / vocabulary when it is clearly the same person.
era: the chapter of life it belongs to: one of the known chapters when it fits; otherwise a short new name only if
  they made it clear (e.g. "School in Patna"), else "".
content: false if they said it's not important, private, or not to be used, or said only "skip"; else true.
angle: a one-line reel idea only if the story clearly has one; else "".
The footage list is numbered as on their screen; they may say "photo 3 is..." or "first picture...". about_photo is
what they said while that numbered photo was open full screen. Use both to understand which picture is which.
If an event has already_told (their earlier story of it), write one story that keeps everything from it and adds
the new words. If an event has too_short, your earlier version of it was a summary: retell everything.
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
            "questions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["event_id", "story", "meaning", "feeling", "importance", "people", "era", "content", "angle",
                     "questions"],
        "additionalProperties": False}}},
    "required": ["memories"], "additionalProperties": False,
}
# what the AI gets of each event (the rest is for the page)
SENT = ("event_id", "date", "time", "place", "footage", "about_photo", "people_seen", "said", "already_told",
        "too_short")

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
                          "running": s["id"] in _running and _running[s["id"]].is_alive(),
                          "outdated": _outdated(cfg, s)})
    return out


def _outdated(cfg: Config, s: dict) -> bool:
    """Drafts made with the old voice-to-text while the recording is still here: worth doing again."""
    return s.get("state") == "review" and s.get("voice") != VOICE_VERSION and (_dir(cfg) / f"{s['id']}.audio").exists()


def info(cfg: Config, sid: str) -> dict:
    s = load(cfg, sid)
    s.pop("partial", None)
    return s | {"outdated": _outdated(cfg, s), "has_audio": (_dir(cfg) / f"{sid}.audio").exists()}


def discard(cfg: Config, sid: str) -> None:
    s = load(cfg, sid)
    s["state"] = "discarded"
    _save(cfg, s)
    (_dir(cfg) / f"{sid}.audio").unlink(missing_ok=True)


# ---------------------------------------------------------------- processing

def _entry(seg: dict, timeline: list[dict]) -> dict:
    """The timeline entry (event, or one photo of it opened full screen) on screen for most of a segment.
    A segment transcribed from one event's part of the recording stays with that event."""
    if seg.get("event_id"):
        timeline = [t for t in timeline if t["event_id"] == seg["event_id"]] or timeline
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
        out.setdefault(seg.get("event_id") or _entry(seg, timeline)["event_id"], []).append(seg["text"])
    return out


def assign_photos(segments: list[dict], timeline: list[dict]) -> dict[str, dict[str, list[str]]]:
    """{event_id: {media_id: [what was said while that photo was open full screen]}}."""
    out: dict[str, dict[str, list[str]]] = {}
    for seg in segments if timeline else []:
        e = _entry(seg, timeline)
        if e.get("item"):
            out.setdefault(e["event_id"], {}).setdefault(e["item"], []).append(seg["text"])
    return out


def runs(timeline: list[dict]) -> list[dict]:
    """The timeline as stretches of one event each (photos opened full screen stay part of their event)."""
    out: list[dict] = []
    for t in sorted(timeline, key=lambda t: t["start"]):
        if out and out[-1]["event_id"] == t["event_id"]:
            out[-1]["end"] = max(out[-1]["end"], t["end"])
        else:
            out.append({"event_id": t["event_id"], "start": t["start"], "end": t["end"]})
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
            "content": not skip, "angle": "", "questions": []}


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


def ask_ai(cfg: Config, ai: str, prompt: str, schema: dict, system: str) -> dict:
    if ai == "claude-code":
        from . import claude_code

        data, _ = claude_code.ask(cfg, [{"type": "text", "text": prompt}], schema, system)
        return data or {}
    from . import gemini

    return gemini.ask_json(cfg, [prompt], schema, system=system) or {}


def _draft_batch(cfg: Config, ai: str, items: list[dict], context: dict) -> list[dict]:
    prompt = json.dumps({"vocabulary": context.get("vocabulary", {}), "known_chapters": context["eras"],
                         "known_people": context["people"],
                         "events": [{k: it[k] for k in SENT if k in it} for it in items]},
                        ensure_ascii=False, indent=1)
    data = ask_ai(cfg, ai, prompt, SCHEMA, SYSTEM)
    by_id = {m.get("event_id"): m for m in data.get("memories", [])}
    out = []
    for it in items:
        m = by_id.get(it["event_id"])
        if not m:
            out.append(_fallback(it))
            continue
        m["importance"] = max(1, min(5, int(m.get("importance") or 1)))
        m.setdefault("questions", [])
        if it.get("mark") == "skip":
            m["content"] = False
        out.append(m)
    return out


def batches(items: list[dict], max_chars: int = BATCH_CHARS, max_items: int = BATCH_ITEMS) -> list[list[dict]]:
    """Groups for the AI by how much was said: a long story gets a call of its own."""
    out: list[list[dict]] = []
    size = 0
    for it in items:
        n = 600 + len(it.get("said") or "") + len(it.get("already_told") or "") \
            + sum(len(x.get("said") or "") for x in it.get("about_photo") or [])
        if not out or size + n > max_chars or len(out[-1]) >= max_items:
            out.append([])
            size = 0
        out[-1].append(it)
        size += n
    return out


def too_short(item: dict, draft: dict) -> bool:
    from .voice import words

    said = words(item.get("said", "")) + words(item.get("already_told", ""))
    return said >= 120 and words(draft.get("story", "")) < SHORT * said


def draft_all(cfg: Config, ai: str | None, items: list[dict], context: dict, status=None) -> tuple[list[dict], str | None]:
    """AI drafts for all items (their words as said when there's no AI or it fails). A draft that came back
    as a summary of a long telling is asked for again, alone."""
    from .voice import words

    drafts, failed = [], None
    groups = batches(items)
    done = 0
    for batch in groups:
        done += len(batch)
        if ai and not failed:
            if status:
                status(f"Writing your memories with {'Claude' if ai == 'claude-code' else 'Gemini'} "
                       f"({done} of {len(items)})…")
            try:
                got = _draft_batch(cfg, ai, batch, context)
                for k, (it, d) in enumerate(zip(batch, got)):
                    if too_short(it, d):
                        again = _draft_batch(cfg, ai, [dict(it, too_short=(
                            f"Your last version had {words(d.get('story', ''))} words for {words(it['said'])} "
                            "words said. That is a summary: retell everything they said."))], context)[0]
                        if words(again.get("story", "")) > words(d.get("story", "")):
                            got[k] = again
                drafts += got
                continue
            except Exception as e:  # noqa: BLE001 - keep their words; the AI can tidy them later
                failed = str(e)[:300]
        drafts += [_fallback(it) for it in batch]
    return drafts, failed


def _wav(src: Path, dest: Path) -> None:
    r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                        str(dest)], capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or not dest.exists():
        raise RuntimeError(f"couldn't read the recording: {r.stderr.strip()[-200:]}")


def _clock(t: float) -> str:
    t = int(t)
    return f"{t // 60}:{t % 60:02d}"


def _transcribe(cfg: Config, conn, s: dict, audio: Path, mode: str, status, quality: str = "fast") -> list[dict]:
    """The recording in pieces of at most ~28 s (cut at quiet moments, silence skipped), each with the names
    of the events on screen during it as vocabulary; every sentence then goes to the event that was on
    screen. Saved as it goes: an interrupted session continues where it stopped."""
    from . import voice
    from .memories import event_terms

    if not s["timeline"]:
        return []
    status("processing", "Turning your voice into text on this Mac…")
    with tempfile.TemporaryDirectory(prefix="crag-story-") as d:
        wav = Path(d) / "story.wav"
        _wav(audio, wav)
        samples = voice.load_wav(wav)
    total = len(samples) / voice.SR
    pieces = voice.speech_pieces(samples)
    parts = runs(s["timeline"])
    vocab = voice.vocabulary(cfg, conn)
    terms = voice.vocab_terms(vocab)
    partial = s.get("partial") or {}
    if (partial.get("voice"), partial.get("mode"), partial.get("quality")) == (VOICE_VERSION, mode, quality):
        segs, start = partial["segments"], partial["pieces"]
    else:
        segs, start = [], 0
    names: dict[str, list[str]] = {}
    t0, said = time.time(), 0.0
    first = not voice._model_ok and quality == "best"
    for k in range(start, len(pieces)):
        a, b = pieces[k]
        left = ""
        if said > 20:  # after a bit: how long the rest will take at this speed
            mins = sum(y - x for x, y in pieces[k:]) * (time.time() - t0) / said / 60
            left = f", about {max(1, round(mins))} min left"
        status("processing", f"Turning your voice into text on this Mac: {_clock(a)} of {_clock(total)}{left}…"
               + (" The first time, the accurate voice model is downloaded (about 3 GB)." if first else ""))
        on_screen = list(dict.fromkeys(r["event_id"] for r in parts if r["start"] < b and r["end"] > a))
        extra = [t for eid in on_screen[:4] for t in names.setdefault(eid, event_terms(cfg, conn, eid))]
        for x in voice.transcribe_piece(cfg, samples, a, b, mode, voice.prompt_for(vocab, extra), terms, quality):
            segs.append(dict(x, event_id=_entry(x, s["timeline"])["event_id"]))
        said += b - a
        first = False
        s["partial"] = {"voice": VOICE_VERSION, "mode": mode, "quality": quality, "pieces": k + 1, "segments": segs}
        _save(cfg, s)
    return voice.merge_unclear(segs)


def process(cfg: Config, sid: str, mode: str = "mixed", quality: str = "fast") -> dict:
    """Transcribe -> split by event -> drafts. Runs in a background thread (see finish)."""
    from . import voice
    from .db import connect
    from .memories import eras, event_detail, nearby_events, suggest_move

    s = load(cfg, sid)

    def status(state: str, message: str) -> None:
        s.update(state=state, message=message)
        _save(cfg, s)

    audio = _dir(cfg) / f"{sid}.audio"
    conn = connect(cfg.db_path, threads=True)
    try:
        segments: list[dict] = s.get("segments") or []
        if s.get("voice") != VOICE_VERSION and audio.exists() and audio.stat().st_size > 1500:
            segments = _transcribe(cfg, conn, s, audio, mode, status, quality)
            s.update(segments=segments, voice=VOICE_VERSION)
            s.pop("partial", None)
            _save(cfg, s)
        elif s.get("voice") != VOICE_VERSION:
            segments = voice.clean_segments(segments)  # the recording is gone: at least clean the old text
        said = assign(segments, s["timeline"])
        by_photo = assign_photos(segments, s["timeline"])
        spans: dict[str, list] = {}
        for r in runs(s["timeline"]):
            spans.setdefault(r["event_id"], []).append([round(r["start"], 1), round(r["end"], 1)])
        ids = list(dict.fromkeys([t["event_id"] for t in s["timeline"]]))
        items = []
        for eid in ids:
            text, mark = " ".join(said.get(eid, [])).strip(), s["marks"].get(eid)
            if text == voice.UNCLEAR:
                text = ""
            if not text and mark != "skip":
                continue  # shown but nothing said: stays untold
            ev = event_detail(cfg, conn, eid, show_hidden_people=True)  # numbered as on screen
            if ev is None:
                continue
            number = {it["id"]: k + 1 for k, it in enumerate(ev["items"])}
            photos = [{"media_id": mid, "n": number.get(mid), "said": " ".join(t).strip()}
                      for mid, t in by_photo.get(eid, {}).items() if mid in number]
            mem = ev["memory"] or {}
            retold = bool(mem.get("retell"))  # the old version was too damaged: not used as already told
            items.append({"event_id": eid, "date": ev["date"], "time": ev["time"], "place": ev["place"],
                          "footage": [{"n": k + 1, "kind": it["kind"], "time": it["time"], "what": it["title"]}
                                      | ({"has_hidden_person": True} if it["hidden_people"] else {})
                                      for k, it in enumerate(ev["items"][:40])],
                          "about_photo": [{"n": x["n"], "said": x["said"]} for x in photos],
                          "photos": photos,
                          "people_seen": ev["people_seen"], "said": text or "(pressed: not important)",
                          "already_told": "" if retold else mem.get("story", ""), "mark": mark, "retold": retold,
                          "thumb": next((i["thumb"] for i in ev["items"] if i["thumb"]), None)})
        ai = ai_name(cfg)
        s["ai"] = ai
        vocab = voice.vocabulary(cfg, conn)
        context = {"eras": eras(cfg), "people": vocab["people"][:300],
                   "vocabulary": {"words": vocab["terms"], "mishearings": vocab["fixes"],
                                  "places": vocab["places"][:60], "folders": vocab["folders"][:30]}}
        drafts, failed = draft_all(cfg, ai, items, context, lambda m: status("processing", m))
        for d, it in zip(drafts, items):
            told = it["said"] if it["said"] != "(pressed: not important)" else ""
            d.update(told=told, date=it["date"], time=it["time"], place=it["place"], thumb=it["thumb"],
                     photos=it["photos"], told_on=it["event_id"], spans=spans.get(it["event_id"], []),
                     retold=it["retold"], nearby=nearby_events(cfg, conn, it["event_id"]),
                     suggest=suggest_move(cfg, conn, it["event_id"], f"{d.get('story', '')}\n{told}") if told else None)
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


def finish(cfg: Config, sid: str, timeline: list, marks: dict, mode: str = "mixed", redo: bool = False,
           quality: str = "fast") -> dict:
    """Stop recording and process in the background (the page polls the session). redo: transcribe the
    recording again (e.g. drafts made with the old voice-to-text, or another language picked)."""
    update(cfg, sid, timeline, marks)
    with _lock:
        s = load(cfg, sid)
        if s["id"] in _running and _running[s["id"]].is_alive():
            return {"ok": True, "state": "processing"}
        if redo and (_dir(cfg) / f"{sid}.audio").exists():
            for k in ("segments", "partial", "voice"):
                s.pop(k, None)
        s.update(state="processing", message="Starting…")
        _save(cfg, s)

    def run():
        try:
            process(cfg, sid, mode, quality)
        except Exception:  # noqa: BLE001 - state 'error' is saved for the page
            pass

    t = threading.Thread(target=run, daemon=True)
    _running[sid] = t
    t.start()
    return {"ok": True, "state": "processing"}


AUDIO_EXT = {"audio/webm": ".webm", "audio/mp4": ".m4a", "audio/ogg": ".ogg", "audio/wav": ".wav"}


def _keep_audio(cfg: Config, s: dict) -> str | None:
    """The recording moves to library_dir/story/audio (kept: it can be transcribed again with a better model).
    Returns its path relative to library_dir."""
    if s.get("audio_file"):
        return s["audio_file"]
    src = _dir(cfg) / f"{s['id']}.audio"
    if not src.exists():
        return None
    dest = _dir(cfg) / "audio" / f"{s['created'][:10]} {s['id']}{AUDIO_EXT.get(s.get('mime', '').split(';')[0], '.webm')}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dest)
    s["audio_file"] = dest.relative_to(cfg.library_dir).as_posix()
    return s["audio_file"]


def _combine(ds: list[dict]) -> dict:
    """One memory from one or more drafts (stories told on different slides that belong to one event)."""
    join = lambda key: "\n\n".join(str(d.get(key) or "").strip() for d in ds if str(d.get(key) or "").strip())  # noqa: E731
    feelings, people, photos, questions = [], [], [], []
    for d in ds:
        f = d.get("feeling") or ""
        feelings += [x.strip() for x in (f if isinstance(f, list) else f.split(",")) if x.strip() and x.strip() not in feelings]
        people += [p for p in d.get("people") or [] if p not in people]
        photos += d.get("photos") or []
        questions += [q for q in d.get("questions") or [] if q not in questions]
    return {"story": join("story"), "meaning": join("meaning"), "angle": join("angle"), "feeling": ", ".join(feelings),
            "importance": max(int(d.get("importance") or 0) for d in ds), "people": people,
            "era": next((d["era"] for d in ds if d.get("era")), ""), "content": any(d.get("content", True) is not False for d in ds),
            "told": join("told"), "source": "story mode", "photos": photos, "questions": questions,
            "retold": any(d.get("retold") for d in ds)}


def approve(cfg: Config, conn, sid: str, drafts: list[dict]) -> dict:
    """Save the (possibly edited) drafts as Memories notes. A draft whose 'Belongs to' was changed goes to that
    event; drafts that end up on the same event are combined into one memory."""
    from .memories import _find, load_memory, save_memory

    s = load(cfg, sid)
    audio = _keep_audio(cfg, s)
    groups: dict[str, list[dict]] = {}
    for d in drafts:
        if d.get("save", True):
            groups.setdefault(str(d.get("event_id")), []).append(d)
    saved, errors = 0, []
    for eid, ds in groups.items():
        if all(d.get("told_on", eid) != eid for d in ds):  # moved onto an event: keep the story it already has
            ev = _find(cfg, conn, eid)
            old = load_memory(cfg, ev) if ev else None
            if old:
                ds = [{k: old.get(k) for k in ("story", "meaning", "angle", "feeling", "importance", "people", "era",
                                                "content")}] + ds
        data = _combine(ds)
        spans = [sp for d in ds for sp in d.get("spans") or []]
        if audio and spans:
            data["recording"] = {"file": audio, "spans": spans}
        try:
            save_memory(cfg, conn, eid, data)
            saved += 1
        except ValueError as e:
            errors.append(f"{eid}: {e}")
    s.update(state="saved", message=f"{saved} memories saved")
    _save(cfg, s)
    return {"ok": True, "saved": saved, "errors": errors}
