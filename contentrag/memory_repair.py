"""crag memories repair: fix memories that were saved from damaged voice-to-text.

Before the voice fixes (voice.py), Whisper guessed the language and often wrote Hindi/Hinglish as Korean,
Russian or Italian, looped, invented "Thank you." and misheard names; the AI then wrote a short summary
from that. The recordings of saved sessions were deleted then, so repair works from the text:

1. 'As told (voice)' (or the story itself, for memories spoken with the Speak button) is cleaned:
   wrong-script and wrong-language lines, loops, "foreign" and invented lines are removed ([unclear] marks
   where words were lost).
2. Damaged memories, and long tellings that became a short summary, get 'What happened' written again
   from the cleaned words as a complete retelling, with your vocabulary and known names. What the AI can't
   make out goes to 'Open questions'.
3. Memories where most of the words were lost are marked `needs_retelling: yes` (the Memories tab lists
   them, and Story mode can play just those).
4. Stories that name a nearby event's place or occasion much more than their own are listed: they were
   probably told while the wrong slide was on screen (move them in Memories > One by one).

Nothing is deleted: the earlier text is kept in the note under 'Earlier versions (voice-to-text errors)'.
A report is written to LifeVault/_generated/Memory repair.md.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .config import Config
from .memories import EARLIER

SYSTEM = """You repair memory notes of a person's private life archive. Each note was written earlier by an AI from
voice-to-text of what they said (Hindi, English or both mixed). That voice-to-text was badly damaged: parts came out
in wrong scripts or languages, it looped, invented lines and misheard names. For each note you get:
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
  feelings and why). Translate Hindi/Hinglish into English. Fix a misheard word only when the vocabulary, the known
  people/places or the footage make it clear; else keep their word in quotes or write [unclear]. Use a fact from
  earlier_draft only if as_told supports it, or it is plainly a hand correction that doesn't contradict as_told;
  when they disagree, follow as_told and ask in questions. Never add names, schools, colleges, cities, hostels,
  relationships, reasons or feelings that aren't there. Never merge two names into one person.
questions: what they should clear up: names or places that couldn't be read, which institution, parts lost as
  [unclear] that seem to matter, contradictions between as_told and earlier_draft. [] if nothing.
people: the people named in the story, spelled as in the vocabulary / known people when clearly the same person.
If an entry has too_short, your earlier version of it was a summary: retell everything.
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
SENT = ("id", "date", "place", "footage", "people_seen", "earlier_people", "as_told", "earlier_draft", "too_short")
RETELL_LOST = 0.4   # this much of what was said lost: tell it again
TOLD = "As told (voice)"


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


# ---------------------------------------------------------------- check

def notes(cfg: Config) -> list[Path]:
    root = cfg.vault_dir / "Memories"
    return [p for p in sorted(root.rglob("*.md")) if not any(x.startswith("_") for x in p.relative_to(root).parts)]


def check(path: Path) -> dict:
    """What's wrong with one memory note."""
    from .memories import _read
    from .story import SHORT
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
    elif told and words(told) >= 120 and words(note["story"]) < SHORT * words(told):
        out["kind"] = "short"
    return out


# ---------------------------------------------------------------- repair

