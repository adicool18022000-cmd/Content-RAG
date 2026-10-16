"""crag memories repair: fix the memories saved before the voice fixes.

Before voice.py, Whisper guessed the language and often wrote Hindi/Hinglish as Korean, Russian or Italian,
looped, invented "Thank you." and misheard names; the slideshow moved on after 4 quiet seconds, so stories
landed on the wrong event (or were split over several slides); and the AI wrote a short summary. The
recordings of saved sessions were deleted then, so the repair works from the text in the notes:

1. Placement: for every memory told in Story mode before the fix, the AI reads what was said (as numbered
   sentences) next to the events just before and after it, and the end of what was said on the event
   before. Sentences that clearly belong to another event (they name its place or occasion, or plainly
   continue the story before) are moved there; a memory that is entirely about another event is moved
   whole and combined with what's there.
2. 'As told (voice)' is cleaned: wrong-script and wrong-language lines, loops, "foreign" and invented lines
   are removed ([unclear] marks where words were lost).
3. 'What happened' is written again as a complete retelling of the words (not a summary), with your
   vocabulary and known names, for every memory told in Story mode before the fix, every damaged one,
   and every one that was moved. What the AI can't make out goes to 'Open questions'.
4. Memories where most of the words were lost are marked `needs_retelling: yes` (Story mode can play just
   those).

Nothing is lost: every note is copied as it was to Memories/_before repair/<time>/ before its first change
(`crag memories repair --undo` puts the last run back), and the earlier text also stays in the note under
'Earlier versions (voice-to-text errors)'. Each batch is saved as soon as it's done: a stopped or interrupted
run continues where it was. Claude usage limits are waited out. Report: LifeVault/_generated/Memory repair.md.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from .config import Config
from .memories import EARLIER

SYSTEM = """You repair memory notes of a person's private life archive. Each note was written earlier by an AI from
voice-to-text of what they said (Hindi, English or both mixed). That voice-to-text was often damaged: parts came out
in wrong scripts or languages, it looped, invented lines and misheard names, and the AI wrote a short summary. For
each note you get:
- as_told: what they said, cleaned: wrong-script/wrong-language lines, loops and invented lines are removed;
  [unclear] marks where words were lost.
- earlier_draft: the story written from the damaged text. It may contain errors that came from mishearing (a wrong
  college, city or hostel, two people merged into one, invented details). It may also contain corrections they
  typed by hand.
- the date, place, footage, people recognised, their earlier people list, and a vocabulary of the names, places and
  words of their life (with known mishearings, "wrong": "right").

Write for each note:
story: a faithful, complete retelling of everything in as_told, in first-person English ("I ..."), in the order
  told, in paragraphs. NOT a summary: keep every detail (names, places, numbers, who said what, side stories, jokes,
  feelings and why). A long telling gives a long story. Translate Hindi/Hinglish into English. Fix a misheard word
  only when the vocabulary, the known people/places or the footage make it clear; else keep their word in quotes or
  write [unclear]. Use a fact from earlier_draft only if as_told supports it, or it is plainly a hand correction that
  doesn't contradict as_told; when they disagree, follow as_told and ask in questions. Never add names, schools,
  colleges, cities, hostels, relationships, reasons or feelings that aren't there. Never merge two names into one
  person.
questions: what they should clear up: names or places that couldn't be read, which institution, parts lost as
  [unclear] that seem to matter, contradictions between as_told and earlier_draft. [] if nothing.
people: the people named in the story, spelled as in the vocabulary / known people when clearly the same person.
If an entry has written_story, they typed that story themselves: keep everything in it and add what as_told adds.
If an entry has moved_out, those parts of what they said were moved to other events' memories: leave them out,
even if earlier_draft has them. If an entry has too_short, your earlier version of it was a summary: retell
everything.
Return one entry per id."""

SCHEMA = {
    "type": "object",
    "properties": {"memories": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "story": {"type": "string"},
                       "questions": {"type": "array", "items": {"type": "string"}},
                       "people": {"type": "array", "items": {"type": "string"}}},
        "required": ["id", "story", "questions", "people"], "additionalProperties": False}}},
    "required": ["memories"], "additionalProperties": False,
}
SENT = ("id", "date", "place", "footage", "people_seen", "earlier_people", "as_told", "earlier_draft",
        "written_story", "moved_out", "too_short")

PLACE_SYSTEM = """You check which event spoken memories belong to. The person watched a slideshow of their life, one event
at a time, and talked. The old slideshow moved on after a few quiet seconds, so it sometimes skipped a big event before
they started talking, or moved on while they were still telling a story: a story can sit on the wrong event, or be
split over the next few events.

