"""Talking head in, edit plan out.

1. Find the speech in the A-roll and drop pauses/fillers (jump cuts).
2. Transcribe with word timings (Whisper on the Mac if installed, otherwise Gemini).
3. Gemini reads the lines: English meaning, Roman-script Hinglish for captions, what should be on
   screen for each line, which line is the payoff, what kind of hook fits.
4. Build the timeline the style asks for: a hook clip from the library first, then the talk with
   B-roll inserts from the library (fresh, privacy-safe, matching each line), the hook moment shown
   again in full at the payoff, captions, sound effects and light-leak transitions.
The result is an EditPlan (plan.json) that every exporter renders.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..search import Filters, search
from .plan import Caption, Clip, EditPlan, Sound
from .style import load_style, pick_asset

FILLERS = {"um", "umm", "uh", "uhh", "uhm", "hmm", "erm", "er", "ah", "aa", "aaa"}


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Line:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    english: str = ""
    roman: str = ""
    visual_query: str = ""
    wants_broll: bool = True
    emphasis: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- media helpers

def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                         capture_output=True, text=True, check=True)
    info = json.loads(out.stdout)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    rate = v.get("avg_frame_rate") or "30/1"
    num, den = (rate.split("/") + ["1"])[:2]
    return {"duration": float(info["format"].get("duration", 0)), "width": v.get("width"), "height": v.get("height"),
            "fps": round(float(num) / float(den or 1)) if float(den or 1) else 30,
            "has_audio": any(s.get("codec_type") == "audio" for s in info["streams"])}


def speech_ranges(path: Path, duration: float, silence: float, noise_db: int = -32) -> list[tuple[float, float]]:
    from ..probe import audio_stream

    a = audio_stream(path)
    if a is None:
        return [(0.0, duration)]
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "info", "-i", str(path), "-map", f"0:{a}", "-vn",
                        "-af", f"silencedetect=noise={noise_db}dB:d={silence}", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=900)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    speech, cur = [], 0.0
    for i, s in enumerate(starts):
        if s > cur:
            speech.append((cur, s))
        cur = ends[i] if i < len(ends) else duration
    if cur < duration:
        speech.append((cur, duration))
    return [(a, b) for a, b in speech if b - a > 0.15] or [(0.0, duration)]


def subtract(ranges: list[tuple[float, float]], cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = []
    for a, b in ranges:
        parts = [(a, b)]
        for c0, c1 in cuts:
            nxt = []
            for p0, p1 in parts:
                if c1 <= p0 or c0 >= p1:
                    nxt.append((p0, p1))
                    continue
                if c0 > p0:
                    nxt.append((p0, c0))
                if c1 < p1:
                    nxt.append((c1, p1))
            parts = nxt
        out += [(p0, p1) for p0, p1 in parts if p1 - p0 > 0.12]
    return out


# ---------------------------------------------------------------- transcription

def transcribe(cfg: Config, path: Path, log=print) -> list[Word]:
    """Word timings: mlx-whisper on Apple Silicon, else Gemini (line level, words spread evenly)."""
    try:
        import mlx_whisper  # type: ignore

        import tempfile

        from ..prep import extract_audio

        with tempfile.TemporaryDirectory() as tmp:  # a clean mono wav (iPhone spatial audio is skipped)
            wav = Path(tmp) / "voice.wav"
            if not extract_audio(path, wav):
                raise RuntimeError(f"no usable audio track in {path.name}")
            res = mlx_whisper.transcribe(str(wav), path_or_hf_repo=cfg.whisper_model, language=cfg.language or None,
                                         word_timestamps=True, condition_on_previous_text=False)
        return [Word(float(w["start"]), float(w["end"]), w["word"].strip())
                for seg in res.get("segments", []) for w in seg.get("words", []) if w.get("word", "").strip()]
    except ImportError:
        pass
    log("[edit] Whisper not installed; transcribing with Gemini")
    import tempfile

    from .. import gemini

    schema = {"type": "object", "properties": {"lines": {"type": "array", "items": {
        "type": "object", "properties": {"start": {"type": "number"}, "end": {"type": "number"}, "text": {"type": "string"}},
        "required": ["start", "end", "text"], "additionalProperties": False}}},
        "required": ["lines"], "additionalProperties": False}
    with tempfile.TemporaryDirectory() as tmp:
        part = gemini.video_part(cfg, path, Path(tmp), max_seconds=600)
        data = gemini.ask_json(cfg, [part, "Transcribe the speech exactly as spoken (Hindi in Devanagari, English "
                                           "as English). One entry per short phrase with accurate start/end seconds."],
                               schema)
    words = []
    for ln in data["lines"]:
        toks = ln["text"].split()
        span = max(0.05, (ln["end"] - ln["start"]) / max(1, len(toks)))
        words += [Word(ln["start"] + i * span, ln["start"] + (i + 1) * span, t) for i, t in enumerate(toks)]
    return words


def to_lines(words: list[Word], max_len: float = 6.0, gap: float = 0.8) -> list[Line]:
    lines: list[Line] = []
    for w in words:
        if lines:
            cur = lines[-1]
            ends_sentence = re.search(r"[.!?।]$", cur.words[-1].text) is not None
            if not ends_sentence and w.start - cur.end < gap and w.end - cur.start <= max_len:
                cur.words.append(w)
                cur.end = w.end
                cur.text += " " + w.text
                continue
        lines.append(Line(w.start, w.end, w.text, [w]))
    return lines


# ---------------------------------------------------------------- understanding (Gemini)

UNDERSTAND_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
        "hook_query": {"type": "string", "description": "What kind of library footage would make a scroll-stopping "
                                                       "first 1.5 s for this video (English search words)."},
        "collection_hint": {"type": "string", "description": "Trip/period/place the story is about if any, else ''."},
        "payoff_line": {"type": "integer", "description": "Index of the line with the punchline / key moment."},
        "lines": {"type": "array", "items": {"type": "object", "properties": {
            "index": {"type": "integer"},
            "english": {"type": "string"},
            "roman": {"type": "string", "description": "The line exactly as spoken, Hindi written in Latin letters."},
            "visual_query": {"type": "string", "description": "English search words for footage to show on this line."},
            "wants_broll": {"type": "boolean", "description": "False when the face should stay (direct address, joke "
                                                            "delivery, emotional beat)."},
            "emphasis": {"type": "array", "items": {"type": "string"},
                         "description": "1-2 key words (from roman) to highlight in captions."}},
            "required": ["index", "english", "roman", "visual_query", "wants_broll", "emphasis"],
            "additionalProperties": False}},
    },
    "required": ["topic", "hook_query", "collection_hint", "payoff_line", "lines"],
    "additionalProperties": False,
}


def understand(cfg: Config, lines: list[Line], notes: str = "", log=print) -> dict:
    """Ask the AI what each line means and what footage would illustrate it (the configured backend:
    Claude subscription for claude-code, else Gemini)."""
    text = "\n".join(f"{i}: [{ln.start:.1f}-{ln.end:.1f}] {ln.text}" for i, ln in enumerate(lines))
    prompt = f"Talking-head reel transcript (Hindi/Hinglish/English):\n{text}\n\nCreator notes: {notes or '-'}"
    system = ("You plan B-roll and captions for Instagram reels made from the creator's own life footage archive "
              "(searchable by English descriptions).")
    try:
        if cfg.backend == "claude-code":
            from .. import claude_code

            return claude_code.ask(cfg, [{"type": "text", "text": prompt}], UNDERSTAND_SCHEMA, system)[0]
        from .. import gemini

        return gemini.ask_json(cfg, [prompt], UNDERSTAND_SCHEMA, system=system)
    except (Exception, SystemExit) as e:  # offline / no key: simple fallback, still a usable plan
        log(f"[edit] couldn't ask the AI ({e}); using the transcript as search text")
        return {"topic": "", "hook_query": "funny surprising moment", "collection_hint": "",
                "payoff_line": max(0, len(lines) - 1),
                "lines": [{"index": i, "english": ln.text, "roman": ln.text, "visual_query": ln.text,
                           "wants_broll": i > 0, "emphasis": []} for i, ln in enumerate(lines)]}


# ---------------------------------------------------------------- the plan

class TimeMap:
    """A-roll source time -> output time, for the kept (jump-cut) segments."""

    def __init__(self, segments: list[tuple[float, float]], offset: float):
        self.items, at = [], offset
        for a, b in segments:
            self.items.append((a, b, at))
            at += b - a
        self.end = at

    def __call__(self, t: float) -> float | None:
        for a, b, at in self.items:
            if a - 1e-6 <= t <= b + 1e-6:
                return at + (t - a)
        return None

    def nearest(self, t: float) -> float:
        best = min(self.items, key=lambda it: 0 if it[0] <= t <= it[1] else min(abs(t - it[0]), abs(t - it[1])))
        a, b, at = best
        return at + (min(max(t, a), b) - a)


def plan_talking(cfg: Config, conn, aroll: Path, name: str, style_name: str | None = None, page: str | None = None,
                 music: str | None = None, collection: str | None = None, notes: str = "",
                 words: list[Word] | None = None, understanding: dict | None = None, use_vectors: bool = True,
                 log=print) -> EditPlan:
    style = load_style(cfg, style_name)
    pace, hook_cfg, sfx = style["pace"], style["hook"], style["sfx"]
    info = probe(aroll)
    plan = EditPlan(name=name, style=style["name"], page=page or style.get("page"), fps=30,
                    grade=style["grade"], caption_style=style["captions"])

    # 1-2. speech, fillers, words
    keep = speech_ranges(aroll, info["duration"], pace["jump_cut_silence"]) if info["has_audio"] \
        else [(0.0, info["duration"])]
    keep = [(max(0.0, a - pace["keep_padding"]), min(info["duration"], b + pace["keep_padding"])) for a, b in keep]
    words = words if words is not None else transcribe(cfg, aroll, log)
    fillers = [(w.start, w.end) for w in words if re.sub(r"\W", "", w.text.lower()) in FILLERS]
    keep = subtract(_merge(keep), fillers)
    words = [w for w in words if (w.start, w.end) not in fillers]
    lines = to_lines(words)
    u = understanding or understand(cfg, lines, notes, log)
    for item in u.get("lines", []):
        if 0 <= item["index"] < len(lines):
            ln = lines[item["index"]]
            ln.english, ln.roman, ln.visual_query = item["english"], item["roman"], item["visual_query"]
            ln.wants_broll, ln.emphasis = item["wants_broll"], item["emphasis"]
    coll = collection or (u.get("collection_hint") or None)

    # 3. hook clip from the library
    hook = None
    t0 = 0.0
    if hook_cfg["open_with_hook"]:
        cands = _candidates(cfg, conn, u.get("hook_query") or "", coll, ("hook",), use_vectors) \
            or _candidates(cfg, conn, u.get("hook_query") or "", None, ("hook",), use_vectors)
        cands = [c for c in cands if c["end"] - c["start"] >= 0.8]
        if cands:
            hook = cands[0]
            length = min(hook_cfg["hook_length"], hook["end"] - hook["start"])
            plan.clips.append(Clip("broll", hook["file"], hook["start"], round(hook["start"] + length, 3), 0.0,
                                   hook["media_id"], hook["moment_id"], "hook", audio=hook_cfg["hook_audio"],
                                   volume=0.8, reframe=_reframe(hook, style), note=f"hook: {hook['description']}"))
            t0 = length
            _sfx(cfg, plan, sfx.get("on_hook"), 0.0, sfx["volume"], name)
        else:
            plan.notes.append("No hook clip found in the library; starts on the face.")

    # 4. A-roll segments
    tmap = TimeMap(keep, t0)
    for a, b, at in tmap.items:
        plan.clips.append(Clip("aroll", str(aroll), round(a, 3), round(b, 3), round(at, 3), role="talk", audio=True,
                               reframe="crop"))
    if hook:
        _transition(cfg, plan, style, t0, f"{name}-hook", "hook")

    # 5. B-roll per line
    used: dict[int, float] = {}  # moment -> next unused second
    payoff_i = u.get("payoff_line", len(lines) - 1)
    last_end = t0 + pace["face_first_seconds"]
    for i, ln in enumerate(lines):
        start_out, end_out = tmap.nearest(ln.start), tmap.nearest(ln.end)
        if i == payoff_i and hook and hook_cfg["replay_hook_later"] and start_out - t0 >= 2.5:
            length = min(max(pace["broll_max"] * 1.4, 2.0), hook["end"] - hook["start"], max(0.8, end_out - start_out))
            plan.clips.append(Clip("broll", hook["file"], hook["start"], round(hook["start"] + length, 3),
                                   round(start_out, 3), hook["media_id"], hook["moment_id"], "payoff",
                                   reframe=_reframe(hook, style), note=f"payoff, hook shown in full: {ln.english}"))
            if not _transition(cfg, plan, style, start_out, f"{name}-payoff", "payoff"):
                _sfx(cfg, plan, sfx.get("before_payoff"), start_out, sfx["volume"], name, end_at=True)
            last_end = start_out + length
            continue
        if not ln.wants_broll or end_out - max(start_out, last_end) < pace["broll_min"]:
            continue
        if start_out - last_end < pace["broll_every"] - pace["broll_max"]:
            continue
        at = max(start_out + 0.15, last_end + 0.4)
        need = round(min(pace["broll_max"], end_out - at - 0.1), 3)
        if need < pace["broll_min"]:
            continue
        pick = None
        for c in _candidates(cfg, conn, ln.visual_query or ln.english or ln.text, coll, (), use_vectors) \
                or _candidates(cfg, conn, ln.visual_query or ln.english or ln.text, None, (), use_vectors):
            src_in = used.get(c["moment_id"], c["start"])
            if c["end"] - src_in >= need and (not hook or c["moment_id"] != hook["moment_id"]):
                pick = (c, src_in)
                break
        if pick is None:
            plan.shoot_list.append(f"“{ln.english or ln.text}” — film: {ln.visual_query}")
            continue
        c, src_in = pick
        used[c["moment_id"]] = src_in + need
        plan.clips.append(Clip("broll", c["file"], round(src_in, 3), round(src_in + need, 3), round(at, 3),
                               c["media_id"], c["moment_id"], "broll", reframe=_reframe(c, style),
                               note=f"line {i}: {ln.english or ln.text} → {c['description']}"))
        if style["transitions"]["where"] != "every_broll" or \
                not _transition(cfg, plan, style, at, f"{name}-{i}", "broll"):
            _sfx(cfg, plan, sfx.get("on_broll"), at - 0.08, sfx["volume"] * 0.7, f"{name}-{i}")
        last_end = at + need

    # 6. captions, music
    if style["captions"]["enabled"]:
        plan.captions = _captions(lines, tmap, style["captions"])
    if music:
        from ..music import load as load_song

        song = load_song(cfg, music)
        plan.sounds.append(Sound(0.0, song.file, style["music"]["volume_under_voice"], "music", 0.0,
                                 min(song.duration, tmap.end)))
    plan.notes.append(f"Topic: {u.get('topic', '')}. Kept {sum(b - a for a, b in keep):.1f}s of "
                      f"{info['duration']:.1f}s talking head ({len(keep)} parts).")
    plan.fix_duration()
    return plan


def _merge(ranges):
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _reframe(c: dict, style: dict) -> str | None:
    return None if c.get("orientation") == "vertical" else style.get("reframe", "crop")


def _candidates(cfg, conn, query, collection, roles, use_vectors):
    f = Filters(kind="video", collection=collection, roles=tuple(roles))
    res = search(cfg, conn, query, f, limit=30, use_vectors=use_vectors) if query else []
    if not res and roles:
        res = search(cfg, conn, "", f, limit=30, use_vectors=False)
    # prefer vertical and decent quality without hiding everything else
    return sorted(res, key=lambda r: (r["orientation"] != "vertical", -(r["broll_score"] or 0) // 2,
                                      bool(r["quality_issues"])))


def _sfx(cfg, plan: EditPlan, kind: str | None, at: float, volume: float, seed: str, end_at: bool = False):
    f = pick_asset(cfg, kind or "", seed) if kind and kind != "none" else None
    if f is None:
        return
    if end_at:  # e.g. a riser that should finish exactly on the payoff
        try:
            at = max(0.0, at - probe_audio(f))
        except Exception:
            pass
    plan.sounds.append(Sound(round(max(0.0, at), 3), str(f), volume, "sfx"))


def probe_audio(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return float(r.stdout.strip() or 0)


def _transition(cfg, plan: EditPlan, style: dict, at: float, seed: str, moment: str = "section") -> bool:
    """A transition on the cut at `at`. With an imported transition library (crag assets import) a whole
    recipe (plate + its SFX, timed and levelled as in the template) is used; returns True then."""
    t = style["transitions"]
    if t.get("library", "auto") != "off" and t["style"] != "cut":
        from .transitions import apply, choose, load_library

        recipes = load_library(cfg)
        recent = {c.note.split(" ")[0] for c in plan.track("overlay")[-3:] if c.role == "transition"}
        r = choose(recipes, moment, seed, avoid=recent, prefer=t.get("prefer"))
        if r is not None:
            apply(cfg, plan, r, at, sfx_volume=style["sfx"]["volume"] / 0.6, opacity=t.get("plate_opacity", 1.0))
            return True
    if t["style"] == "leak":
        leak = pick_asset(cfg, "light_leaks", seed)
        if leak is not None:
            d = t["duration"]
            plan.clips.append(Clip("overlay", str(leak), 0.0, d, round(max(0.0, at - d / 2), 3), role="leak",
                                   blend="screen", volume=t.get("opacity", 0.85), note="light leak"))
    _sfx(cfg, plan, style["sfx"].get("on_transition"), at - 0.05, style["sfx"]["volume"], seed)
    return False


def _captions(lines: list[Line], tmap: TimeMap, cs: dict) -> list[Caption]:
    out = []
    n = max(1, int(cs["words_per_caption"]))
    for ln in lines:
        text = {"roman": ln.roman, "english": ln.english}.get(cs["script"]) or ln.text
        toks = text.split()
        if not toks:
            continue
        if cs["script"] == "devanagari" or text == ln.text:
            timed = [(tmap.nearest(w.start), tmap.nearest(w.end), w.text) for w in ln.words]
        else:  # spread the translated/transliterated words over the line, by length
            s, e = tmap.nearest(ln.start), tmap.nearest(ln.end)
            total = sum(len(t) + 1 for t in toks)
            timed, cur = [], s
            for t in toks:
                d = (e - s) * (len(t) + 1) / total
                timed.append((cur, cur + d, t))
                cur += d
        for k in range(0, len(timed), n):
            chunk = timed[k:k + n]
            words = [w for _, _, w in chunk]
            txt = " ".join(words)
            emph = [w for w in words if re.sub(r"\W", "", w.lower()) in
                    {re.sub(r"\W", "", e.lower()) for e in ln.emphasis}]
            out.append(Caption(round(chunk[0][0], 3), round(max(chunk[-1][1], chunk[0][0] + 0.25), 3),
                               txt.upper() if cs["uppercase"] else txt,
                               [e.upper() if cs["uppercase"] else e for e in emph]))
    for a, b in zip(out, out[1:]):  # no overlaps, no flicker gaps
        a.end = round(min(max(a.end, b.start), b.start), 3) if b.start - a.end < 0.25 else a.end
    return out