def repair(cfg: Config, conn, dry_run: bool = False, force: bool = False, log=print, stop=None) -> dict:
    from . import story, voice
    from .memories import _find, event_detail, suggest_move, sync_content_flags

    checked = [check(p) for p in notes(cfg)]
    todo = [c for c in checked if c["kind"] != "ok" and (force or not c["repaired"])]
    moves = []
    for c in checked:
        if "story" in (c["source"] or "") and c["event_id"] and _find(cfg, conn, c["event_id"]):
            sug = suggest_move(cfg, conn, c["event_id"], f"{c['story']}\n{c['cleaned']}")
            if sug:
                moves.append((c, sug))
    counts = {k: sum(1 for c in todo if c["kind"] == k) for k in ("damaged", "short", "retell")}
    log(f"[repair] {len(checked)} memories checked: {counts['damaged']} damaged by voice-to-text, "
        f"{counts['short']} long tellings saved as a short summary, {counts['retell']} lost too much (tell again), "
        f"{len(moves)} maybe on the wrong event")
    ai = None if dry_run else story.ai_name(cfg)
    if not dry_run and not ai and todo:
        log("[repair] no AI available (log in to Claude Code, or set a Gemini key): only marking what to tell again")
    vocab = voice.vocabulary(cfg, conn)
    context = {"vocabulary": {"words": vocab["terms"], "mishearings": vocab["fixes"], "places": vocab["places"][:60],
                              "folders": vocab["folders"][:30]},
               "known_people": vocab["people"][:300]}
    items = []
    for k, c in enumerate(todo):
        ev = event_detail(cfg, conn, c["event_id"]) if c["event_id"] and _find(cfg, conn, c["event_id"]) else None
        c["id"] = f"n{k}"
        if voice.words(c["cleaned"]) >= 15:
            items.append({"id": c["id"], "date": c["date"], "place": (ev or {}).get("place", ""),
                          "footage": [x["title"] for x in (ev or {}).get("items", [])[:25] if x["title"]],
                          "people_seen": (ev or {}).get("people_seen", []), "earlier_people": c["people"],
                          "as_told": c["cleaned"], "earlier_draft": c["story"],
                          "said": c["cleaned"], "already_told": c["story"]})  # (sizes for batching)
    results: dict[str, dict] = {}
    failed = None
    if ai and items:
        for n, batch in enumerate(story.batches(items), 1):
            if stop is not None and stop.is_set():
                log("[repair] stopped")
                break
            log(f"[repair] rewriting {len(batch)} memor{'y' if len(batch) == 1 else 'ies'} "
                f"(part {n}, {len(results)} of {len(items)} done)…")
            try:
                got = _ask(cfg, ai, batch, context)
                for it in batch:
                    r = got.get(it["id"])
                    if r and voice.words(it["as_told"]) >= 120 and \
                            voice.words(r["story"]) < story.SHORT * voice.words(it["as_told"]):
                        again = _ask(cfg, ai, [dict(it, too_short=(
                            f"Your last version had {voice.words(r['story'])} words for {voice.words(it['as_told'])} "
                            "words said. That is a summary: retell everything."))], context).get(it["id"])
                        if again and voice.words(again["story"]) > voice.words(r["story"]):
                            got[it["id"]] = again
                results.update(got)
            except Exception as e:  # noqa: BLE001 - e.g. usage limit: what's done is saved, run again later
                failed = str(e)[:300]
                log(f"[repair] AI stopped: {failed}. Run it again later; repaired memories are skipped.")
                break
    known = {p.lower() for p in vocab["people"]}
    today = time.strftime("%Y-%m-%d")
    written = flagged = 0
    for c in todo:
        r = results.get(c.get("id", ""))
        retell = c["kind"] == "retell"
        if dry_run or (not r and not retell):
            continue
        text = c["path"].read_text(encoding="utf-8", errors="ignore")
        if r and r["story"].strip():
            earlier = [f"**Earlier draft (before the repair on {today}):**\n{c['story']}" if c["story"] else ""]
            if c["told"] and c["cleaned"] != c["told"]:
                earlier.append(f"**Voice-to-text then (with its errors):**\n{c['told']}")
            text = set_section(text, "What happened", r["story"].strip())
            if c["told"] and c["cleaned"] != c["told"]:
                text = set_section(text, TOLD, c["cleaned"] or voice.UNCLEAR)
            earlier = "\n\n".join(x for x in earlier if x)
            if earlier:
                old = get_section(text, EARLIER)
                text = set_section(text, EARLIER, f"{old}\n\n{earlier}" if old else earlier)
            qs = [f"- {q.strip()}" for q in r.get("questions") or [] if q.strip()]
            if qs:
                text = add_lines(text, "Open questions", qs)
            people = [p.strip() for p in r.get("people") or [] if p.strip()]
            people += [p for p in c["people"] if p.lower() in known and p not in people]
            if people:
                from .vault import _yaml_list

                text = set_fm(text, "people", _yaml_list(people))
            text = set_fm(text, "repaired", today)
            written += 1
        if retell:
            text = set_fm(text, "needs_retelling", "yes")
            flagged += 1
        c["path"].write_text(text, encoding="utf-8")
    if written or flagged:
        sync_content_flags(cfg, conn)
    report = _report(cfg, checked, todo, results, moves, dry_run, failed)
    again = counts["retell"] if dry_run else flagged
    log(f"[repair] {'would rewrite' if dry_run else 'rewrote'} {len(items) if dry_run else written} memories; "
        f"{again} need telling again. Report: {report.relative_to(cfg.vault_dir)}")
    return {"checked": len(checked), "damaged": counts["damaged"], "short": counts["short"],
            "retell": counts["retell"], "rewritten": written, "flagged": flagged, "maybe_wrong_event": len(moves),
            "dry_run": dry_run, "ai": ai, "error": failed, "report": str(report)}