For each memory you get the event it is saved on (saved_on), the events just before and after it (candidates: date,
place, what their photos/videos show, how many), what was said on the event before it (said_just_before), and what
they said, as numbered sentences (voice-to-text of Hindi/English, with errors).

Split the sentences into consecutive ranges and give each range the event it is about:
- keep it on saved_on unless the words clearly fit a candidate better: they name its place, occasion, people or
  something its footage shows, or they plainly continue the story in said_just_before about that candidate.
- general talk (feelings, life at the time) stays on saved_on.
- sure: true only when the words make it clear. Ranges that aren't sure stay where they are.
- why: a few words (e.g. "names Varanasi and the aarti", "continues the wedding story").
Cover every sentence exactly once, in order. Most memories are a single range on saved_on."""

PLACE_SCHEMA = {
    "type": "object",
    "properties": {"notes": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "ranges": {"type": "array", "items": {
            "type": "object",
            "properties": {"from": {"type": "integer"}, "to": {"type": "integer"}, "event": {"type": "string"},
                           "sure": {"type": "boolean"}, "why": {"type": "string"}},
            "required": ["from", "to", "event", "sure", "why"], "additionalProperties": False}}},
        "required": ["id", "ranges"], "additionalProperties": False}}},
    "required": ["notes"], "additionalProperties": False,
}
PLACE_SENT = ("id", "saved_on", "candidates", "said_just_before", "sentences")

RETELL_LOST = 0.4   # this much of what was said lost: tell it again
SHORT = 0.5         # a story shorter than half of the words said lost too much in the summary
TOLD = "As told (voice)"
BACKUPS = "_before repair"
NEAR = 4            # events before/after a memory that it may really belong to


# ---------------------------------------------------------------- editing a note's text

def _split(text: str) -> tuple[str, str]:
    """(frontmatter block incl. both ---, the rest)."""
    if text.startswith("---") and "\n---" in text[3:]:
        end = text.index("\n---", 3) + 4
        nl = text.find("\n", end)
        end = len(text) if nl < 0 else nl + 1
        return text[:end], text[end:]
    return "", text


def set_fm(text: str, key: str, value: str | None) -> str:
    fm, body = _split(text)
    if not fm:
        fm = "---\n---\n"
    lines = fm.rstrip("\n").split("\n")
    idx = next((k for k, ln in enumerate(lines[1:-1], 1) if re.match(rf"{re.escape(key)}\s*:", ln)), None)
    if value is None:
        if idx is not None:
            del lines[idx]
    elif idx is not None:
        lines[idx] = f"{key}: {value}"
    else:
        lines.insert(len(lines) - 1, f"{key}: {value}")
    return "\n".join(lines) + "\n" + body


def get_section(text: str, heading: str) -> str | None:
    m = re.search(rf"^## +{re.escape(heading)}[ \t]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else None


def set_section(text: str, heading: str, body: str) -> str:
    pat = re.compile(rf"^## +{re.escape(heading)}[ \t]*\n.*?(?=^## |\Z)", re.M | re.S)
    block = f"## {heading}\n{body.strip()}\n\n"
    if pat.search(text):
        return pat.sub(lambda _: block, text, count=1)
    return text.rstrip("\n") + "\n\n" + block


def add_lines(text: str, heading: str, lines: list[str]) -> str:
    old = get_section(text, heading)
    have = (old or "").splitlines()
    new = have + [ln for ln in lines if ln not in have]
    return set_section(text, heading, "\n".join(new)) if new != have or old is None else text


def sentences(text: str, longest: int = 60) -> list[str]:
    """Sentences of a (cleaned) telling; very long ones (no punctuation) in pieces of 40 words."""
    from .voice import SENTENCE

    out: list[str] = []
    for para in re.split(r"\n\s*\n", text or ""):
        for s in SENTENCE.split(para.strip()):
            words = s.split()
            if len(words) > longest:
                out += [" ".join(words[i:i + 40]) for i in range(0, len(words), 40)]
            elif words:
                out.append(" ".join(words))
    return out


# ---------------------------------------------------------------- backups / undo

class Backup:
    """Copies each note as it was before its first change in this run; a manifest says what to put back."""

    def __init__(self, cfg: Config):
        self.vault = cfg.vault_dir
        self.dir = _free(cfg.vault_dir / "Memories" / BACKUPS / time.strftime("%Y-%m-%d %H.%M.%S"))
        self.m: dict[str, list[str]] = {"changed": [], "created": []}

    def _rel(self, path: Path) -> str:
        return Path(path).resolve().relative_to(self.vault.resolve()).as_posix()

    def save(self, path: Path) -> None:
        rel = self._rel(path)
        if rel in self.m["changed"] or rel in self.m["created"] or not Path(path).exists():
            return
        dest = self.dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        self.m["changed"].append(rel)
        self._write()

    def created(self, path: Path) -> None:
        rel = self._rel(path)
        if rel not in self.m["changed"] and rel not in self.m["created"]:
            self.m["created"].append(rel)
            self._write()

    def _write(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "manifest.json").write_text(json.dumps(self.m, indent=1), encoding="utf-8")


def _free(path: Path) -> Path:
    k, out = 2, path
    while out.exists():
        out = path.with_name(f"{path.name} ({k})")
        k += 1
    return out


def last_backup(cfg: Config) -> dict | None:
    root = cfg.vault_dir / "Memories" / BACKUPS
    runs = sorted((d for d in root.iterdir() if (d / "manifest.json").exists() and "(undone" not in d.name),
                  key=lambda d: (d / "manifest.json").stat().st_mtime) if root.is_dir() else []
    if not runs:
        return None
    m = json.loads((runs[-1] / "manifest.json").read_text(encoding="utf-8"))
    return {"run": runs[-1].name, "dir": runs[-1], "changed": len(m["changed"]), "created": len(m["created"]),
            "manifest": m}


def undo(cfg: Config, conn) -> dict:
    """Put back the notes changed by the last repair run (changes made to them since are lost too)."""
    from .memories import sync_content_flags

    last = last_backup(cfg)
    if not last:
        raise ValueError("there is no repair to undo")
    m, d = last["manifest"], last["dir"]
    for rel in m["created"]:
        (cfg.vault_dir / rel).unlink(missing_ok=True)
    for rel in m["changed"]:
        dest = cfg.vault_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(d / rel, dest)
    d.rename(_free(d.with_name(d.name + " (undone)")))
    _state_path(cfg).unlink(missing_ok=True)  # placement is checked again next time
    sync_content_flags(cfg, conn)
    return {"ok": True, "run": last["run"], "restored": len(m["changed"]), "removed": len(m["created"])}


# ---------------------------------------------------------------- check

def notes(cfg: Config) -> list[Path]:
    root = cfg.vault_dir / "Memories"
    return [p for p in sorted(root.rglob("*.md")) if not any(x.startswith("_") for x in p.relative_to(root).parts)]


def _old_story_mode(text: str, source: str) -> bool:
    """Told in Story mode before the fix: has the voice text but no kept recording."""
    return "story" in (source or "") and bool(get_section(text, TOLD)) and get_section(text, "Recordings") is None


def check(path: Path) -> dict:
    """What's wrong with one memory note. kind: retell | damaged | short | old (told in Story mode before the
    fix: summarised) | ok."""
    from .memories import _read
    from .vault import _frontmatter
    from .voice import clean_text, damaged, words

    text = path.read_text(encoding="utf-8", errors="ignore")
    fm, note = _frontmatter(text), _read(path)
    told = get_section(text, TOLD) or ""
    spoken = told or note["story"]  # Speak button text went straight into the story
    cleaned, stats = clean_text(spoken)
    out = {"path": path, "event_id": fm.get("event_id", ""), "date": fm.get("date", ""), "title": note["title"],
           "source": note["source"], "told": told, "story": note["story"], "cleaned": cleaned, "stats": stats,
           "people": note["people"], "repaired": note["repaired"], "retell": note["retell"], "kind": "ok"}
    if not spoken.strip():
        return out
    if stats["lost"] >= 10 and stats["lost_share"] >= RETELL_LOST:
        out["kind"] = "retell"
    elif damaged(stats):
        out["kind"] = "damaged"
    elif told and words(told) >= 80 and words(note["story"]) < SHORT * words(cleaned):
        out["kind"] = "short"
    elif _old_story_mode(text, note["source"]) and words(cleaned) >= 15:
        out["kind"] = "old"
    return out


# ---------------------------------------------------------------- AI

def _sleep(seconds: float, stop) -> bool:
    """Wait; True if asked to stop meanwhile."""
    end = time.time() + seconds
    while time.time() < end:
        if stop is not None and stop.is_set():
            return True
        time.sleep(min(10.0, max(0.0, end - time.time())))
    return False


def _ask_patient(cfg: Config, ai: str, prompt: str, schema: dict, system: str, log, stop) -> dict:
    """One AI call; usage limits and connection problems are waited out (it continues by itself)."""
    from .story import ask_ai

    for attempt in range(10):
        try:
            return ask_ai(cfg, ai, prompt, schema, system)
        except Exception as e:  # noqa: BLE001 - only waitable problems are retried
            code = getattr(e, "code", None)
            if code not in (429, "network", 500, 502, 503, 504) or attempt == 9:
                raise
            resume = getattr(e, "resume_at", None)
            wait = (resume - time.time() + 60) if resume else (900 if code == 429 else 120)
            wait = min(6 * 3600, max(60.0, wait))
            what = "Claude usage limit reached" if code == 429 else "no connection to the AI"
            log(f"[repair] {what}: waiting until {time.strftime('%H:%M', time.localtime(time.time() + wait))}, "
                "then it continues by itself (or stop it; repaired memories are kept)…")
            if _sleep(wait, stop):
                raise RuntimeError("stopped")
    raise RuntimeError("the AI kept failing")


def _ask(cfg: Config, ai: str, batch: list[dict], context: dict, log=print, stop=None) -> dict[str, dict]:
    prompt = json.dumps(context | {"notes": [{k: it[k] for k in SENT if k in it} for it in batch]},
                        ensure_ascii=False, indent=1)
    data = _ask_patient(cfg, ai, prompt, SCHEMA, SYSTEM, log, stop)
    return {m["id"]: m for m in data.get("memories", []) if m.get("id")}


# ---------------------------------------------------------------- 1. placement

def _state_path(cfg: Config) -> Path:
    return cfg.library_dir / "story" / "placement.json"


def _hash(text: str) -> str:
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]


def _describe(ev: list) -> dict:
    from .memories import _place
    from .vault import _city

    return {"event": ev[0]["id"], "date": ev[0]["taken_at"][:10], "end": ev[-1]["taken_at"][:10],
            "place": _city(_place(ev)) or "", "shows": list(dict.fromkeys(m["title"] for m in ev if m["title"]))[:8],
            "items": len(ev)}


def _label(d: dict) -> str:
    return f"{d['date']} · {d['place'] or (d['shows'] or ['an event'])[0]}"


def _valid_ranges(note: dict | None, n: int, allowed: set[str], own: str) -> list[dict] | None:
    if not note:
        return None
    rs = sorted((r for r in note.get("ranges") or []), key=lambda r: int(r.get("from") or 0))
    k = 1
    for r in rs:
        if int(r.get("from") or 0) != k or int(r.get("to") or 0) < k:
            return None
        k = int(r["to"]) + 1
    if k != n + 1:
        return None
    for r in rs:
        if r.get("event") not in allowed:
            r["event"], r["sure"] = own, False
    return rs


def _place_all(cfg: Config, conn, ai: str, backup: Backup, touched: dict, log, stop, force: bool) -> list[dict]:
    """Step 1: move what was told on the wrong event to the right one. Returns the moves done."""
    from .memories import _events, _read
    from .vault import _frontmatter, event_memory, read_memories
    from .voice import clean_text, words

    state_file = _state_path(cfg)
    memories = read_memories(cfg.vault_dir)
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    evs = _events(cfg, conn)
    index = {m["id"]: k for k, ev in enumerate(evs) for m in ev}
    items = []
    for path in notes(cfg):
        text = path.read_text(encoding="utf-8", errors="ignore")
        fm = _frontmatter(text)
        told = get_section(text, TOLD) or ""
        k = index.get(fm.get("event_id", ""))
        if k is None or not _old_story_mode(text, fm.get("source", "")) or words(told) < 15:
            continue
        key = path.relative_to(cfg.vault_dir).as_posix()
        if state.get(key) == _hash(told) and not force:
            continue  # checked before and not changed since
        sents = sentences(clean_text(told)[0])
        if not sents:
            continue
        cands = [_describe(evs[j]) | {"has_memory": event_memory(evs[j], memories) is not None}
                 for j in range(max(0, k - NEAR), min(len(evs), k + NEAR + 1)) if j != k]
        before = ""
        prev = event_memory(evs[k - 1], memories) if k > 0 else None
        if prev and (cfg.vault_dir / f"{prev['name']}.md").exists():
            pp = cfg.vault_dir / f"{prev['name']}.md"
            before = (get_section(pp.read_text(encoding="utf-8", errors="ignore"), TOLD) or _read(pp)["story"] or "")[-500:]
        items.append({"id": f"p{len(items)}", "path": path, "key": key, "own": evs[k][0]["id"], "sents": sents,
                      "saved_on": _describe(evs[k]), "candidates": cands, "said_just_before": before,
                      "sentences": [f"{i}. {s}" for i, s in enumerate(sents, 1)], "said": " ".join(sents)})
    if not items:
        return []
    from .story import batches

    log(f"[repair] step 1 of 2: checking which event {len(items)} Story-mode memories really belong to…")
    done: list[dict] = []
    for n, batch in enumerate(batches(items), 1):
        if stop is not None and stop.is_set():
            raise RuntimeError("stopped")
        log(f"[repair] step 1: part {n} ({len(done)} stories moved so far)…")
        prompt = json.dumps({"memories": [{k: it[k] for k in PLACE_SENT} for it in batch]}, ensure_ascii=False, indent=1)
        got = {x.get("id"): x for x in _ask_patient(cfg, ai, prompt, PLACE_SCHEMA, PLACE_SYSTEM, log, stop).get("notes", [])}
        for it in batch:
            allowed = {it["own"]} | {c["event"] for c in it["candidates"]}
            ranges = _valid_ranges(got.get(it["id"]), len(it["sents"]), allowed, it["own"])
            if ranges and any(r["sure"] and r["event"] != it["own"] for r in ranges):
                done += _apply_moves(cfg, conn, it, ranges, backup, touched, log)
            if it["path"].exists():
                state[it["key"]] = _hash(get_section(it["path"].read_text(encoding="utf-8", errors="ignore"), TOLD) or "")
        for key in list(touched):  # moved into: checked with what's in it now
            p = cfg.vault_dir / key
            if p.exists():
                state[key] = _hash(get_section(p.read_text(encoding="utf-8", errors="ignore"), TOLD) or "")
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, indent=1), encoding="utf-8")
    return done


def _apply_moves(cfg: Config, conn, it: dict, ranges: list[dict], backup: Backup, touched: dict, log) -> list[dict]:
    from .memories import _find, _memory_paths, _read, _set_aside, move_memory, save_memory
    from .voice import clean_text

    path, own = it["path"], it["own"]
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="ignore")
    now = sentences(clean_text(get_section(text, TOLD) or "")[0])
    if now[:len(it["sents"])] != it["sents"]:
        log(f"[repair] {it['key']} changed meanwhile: left where it is")
        return []
    extra = now[len(it["sents"]):]  # moved into it earlier in this run: stays
    dest = {}
    for r in ranges:
        for i in range(int(r["from"]), int(r["to"]) + 1):
            dest[i] = r["event"] if r["sure"] else own
    keep = [s for i, s in enumerate(it["sents"], 1) if dest[i] == own] + extra
    out: dict[str, list[str]] = {}
    for i, s in enumerate(it["sents"], 1):
        if dest[i] != own:
            out.setdefault(dest[i], []).append(s)
    why = {r["event"]: r["why"] for r in ranges if r["sure"] and r["event"] != own}
    labels = {c["event"]: _label(c) for c in it["candidates"]}
    src_label = _label(it["saved_on"])
    today = time.strftime("%Y-%m-%d")
    rel = lambda p: Path(p).relative_to(cfg.vault_dir).as_posix()  # noqa: E731
    done = []
    own_notes = _memory_paths(cfg, _find(cfg, conn, own))
    if not keep and len(out) == 1 and [Path(x).resolve() for x in own_notes] == [path.resolve()]:
        target = next(iter(out))  # all of it is about another event: the whole memory moves (meaning and all)
        tpaths = _memory_paths(cfg, _find(cfg, conn, target))
        typed = _typed_story(tpaths[0]) if tpaths else ""
        backup.save(path)
        for p in tpaths:
            backup.save(p)
        res = move_memory(cfg, conn, own, target)
        note = cfg.vault_dir / res["note"]
        if not tpaths:
            backup.created(note)
        info = touched.setdefault(rel(note), {})
        info.setdefault("moved_in", []).append(src_label)
        if typed:
            info["written_story"] = typed
        done.append({"from": src_label, "to": labels[target], "sentences": len(out[target]), "whole": True,
                     "why": why.get(target, ""), "note": res["note"]})
        return done
    for target, sents in out.items():
        moved = " ".join(sents)
        tpaths = _memory_paths(cfg, _find(cfg, conn, target))
        line = f"- {len(sents)} sentences from {src_label} (told while that event was on screen), moved {today}"
        if tpaths:
            backup.save(tpaths[0])
            t = tpaths[0].read_text(encoding="utf-8", errors="ignore")
            old_told = get_section(t, TOLD)
            info = touched.setdefault(rel(tpaths[0]), {})
            if _typed_story(tpaths[0]):
                info["written_story"] = _typed_story(tpaths[0])  # they typed this one: kept as it is
            t = set_section(t, TOLD, f"{old_told}\n\n{moved}" if old_told else moved)
            tpaths[0].write_text(add_lines(t, "Moved here", [line]), encoding="utf-8")
            note = tpaths[0]
        else:
            src = _read(path)
            res = save_memory(cfg, conn, target, {"story": "", "told": moved, "source": "story mode",
                                                  "importance": src["importance"], "era": src["era"],
                                                  "content": src["content"]})
            note = cfg.vault_dir / res["note"]
            backup.created(note)
            note.write_text(add_lines(note.read_text(encoding="utf-8"), "Moved here", [line]), encoding="utf-8")
            info = touched.setdefault(rel(note), {})
        info.setdefault("moved_in", []).append(src_label)
        done.append({"from": src_label, "to": labels[target], "sentences": len(sents), "whole": False,
                     "why": why.get(target, ""), "note": rel(note)})
    backup.save(path)
    if keep:
        text = path.read_text(encoding="utf-8", errors="ignore")
        text = set_section(text, TOLD, " ".join(keep))
        text = add_lines(text, "Moved to other events", [f"- {len(s)} sentences → {labels[t]}" for t, s in out.items()])
        path.write_text(text, encoding="utf-8")
        touched.setdefault(rel(path), {})["moved_out"] = [labels[t] for t in out]
    else:  # split over several other events: nothing is left here (the note is kept in Memories/_merged)
        _set_aside(cfg, path)
    return done


def _typed_story(path: Path) -> str:
    """The story of a memory written in One by one (no voice text): the repair keeps it as it is."""
    from .memories import _read

    if not path.exists() or get_section(path.read_text(encoding="utf-8", errors="ignore"), TOLD):
        return ""
    return _read(path)["story"]


# ---------------------------------------------------------------- 2. rewrite

def repair(cfg: Config, conn, dry_run: bool = False, force: bool = False, log=print, stop=None,
           place: bool = True) -> dict:
    from . import story, voice
    from .memories import _find, suggest_move, sync_content_flags

    ai = None if dry_run else story.ai_name(cfg)
    backup = Backup(cfg)
    touched: dict[str, dict] = {}
    moved: list[dict] = []
    failed = None
    if ai and place:
        try:
            moved = _place_all(cfg, conn, ai, backup, touched, log, stop, force)
        except Exception as e:  # noqa: BLE001 - what's moved is saved; the rewrite still runs
            failed = str(e)[:300]
            log(f"[repair] checking events stopped: {failed}")
    checked = [check(p) for p in notes(cfg)]
    for c in checked:
        info = touched.get(c["path"].relative_to(cfg.vault_dir).as_posix())
        if info is not None:
            c["touched"] = info
            if c["kind"] == "ok" and voice.words(c["cleaned"]) >= 15:
                c["kind"] = "moved"
    todo = [c for c in checked if c["kind"] != "ok" and (force or not c["repaired"] or c.get("touched"))]
    hints = []
    for c in checked:
        if not moved and "story" in (c["source"] or "") and c["event_id"] and _find(cfg, conn, c["event_id"]):
            sug = suggest_move(cfg, conn, c["event_id"], f"{c['story']}\n{c['cleaned']}")
            if sug:
                hints.append((c, sug))
    counts = {k: sum(1 for c in todo if c["kind"] == k) for k in ("damaged", "short", "old", "retell", "moved")}
    log(f"[repair] {len(checked)} memories: {len(moved)} stories moved to the right event; to rewrite in full: "
        f"{counts['old'] + counts['short']} told before the fix, {counts['damaged']} damaged by voice-to-text, "
        f"{counts['moved']} that got moved text; {counts['retell']} lost too much (tell again)")
    if not dry_run and not ai and todo:
        log("[repair] no AI available (log in to Claude Code, or set a Gemini key): only marking what to tell again")
    vocab = voice.vocabulary(cfg, conn)
    context = {"vocabulary": {"words": vocab["terms"], "mishearings": vocab["fixes"], "places": vocab["places"][:60],
                              "folders": vocab["folders"][:30]},
               "known_people": vocab["people"][:300]}
    flagged = 0
    for c in todo:
        if c["kind"] == "retell" and not dry_run and "needs_retelling: yes" not in c["path"].read_text(encoding="utf-8"):
            backup.save(c["path"])
            c["path"].write_text(set_fm(c["path"].read_text(encoding="utf-8"), "needs_retelling", "yes"),
                                 encoding="utf-8")
            flagged += 1
    items = []
    for k, c in enumerate(todo):
        ev = _context(cfg, conn, c["event_id"])
        c["id"] = f"n{k}"
        if voice.words(c["cleaned"]) >= 15:
            info = c.get("touched") or {}
            items.append({"id": c["id"], "date": c["date"], "place": (ev or {}).get("place", ""),
                          "footage": (ev or {}).get("footage", []),
                          "people_seen": (ev or {}).get("people_seen", []), "earlier_people": c["people"],
                          "as_told": c["cleaned"], "earlier_draft": c["story"], "c": c,
                          **({"written_story": info["written_story"]} if info.get("written_story") else {}),
                          **({"moved_out": info["moved_out"]} if info.get("moved_out") else {}),
                          "said": c["cleaned"], "already_told": c["story"]})  # (sizes for batching)
    results: dict[str, dict] = {}
    written = 0
    if ai and items and not (failed and failed == "stopped"):
        log(f"[repair] step 2 of 2: writing {len(items)} memories again in full…")
        for n, batch in enumerate(story.batches(items), 1):
            if stop is not None and stop.is_set():
                failed = "stopped"
                break
            log(f"[repair] step 2: part {n} ({written} of {len(items)} rewritten)…")
            try:
                got = _ask(cfg, ai, batch, context, log, stop)
                for it in batch:
                    r = got.get(it["id"])
                    if r and voice.words(it["as_told"]) >= 120 and \
                            voice.words(r["story"]) < story.SHORT * voice.words(it["as_told"]):
                        again = _ask(cfg, ai, [dict(it, too_short=(
                            f"Your last version had {voice.words(r['story'])} words for {voice.words(it['as_told'])} "
                            "words said. That is a summary: retell everything."))], context, log, stop).get(it["id"])
                        if again and voice.words(again["story"]) > voice.words(r["story"]):
                            got[it["id"]] = again
            except Exception as e:  # noqa: BLE001 - what's done is saved; run again later to continue
                failed = str(e)[:300]
                log(f"[repair] AI stopped: {failed}. Start it again later: it continues where it stopped.")
                break
            for it in batch:  # saved right away: an interrupted run loses nothing
                r = got.get(it["id"])
                if r and r["story"].strip():
                    _write(cfg, it["c"], r, vocab, backup)
                    results[it["id"]] = r
                    written += 1
    if written or flagged or moved:
        sync_content_flags(cfg, conn)
    report = _report(cfg, checked, todo, results, moved, hints, dry_run, failed)
    log(f"[repair] {'would rewrite' if dry_run else 'rewrote'} {len(items) if dry_run else written} memories, moved "
        f"{len(moved)} stories, {counts['retell']} to tell again. Report: {report.relative_to(cfg.vault_dir)}"
        + (f". Undo: crag memories repair --undo (notes as they were: Memories/{BACKUPS}/{backup.dir.name})"
           if backup.m["changed"] or backup.m["created"] else ""))
    return {"checked": len(checked), "damaged": counts["damaged"], "short": counts["short"], "old": counts["old"],
            "retell": counts["retell"], "moved": len(moved), "rewritten": written, "flagged": flagged,
            "maybe_wrong_event": len(hints), "dry_run": dry_run, "ai": ai, "error": failed, "report": str(report),
            "backup": backup.dir.name if backup.m["changed"] or backup.m["created"] else None}


def _context(cfg: Config, conn, event_id: str) -> dict | None:
    """Where the event was, what its footage shows and who is in it, for the AI."""
    from .memories import _find, _place

    ev = _find(cfg, conn, event_id) if event_id else None
    if not ev:
        return None
    ids = [m["id"] for m in ev]
    marks = ",".join("?" * len(ids))
    people = [r[0] for r in conn.execute(
        f"SELECT p.name, count(*) n FROM faces f JOIN people p ON p.id=f.person_id WHERE f.media_id IN ({marks}) "
        "AND p.name IS NOT NULL AND p.hidden=0 GROUP BY p.name ORDER BY n DESC LIMIT 12", ids)]
    return {"place": _place(ev), "footage": list(dict.fromkeys(m["title"] for m in ev if m["title"]))[:25],
            "people_seen": people}


def _write(cfg: Config, c: dict, r: dict, vocab: dict, backup: Backup) -> None:
    from .vault import _yaml_list
    from .voice import UNCLEAR

    path = c["path"]
    if not path.exists():
        return
    backup.save(path)
    text = path.read_text(encoding="utf-8", errors="ignore")
    today = time.strftime("%Y-%m-%d")
    earlier = [f"**Earlier draft (before the repair on {today}):**\n{c['story']}" if c["story"] else ""]
    if c["told"] and c["cleaned"] != c["told"]:
        earlier.append(f"**Voice-to-text then (with its errors):**\n{c['told']}")
    text = set_section(text, "What happened", r["story"].strip())
    if c["told"] and c["cleaned"] != c["told"]:
        text = set_section(text, TOLD, c["cleaned"] or UNCLEAR)
    earlier = "\n\n".join(x for x in earlier if x)
    if earlier:
        old = get_section(text, EARLIER)
        text = set_section(text, EARLIER, f"{old}\n\n{earlier}" if old else earlier)
    qs = [f"- {q.strip()}" for q in r.get("questions") or [] if q.strip()]
    if qs:
        text = add_lines(text, "Open questions", qs)
    known = {p.lower() for p in vocab["people"]}
    people = [p.strip() for p in r.get("people") or [] if p.strip()]
    people += [p for p in c["people"] if p.lower() in known and p not in people]
    if people:
        text = set_fm(text, "people", _yaml_list(people))
    path.write_text(set_fm(text, "repaired", today), encoding="utf-8")


def _report(cfg: Config, checked, todo, results, moved, hints, dry_run: bool, failed: str | None) -> Path:
    from .voice import words

    def link(c: dict) -> str:
        name = c["path"].relative_to(cfg.vault_dir).with_suffix("").as_posix()
        return f"[[{name}|{c['date']} {c['title'].split(' · ')[-1] if c['title'] else ''}]]"

    def damage(st: dict) -> str:
        parts = []
        if st["lost"]:
            parts.append(f"{st['lost']} words in a wrong script/language")
        if st["loops"]:
            parts.append(f"{st['loops']} words of loops")
        if st["phantom"]:
            parts.append(f"{st['phantom']} invented lines")
        if st["tags"]:
            parts.append(f"{st['tags']}× 'foreign'/[Music]")
        return ", ".join(parts) or "no damage"

    why = {"short": "long telling saved as a short summary", "old": "told before the fix (summarised)",
           "moved": "got text moved in from another event"}
    head = "Dry run: nothing was changed." if dry_run else \
        f"Every changed note was copied first to Memories/{BACKUPS}/ (`crag memories repair --undo` puts them back)."
    lines = [f"# Memory repair · {time.strftime('%Y-%m-%d %H:%M')}", "", f"*{head} {len(checked)} memories checked.*", ""]
    if failed:
        lines += [f"> [!warning] It stopped: {failed}. Start it again: it continues where it stopped.", ""]
    if moved:
        lines += ["## Moved to the event they're about", "Told while another event was on screen.", ""]
        lines += [f"- {m['from']} → **{m['to']}**: {'the whole memory' if m['whole'] else str(m['sentences']) + ' sentences'}"
                  f"{' (' + m['why'] + ')' if m['why'] else ''}" for m in moved]
        lines.append("")
    retell = [c for c in todo if c["kind"] == "retell"]
    if retell:
        lines += ["## Tell these again", "Most of what you said was lost by the old voice-to-text. In the Memories tab: "
                  "Story mode → Start from → *Memories that need telling again*.", ""]
        lines += [f"- {link(c)}: {int(c['stats']['lost_share'] * 100)}% lost ({damage(c['stats'])})" for c in retell]
        lines.append("")
    fixed = [c for c in todo if c["kind"] != "retell"]
    if fixed:
        lines += ["## Written again in full from your words" if not dry_run else "## Would be written again in full", ""]
        for c in fixed:
            r = results.get(c.get("id", ""))
            what = why.get(c["kind"]) or damage(c["stats"])
            size = f"; story {words(c['story'])} → {words(r['story'])} words" if r else ("" if dry_run else "; not yet")
            lines.append(f"- {link(c)}: {what}{size}")
        lines.append("")
    if hints:
        lines += ["## Maybe told on the wrong event",
                  "The story names a nearby event much more than its own (the real run checks this with the AI and "
                  "moves what's clear).", ""]
        lines += [f"- {link(c)} → {s['date']} · {s['place'] or s['title']} ({s['items']} photos/videos): mentions "
                  f"{', '.join(s['why'])}" for c, s in hints]
        lines.append("")
    qs = [(c, q) for c in todo for q in (results.get(c.get("id", "")) or {}).get("questions", [])]
    if qs:
        lines += ["## Questions for you", "Answer them in the note (or tell Claude in chat).", ""]
        lines += [f"- {link(c)}: {q}" for c, q in qs]
        lines.append("")
    if not (retell or fixed or hints or moved):
        lines += ["Everything looks fine.", ""]
    path = cfg.vault_dir / "_generated" / "Memory repair.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
