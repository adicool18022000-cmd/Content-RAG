"""Voice to text for memories (Story mode and the Speak buttons), with Whisper on this Mac.

What went wrong with plain Whisper on Hindi/Hinglish, and what this does about it:

- It guessed the language from the first 30 seconds and then wrote everything in it (Korean, Russian,
  Chinese, Italian...). Here the language is always set: Hindi + English by default, or English.
- It doesn't know names (Manya -> "Mania", IIT -> "hatty", Purnia -> "Kurnia"). Every piece of audio gets a
  short prompt with your people, places, folders and `library_dir/vocabulary.txt`.
- Long takes drift into loops, "foreign" and invented lines ("Thank you."). The audio is cut into pieces of
  at most ~28 s at quiet moments, silent pieces are skipped, and every line is checked: lines in a wrong
  script or language, loops, "foreign" and invented lines are removed (marked [unclear] when words were lost).
- Speed: the normal (turbo) model is used unless you pick "Most accurate" (large-v3: better with names
  and Hindi, several times slower; downloaded once, ~3 GB). Silence is skipped, a piece is decoded again
  at most once, and Story mode transcribes the whole recording in ~28 s pieces (fewer passes than one
  piece per event).

Audio never leaves this computer.
"""

from __future__ import annotations

import inspect
import re
import threading
import unicodedata
from collections import Counter
from pathlib import Path

from .config import Config

SR = 16000
MAX_PIECE = 28.0       # seconds: Whisper hears 30 s at a time; a piece never spans two windows
SILENCE_DB = -45.0     # a piece whose loudest moment is quieter than this has no speech
UNCLEAR = "[unclear]"

# mode -> (Whisper language, task). There is no automatic mode any more: guessing the language is what
# turned Hinglish into Korean and Italian.
MODES = {
    "mixed": ("hi", "transcribe"),       # Hindi + English, as spoken (recommended)
    "en": ("en", "transcribe"),          # English only
    "translate": ("hi", "translate"),    # Hindi -> English text (shorter: loses details)
}
LEGACY = {"auto": "mixed", "hi": "mixed", "": "mixed", None: "mixed"}

# what Whisper writes when it hears silence or noise instead of speech
PHANTOM = {"thank you", "thanks", "thank you very much", "thank you so much", "thanks for watching", "bye",
           "bye bye", "you", "okay", "ok", "hmm", "uh", "um", "the end", "subtitles by the amaraorg community",
           "thank you for watching", "please subscribe", "like and subscribe", "धन्यवाद", "शुक्रिया",
           "सब्सक्राइब करें", "music", "applause", "foreign", "silence"}
_TAGS = re.compile(r"[\[(]\s*(?:foreign(?: language)?|music|applause|laughter|laughs|inaudible|silence|noise|"
                   r"speaking (?:in )?(?:a )?foreign language|speaks? foreign language|non-english speech|"
                   r"संगीत|बोलती है)\s*[\])]|[♪♫]+", re.I)
# frequent Italian / German / French / Spanish / Portuguese words that are not English or Hinglish:
# a sentence full of them is Whisper drifting into another language
_EURO = set("""che sono della questo questa anche perché molto gli nel delle degli cosa essere stato quando
fatto ancora sempre tutto tutti allora quindi und ist nicht ich eine einen mit auf für wir auch aber sehr
noch wie haben wird sind mich dich oder wenn dann schon les est une vous nous avec dans pour qui sur très
los las muy pero está não uma você esto eso pues entonces também então""".split())
_ACCENT = re.compile(r"[àèìòùáéíóúäöüßñçãõâêôœ]", re.I)
_CJK_ETC = "other"


def _script(ch: str) -> str:
    o = ord(ch)
    if o < 128:
        return "latin" if ch.isalpha() else "mark"
    if 0x0900 <= o <= 0x097F or 0xA8E0 <= o <= 0xA8FF or 0x1CD0 <= o <= 0x1CFF:
        return "deva"
    if 0x00C0 <= o <= 0x024F or 0x1E00 <= o <= 0x1EFF:
        return "latin"
    if 0x0600 <= o <= 0x06FF or 0x0750 <= o <= 0x077F or 0xFB50 <= o <= 0xFDFF or 0xFE70 <= o <= 0xFEFF:
        return "urdu"  # Hindustani written in Urdu script: the same words, Claude reads it
    if ch.isalpha() or unicodedata.category(ch).startswith("M"):
        return _CJK_ETC  # Korean, Chinese, Japanese, Cyrillic, Bengali, Thai, Greek...
    return "mark"