def _ask(cfg: Config, ai: str, batch: list[dict], context: dict) -> dict[str, dict]:
    from .story import ask_ai

    prompt = json.dumps(context | {"notes": [{k: it[k] for k in SENT if k in it} for it in batch]},
                        ensure_ascii=False, indent=1)
    data = ask_ai(cfg, ai, prompt, SCHEMA, SYSTEM)
    return {m["id"]: m for m in data.get("memories", []) if m.get("id")}


def _report(cfg: Config, checked, todo, results, moves, dry_run: bool, failed: str | None) -> Path:
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

    head = "Dry run: nothing was changed." if dry_run else f"Repaired notes keep their earlier text under “{EARLIER}”."
    lines = [f"# Memory repair · {time.strftime('%Y-%m-%d %H:%M')}", "", f"*{head} {len(checked)} memories checked.*", ""]
    if failed:
        lines += [f"> [!warning] The AI stopped: {failed}. Run `crag memories repair` again later.", ""]
    retell = [c for c in todo if c["kind"] == "retell"]
    if retell:
        lines += ["## Tell these again", "Most of what you said was lost by the old voice-to-text. In the Memories tab: "
                  "Story mode → Start from → *Memories that need telling again*.", ""]
        lines += [f"- {link(c)}: {int(c['stats']['lost_share'] * 100)}% lost ({damage(c['stats'])})" for c in retell]
        lines.append("")
    fixed = [c for c in todo if c["kind"] != "retell"]
    if fixed:
        lines += ["## Rewritten from your words" if not dry_run else "## Would be rewritten from your words", ""]
        for c in fixed:
            r = results.get(c.get("id", ""))
            what = "long telling saved as a short summary" if c["kind"] == "short" else damage(c["stats"])
            size = f"; story {words(c['story'])} → {words(r['story'])} words" if r else ""
            lines.append(f"- {link(c)}: {what}{size}")
        lines.append("")
    if moves:
        lines += ["## Maybe told on the wrong event",
                  "The story names a nearby event much more than its own. Open it in Memories → One by one and press "
                  "**Move it there** if that's right.", ""]
        lines += [f"- {link(c)} → {s['date']} · {s['place'] or s['title']} ({s['items']} photos/videos): mentions "
                  f"{', '.join(s['why'])}" for c, s in moves]
        lines.append("")
    qs = [(c, q) for c in todo for q in (results.get(c.get("id", "")) or {}).get("questions", [])]
    if qs:
        lines += ["## Questions for you", "Answer them in the note (or tell Claude in chat).", ""]
        lines += [f"- {link(c)}: {q}" for c, q in qs]
        lines.append("")
    if not (retell or fixed or moves):
        lines += ["Everything looks fine.", ""]
    path = cfg.vault_dir / "_generated" / "Memory repair.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