def foreign_share(text: str) -> float:
    """Share of the letters written in a script that isn't Latin, Devanagari or Urdu."""
    kinds = Counter(_script(c) for c in text)
    letters = sum(n for k, n in kinds.items() if k != "mark")
    return kinds[_CJK_ETC] / letters if letters else 0.0


def drifted(sentence: str) -> bool:
    """Latin letters, but Italian/German/French/Spanish (Whisper switched language)."""
    words = re.findall(r"[^\W\d_]+", sentence.lower())
    if len(words) < 4:
        return False
    euro = sum(1 for w in words if w in _EURO)
    accented = sum(1 for w in words if _ACCENT.search(w))
    return (euro >= 2 and euro / len(words) >= 0.2) or (accented >= 2 and accented / len(words) >= 0.15)


def _norm(text: str) -> str:
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


def collapse_loops(text: str) -> tuple[str, int]:
    """'we went there we went there we went there' -> 'we went there'. Returns (text, words removed)."""
    words = text.split()
    key = [_norm(w) for w in words]
    removed = 0
    for n in range(1, 13):
        need = 4 if n == 1 else 3  # 'bahut bahut' is speech; the same phrase 3+ times in a row is a loop
        out, kout, i = [], [], 0
        while i < len(words):
            k = 1
            while i + (k + 1) * n <= len(words) and key[i + k * n:i + (k + 1) * n] == key[i:i + n] \
                    and any(key[i:i + n]):
                k += 1
            if k >= need:
                out += words[i:i + n]
                kout += key[i:i + n]
                removed += (k - 1) * n
                i += k * n
            else:
                out.append(words[i])
                kout.append(key[i])
                i += 1
        words, key = out, kout
    return " ".join(words), removed


def _drop_bare_foreign(text: str) -> str:
    """Whisper writes 'foreign' for speech it can't place. 'foreign trip' stays."""
    tokens = text.split(" ")
    out = []
    for i, t in enumerate(tokens):
        if re.fullmatch(r"(?i)foreign[.,!?]*", t):
            nxt = next((x for x in tokens[i + 1:] if x), "")
            prev = next((x for x in reversed(out) if x), "")
            if not nxt or not prev or re.fullmatch(r"(?i)foreign[.,!?]*", nxt) or t[-1:] in ".!?" \
                    or prev[-1:] in ".!?।" or nxt[:1].isupper() or _script(nxt[:1]) != "latin":
                continue
        out.append(t)
    return re.sub(r"\s+", " ", " ".join(out)).strip()


def _clean_piece(text: str, vocab_terms: set[str] | None = None) -> tuple[str, str, int]:
    """One Whisper line or one sentence -> (text, kind, words removed as loops). kind: ok | lost (wrong
    script or language: text is UNCLEAR) | phantom (an invented line) | empty."""
    text = _TAGS.sub(" ", text or "")
    text = _drop_bare_foreign(re.sub(r"\s+", " ", text).strip())
    if not text:
        return "", "empty", 0
    if _norm(text) in PHANTOM:
        return "", "phantom", 0
    if foreign_share(text) > 0.3 or drifted(text):
        return UNCLEAR, "lost", 0
    if foreign_share(text) > 0:  # a few stray letters: drop them
        text = re.sub(r"\s+", " ", "".join(c for c in text if _script(c) != _CJK_ETC)).strip()
    if vocab_terms and _looks_like_prompt(text, vocab_terms):
        return "", "phantom", 0
    text, removed = collapse_loops(text)
    return text, "ok", removed


def _looks_like_prompt(text: str, vocab_terms: set[str]) -> bool:
    """On near-silence Whisper sometimes just repeats its prompt (our list of names)."""
    parts = [p.strip().lower() for p in re.split(r"[,،]", text) if p.strip()]
    return len(parts) >= 3 and sum(1 for p in parts if p.strip(" .") in vocab_terms) / len(parts) >= 0.7


def _merge_unclear(parts: list[str]) -> list[str]:
    out: list[str] = []
    for p in parts:
        if p and not (p == UNCLEAR and out and out[-1] == UNCLEAR):
            out.append(p)
    return out


def clean_segments(segs: list[dict], vocab_terms: set[str] | None = None) -> list[dict]:
    """Whisper segments -> [{start, end, text}] without invented lines, loops and wrong-script lines."""
    from .transcribe import keep_segment

    out: list[dict] = []
    for x in segs:
        if not (x.get("text") or "").strip() or not keep_segment(x):
            continue
        text, _, _ = _clean_piece(x["text"], vocab_terms)
        if not text:
            continue
        if out and _norm(out[-1]["text"]) == _norm(text):  # the same line again (a loop), or [unclear] twice
            out[-1]["end"] = float(x.get("end") or out[-1]["end"])
            continue
        out.append({"start": float(x.get("start") or 0), "end": float(x.get("end") or 0), "text": text})
    return out


SENTENCE = re.compile(r"(?<=[.!?।])\s+")
_FOREIGN_WORD = re.compile(r"(?i)\bforeign\b")


def clean_text(text: str) -> tuple[str, dict]:
    """Clean saved voice-to-text (an old 'As told' section). Returns (text, stats): words, kept, lost (words
    in a wrong script/language), loops (words removed as repeats), phantom (invented lines), tags ('foreign',
    [Music]...), lost_share (of what was really said, the share that came out in a wrong script/language)."""
    text = text or ""
    stats = {"words": len(text.split()), "lost": 0, "loops": 0, "phantom": 0, "tags": len(_TAGS.findall(text))}
    paragraphs: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        parts: list[str] = []
        last = None
        for sent in SENTENCE.split(para.strip()):
            if not sent.strip():
                continue
            c, kind, removed = _clean_piece(sent)
            stats["loops"] += removed
            if kind == "phantom":
                stats["phantom"] += 1
            elif kind == "lost":
                stats["lost"] += len(sent.split())
            if kind == "ok" and last is not None and _norm(c) == _norm(last):
                stats["loops"] += len(c.split())
                continue
            if c:
                parts.append(c)
            if kind == "ok":
                last = c
        joined, removed = collapse_loops(" ".join(_merge_unclear(parts)))
        stats["loops"] += removed
        if joined.strip():
            paragraphs.append(joined.strip())
    cleaned = "\n\n".join(_merge_unclear(paragraphs)).strip()
    stats["tags"] += max(0, len(_FOREIGN_WORD.findall(text)) - len(_FOREIGN_WORD.findall(cleaned)))
    stats["kept"] = words(cleaned)
    said = stats["kept"] + stats["lost"]  # what was really said (repeats and invented lines aren't)
    stats["lost_share"] = round(stats["lost"] / said, 2) if said else 0.0
    return cleaned, stats


def damaged(stats: dict) -> bool:
    return bool(stats["lost"] or stats["loops"] >= 6 or stats["phantom"] or stats["tags"])


# ---------------------------------------------------------------- vocabulary

VOCAB_FILE = "vocabulary.txt"
VOCAB_TEMPLATE = """# Words for the voice-to-text: names, places, nicknames, exams, colleges, slang.
# One per line. Whisper is told these before it listens, and Claude uses them to fix mistakes.
# If Whisper keeps hearing something wrong, write:  wrong -> right
#
# Examples (delete the # to use them):
# Mania -> Manya
# hatty -> IIT
# JEE Advanced
"""


def vocab_path(cfg: Config) -> Path:
    return cfg.library_dir / VOCAB_FILE


def vocabulary(cfg: Config, conn=None) -> dict:
    """{terms (your file), fixes {wrong: right}, people, places, folders, eras}. People includes the
    ones you've hidden: they are still named in your stories (this list never leaves the Mac for Whisper)."""
    terms, fixes = [], {}
    p = vocab_path(cfg)
    if not p.exists() and cfg.library_dir.is_dir():
        try:
            p.write_text(VOCAB_TEMPLATE, encoding="utf-8")
        except OSError:
            pass
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            m = re.split(r"\s*(?:->|→|=>)\s*", line, maxsplit=1)
            if len(m) == 2 and m[0] and m[1]:
                fixes[m[0].strip()] = m[1].strip()
                terms.append(m[1].strip())
            else:
                terms.append(line)
    people, places, folders = [], [], []
    if conn is not None:
        people = [r[0] for r in conn.execute(
            "SELECT p.name, sum(p.faces) n FROM people p WHERE p.name IS NOT NULL AND trim(p.name) != '' "
            "GROUP BY lower(p.name) ORDER BY n DESC LIMIT 120")]
        count: Counter = Counter()
        for place, n in conn.execute("SELECT place, count(*) FROM media WHERE place IS NOT NULL GROUP BY place"):
            for part in str(place).split(",")[:3]:
                part = part.strip()
                if part and not re.search(r"\d", part) and len(part) > 2:
                    count[part] += n
        places = [x for x, _ in count.most_common(80)]
        fcount: Counter = Counter()
        for col, n in conn.execute("SELECT collection, count(*) FROM media WHERE collection IS NOT NULL "
                                   "GROUP BY collection"):
            name = re.sub(r"[_\-]+", " ", re.sub(r"\b\d{1,4}([./-]\d{1,2}){0,2}\b", " ", str(col))).strip()
            if len(name) > 2:
                fcount[re.sub(r"\s+", " ", name)] += n
        folders = [x for x, _ in fcount.most_common(40)]
    try:
        from .memories import eras

        era_names = eras(cfg)
    except Exception:  # noqa: BLE001 - no vault yet
        era_names = []
    return {"terms": _uniq(terms), "fixes": fixes, "people": _uniq(people), "places": _uniq(places),
            "folders": _uniq(folders), "eras": era_names}


def _uniq(xs) -> list[str]:
    seen, out = set(), []
    for x in xs:
        k = str(x).strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(str(x).strip())
    return out


def vocab_terms(vocab: dict) -> set[str]:
    return {x.lower() for k in ("terms", "people", "places", "folders") for x in vocab.get(k, [])}


def prompt_for(vocab: dict, extra: list[str] | None = None, limit: int = 700) -> str:
    """Whisper's prompt: a list of names it should spell like this. Whisper keeps the END of a long prompt,
    so the most important words go last: places, folders, people, your own words, then this event."""
    words = _uniq(vocab.get("places", [])[:30] + vocab.get("folders", [])[:15] + vocab.get("people", [])[:50]
                  + vocab.get("terms", []) + (extra or []))
    text = ", ".join(words)
    if len(text) > limit:
        text = text[-limit:].split(", ", 1)[-1]
    return text + ("." if text else "")


# ---------------------------------------------------------------- audio

def load_wav(path: Path):
    """16 kHz mono float32 samples."""
    import numpy as np
    import soundfile as sf

    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    data = data.mean(axis=1)
    if sr != SR:  # _wav always writes 16 kHz; this is only a safety net
        idx = np.linspace(0, len(data) - 1, int(len(data) * SR / sr)).astype(int)
        data = data[idx]
    return np.ascontiguousarray(data, dtype="float32")


def _frame_db(samples, hop: int = 800):
    """Loudness (dBFS) of every 50 ms frame."""
    import numpy as np

    n = len(samples) // hop
    if n == 0:
        return np.array([-120.0])
    frames = samples[:n * hop].reshape(n, hop)
    rms = np.sqrt((frames.astype("float64") ** 2).mean(axis=1)) + 1e-9
    return 20 * np.log10(rms)


def loudest_db(samples) -> float:
    import numpy as np

    return float(20 * np.log10(float(np.abs(samples).max()) + 1e-9)) if len(samples) else float("-inf")


def quiet_point(samples, t: float, around: float = 0.75) -> float:
    """The quietest moment within `around` seconds of t (where to cut without splitting a word)."""
    import numpy as np

    db = _frame_db(samples)
    a, b = max(0, int((t - around) * 20)), min(len(db), int((t + around) * 20) + 1)
    if b <= a:
        return t
    return (a + int(np.argmin(db[a:b]))) / 20.0


def speech_pieces(samples, max_len: float = MAX_PIECE) -> list[tuple[float, float]]:
    """(start, end) seconds of the parts with speech, each at most max_len, cut at quiet moments."""
    import numpy as np

    db = _frame_db(samples)
    if len(samples) == 0 or loudest_db(samples) < SILENCE_DB:
        return []
    floor, peak = float(np.percentile(db, 20)), float(db.max())
    voiced = db > max(-55.0, min(floor + 10.0, peak - 30.0))  # rather too much than a lost word
    if not voiced.any():
        voiced[:] = True
    regions, i = [], 0
    while i < len(voiced):  # voiced runs, with gaps under 0.6 s bridged
        if voiced[i]:
            j = i
            while j < len(voiced) and (voiced[j] or voiced[j:j + 12].any()):
                j += 1
            regions.append([max(0, i - 5), min(len(db), j + 5)])  # 0.25 s padding
            i = j
        else:
            i += 1
    pieces: list[list[int]] = []
    limit = int(max_len * 20)
    for a, b in regions:
        if pieces and b - pieces[-1][0] <= limit and a - pieces[-1][1] <= 60:  # keep context: up to 3 s gaps
            pieces[-1][1] = b
            continue
        while b - a > limit:  # too long: cut at the quietest frame in the second half
            lo = a + limit // 2
            cut = lo + int(np.argmin(db[lo:a + limit]))
            pieces.append([a, cut])
            a = cut
        pieces.append([a, b])
    total = len(samples) / SR
    return [(a / 20.0, min(total, b / 20.0)) for a, b in pieces if b - a >= 6]  # >= 0.3 s


# ---------------------------------------------------------------- Whisper

_lock = threading.Lock()
_model_ok: dict[str, bool] = {}


QUALITY = ("fast", "best")
TEMPERATURES = (0.0, 0.4)  # a piece that comes out as a loop is decoded once more, not five times


def model_for(cfg: Config, quality: str = "fast") -> str:
    """fast: the normal (turbo) model. best: the big memory model (large-v3), several times slower."""
    return (cfg.memory_whisper_model or cfg.whisper_model) if quality == "best" else cfg.whisper_model


def _whisper(cfg: Config, audio, quality: str = "fast", **kw) -> dict:
    """mlx_whisper.transcribe with the model for this quality (falls back to the normal one if the big one
    can't be loaded, e.g. no internet for the first download). Options this mlx-whisper doesn't know are
    left out."""
    import mlx_whisper  # type: ignore

    try:
        params = inspect.signature(mlx_whisper.transcribe).parameters
        open_kw = any(p.kind is p.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        params, open_kw = {}, True
    base = ("language", "task", "condition_on_previous_text", "verbose", "path_or_hf_repo")
    kw = {k: v for k, v in kw.items() if k in params or k in base or (open_kw and k in ("language", "task"))}
    models = [m for m in dict.fromkeys([model_for(cfg, quality), cfg.whisper_model]) if _model_ok.get(m) is not False]
    err: Exception | None = None
    for m in models or [cfg.whisper_model]:
        try:
            with _lock:  # one at a time: the model is big
                res = mlx_whisper.transcribe(audio, path_or_hf_repo=m, **kw)
            _model_ok[m] = True
            return res
        except Exception as e:  # noqa: BLE001 - model missing / can't download: try the next one
            if _model_ok.get(m):
                raise
            _model_ok[m] = False
            err = e
    raise RuntimeError(f"Whisper couldn't load a model: {err}")


def _mode(mode: str) -> tuple[str, str]:
    mode = mode if mode in MODES else LEGACY.get(mode, "mixed")
    return MODES.get(mode, MODES["mixed"])


def transcribe_piece(cfg: Config, samples, a: float, b: float, mode: str = "mixed", prompt: str = "",
                     vocab_terms_: set[str] | None = None, quality: str = "fast") -> list[dict]:
    """Cleaned [{start, end, text}] (seconds in `samples`) for the stretch a..b (at most ~30 s)."""
    chunk = samples[int(a * SR):int(b * SR)]
    if len(chunk) == 0 or loudest_db(chunk) < SILENCE_DB:
        return []
    language, task = _mode(mode)
    res = _whisper(cfg, chunk, quality=quality, language=language, task=task, initial_prompt=prompt or None,
                   condition_on_previous_text=False, verbose=None, temperature=TEMPERATURES,
                   compression_ratio_threshold=2.4, logprob_threshold=-1.0, no_speech_threshold=0.6)
    segs = [dict(x, start=float(x.get("start") or 0) + a, end=float(x.get("end") or 0) + a)
            for x in res.get("segments", [])]
    if not segs and (res.get("text") or "").strip():
        segs = [{"start": a, "end": b, "text": res["text"]}]
    return clean_segments(segs, vocab_terms_)


def merge_unclear(segs: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for s in segs:  # [unclear] next to [unclear] once
        if merged and s["text"] == UNCLEAR and merged[-1]["text"] == UNCLEAR:
            merged[-1]["end"] = s["end"]
        else:
            merged.append(s)
    return merged


def transcribe_samples(cfg: Config, samples, mode: str = "mixed", prompt: str = "", offset: float = 0.0,
                       vocab_terms_: set[str] | None = None, progress=None, quality: str = "fast") -> list[dict]:
    """[{start, end, text}] (seconds + offset) for 16 kHz samples, piece by piece."""
    out: list[dict] = []
    pieces = speech_pieces(samples)
    for k, (a, b) in enumerate(pieces):
        if progress:
            progress(k, len(pieces))
        out += [dict(x, start=x["start"] + offset, end=x["end"] + offset)
                for x in transcribe_piece(cfg, samples, a, b, mode, prompt, vocab_terms_, quality)]
    return merge_unclear(out)


def words(text: str) -> int:
    return len([w for w in (text or "").split() if w != UNCLEAR])
