"""Editing styles (one or more per Instagram page) and the assets they use.

A style is a JSON file in library/styles/<name>.json (+ a readable note in the vault, Styles/).
Create one from example reels: `crag style learn <name> ref1.mp4 ref2.mp4 --page <page>`; Gemini
describes captions, grade, transitions, SFX, pacing and hook structure, and the real cut rate is
measured locally. Edit the JSON (or ask Claude) to tweak it; experiment with variants
(`page1-fast`, `page1-cinematic`) and compare them with `crag videos --by-style`.

Assets (your own files) live in library/assets/:
    light_leaks/   colour or light leak clips (dark background; blended "screen")
    sfx/riser/  sfx/shutter/  sfx/whoosh/  sfx/impact/  sfx/pop/   sound effects
    luts/          .cube colour LUTs
    fonts/         .ttf/.otf caption fonts
"""

from __future__ import annotations

import copy
import json
import random
import re
import subprocess
import tempfile
from pathlib import Path

from ..config import Config

ASSET_DIRS = ["light_leaks", "sfx/riser", "sfx/shutter", "sfx/whoosh", "sfx/impact", "sfx/pop", "luts", "fonts", "music"]
MEDIA_EXT = {".mp4", ".mov", ".m4v", ".webm"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".aiff", ".aif", ".flac", ".ogg"}

DEFAULT_STYLE: dict = {
    "name": "default",
    "page": None,
    "description": "Clean talking-head reel: hook first, B-roll every few seconds, bold captions.",
    "pace": {
        "jump_cut_silence": 0.35,     # remove pauses longer than this (s) from the talking head
        "keep_padding": 0.08,         # breath kept around each spoken part (s)
        "broll_every": 3.5,           # aim for a B-roll insert about this often (s)
        "broll_min": 1.2, "broll_max": 2.8,
        "face_first_seconds": 2.0,    # show the face this long before the first B-roll
    },
    "hook": {"open_with_hook": True, "hook_length": 1.6, "hook_audio": True, "replay_hook_later": True},
    "captions": {
        "enabled": True, "script": "roman",        # roman (Hinglish in Latin letters) | devanagari | english
        "words_per_caption": 3, "uppercase": True,
        "font": "Montserrat ExtraBold", "font_file": None, "size": 74,
        "color": "#FFFFFF", "highlight_color": "#FFD400", "stroke_color": "#000000", "stroke_width": 6,
        "position": 0.68,                          # vertical position, 0 = top, 1 = bottom
        "animation": "pop",
    },
    "grade": {"lut": None, "contrast": 1.05, "saturation": 1.08, "brightness": 0.0, "warmth": 0.0, "vignette": False},
    "transitions": {"style": "leak", "where": "sections", "duration": 0.7, "opacity": 0.85},
    "sfx": {"on_broll": "whoosh", "on_transition": "shutter", "before_payoff": "riser", "on_hook": "impact",
            "volume": 0.6},
    "music": {"volume_under_voice": 0.12, "volume_alone": 0.9},
    "reframe": "crop",                             # crop | blur, for horizontal footage in 9:16
}


# ---------------------------------------------------------------- storage

def styles_dir(cfg: Config) -> Path:
    return cfg.library_dir / "styles"


def assets_dir(cfg: Config) -> Path:
    d = cfg.library_dir / "assets"
    for sub in ASSET_DIRS:
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_style(cfg: Config, name: str | None) -> dict:
    if not name or name == "default":
        return copy.deepcopy(DEFAULT_STYLE)
    path = styles_dir(cfg) / f"{_slug(name)}.json"
    if not path.exists():
        raise SystemExit(f"No style '{name}'. Create one: crag style learn {name} <example reels...>")
    return _merge(DEFAULT_STYLE, json.loads(path.read_text(encoding="utf-8")))


def save_style(cfg: Config, style: dict) -> Path:
    d = styles_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_slug(style['name'])}.json"
    path.write_text(json.dumps(style, ensure_ascii=False, indent=2), encoding="utf-8")
    _note(cfg, style)
    return path


def list_styles(cfg: Config) -> list[dict]:
    out = [{"name": "default", "page": None, "description": DEFAULT_STYLE["description"]}]
    for p in sorted(styles_dir(cfg).glob("*.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        out.append({"name": s.get("name", p.stem), "page": s.get("page"), "description": s.get("description", "")})
    return out


def _slug(name: str) -> str:
    return re.sub(r"[^\w-]+", "-", name.lower()).strip("-")[:60] or "style"


def _note(cfg: Config, style: dict) -> None:
    d = cfg.vault_dir / "Styles"
    d.mkdir(parents=True, exist_ok=True)
    c, g, t, p = style["captions"], style["grade"], style["transitions"], style["pace"]
    lines = ["---", "type: style", f"page: {style.get('page') or ''}", "---", f"# Style: {style['name']}", "",
             style.get("description", ""), "",
             f"- **Pace:** B-roll about every {p['broll_every']} s ({p['broll_min']}–{p['broll_max']} s long); "
             f"pauses over {p['jump_cut_silence']} s cut",
             f"- **Captions:** {c['words_per_caption']} words, {c['font']} {c['size']} px, {c['color']} with "
             f"{c['highlight_color']} highlight, {'UPPERCASE' if c['uppercase'] else 'normal case'}, {c['script']}",
             f"- **Grade:** contrast {g['contrast']}, saturation {g['saturation']}, warmth {g['warmth']}"
             + (f", LUT {Path(g['lut']).name}" if g.get("lut") else ""),
             f"- **Transitions:** {t['style']} at {t['where']}",
             f"- **SFX:** {', '.join(f'{k}={v}' for k, v in style['sfx'].items() if k != 'volume')}",
             f"- **Hook:** {'opens with a hook clip' if style['hook']['open_with_hook'] else 'starts on the face'}"]
    if style.get("analysis"):
        lines += ["", "## What the example reels do", style["analysis"]]
    if style.get("references"):
        lines += ["", "## Learned from", *[f"- {r}" for r in style["references"]]]
    (d / f"{_slug(style['name'])}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- assets

def pick_asset(cfg: Config, kind: str, seed: str = "") -> Path | None:
    """A random-but-stable file from library/assets/<kind> (e.g. 'sfx/whoosh', 'light_leaks')."""
    if not kind or kind == "none":
        return None
    folder = assets_dir(cfg) / (kind if "/" in kind or kind in ("light_leaks", "luts", "music") else f"sfx/{kind}")
    files = sorted(p for p in folder.glob("*") if p.suffix.lower() in MEDIA_EXT | AUDIO_EXT | {".cube"})
    if not files:
        return None
    return random.Random(f"{kind}:{seed}").choice(files)


def asset_report(cfg: Config) -> dict:
    d = assets_dir(cfg)
    return {sub: len([p for p in (d / sub).glob("*") if p.is_file() and not p.name.startswith(".")])
            for sub in ASSET_DIRS} | {"folder": str(d)}


# ---------------------------------------------------------------- learning from example reels

LEARN_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string", "description": "2-4 sentences describing the editing style."},
        "analysis": {"type": "string", "description": "Detailed notes: hook structure, pacing, captions, grade, "
                                                     "transitions, sound design, B-roll use, text overlays, zooms."},
        "broll_every": {"type": "number", "description": "Average seconds between B-roll inserts over the talking head."},
        "broll_length": {"type": "number", "description": "Typical B-roll insert length in seconds."},
        "jump_cuts": {"type": "boolean", "description": "Are pauses cut out of the talking head?"},
        "opens_with_hook_clip": {"type": "boolean"},
        "caption_words": {"type": "integer", "description": "Words shown at a time."},
        "caption_uppercase": {"type": "boolean"},
        "caption_font_style": {"type": "string", "description": "e.g. 'heavy rounded sans', 'serif italic', 'typewriter'."},
        "caption_color": {"type": "string", "description": "Hex colour of normal caption text."},
        "caption_highlight_color": {"type": "string", "description": "Hex colour of highlighted words, or same."},
        "caption_has_outline": {"type": "boolean"},
        "caption_position": {"type": "number", "description": "Vertical position 0 (top) to 1 (bottom)."},
        "caption_language": {"type": "string", "enum": ["roman", "devanagari", "english"]},
        "grade_contrast": {"type": "number", "description": "1.0 neutral; 0.8-1.3."},
        "grade_saturation": {"type": "number", "description": "1.0 neutral; 0.6-1.5."},
        "grade_warmth": {"type": "number", "description": "-1 cool .. 0 neutral .. 1 warm."},
        "grade_brightness": {"type": "number", "description": "-0.2 .. 0.2"},
        "vignette": {"type": "boolean"},
        "transition_style": {"type": "string", "enum": ["cut", "leak", "flash", "zoom", "whip", "mixed"]},
        "transitions_where": {"type": "string", "enum": ["sections", "every_broll", "rarely"]},
        "sfx_used": {"type": "array", "items": {"type": "string",
                                                 "enum": ["riser", "shutter", "whoosh", "impact", "pop", "none"]}},
        "music_under_voice": {"type": "boolean"},
    },
    "required": ["description", "analysis", "broll_every", "broll_length", "jump_cuts", "opens_with_hook_clip",
                 "caption_words", "caption_uppercase", "caption_font_style", "caption_color",
                 "caption_highlight_color", "caption_has_outline", "caption_position", "caption_language",
                 "grade_contrast", "grade_saturation", "grade_warmth", "grade_brightness", "vignette",
                 "transition_style", "transitions_where", "sfx_used", "music_under_voice"],
    "additionalProperties": False,
}

FONT_GUESS = [("serif", "Playfair Display Bold"), ("typewriter", "Courier Prime Bold"),
              ("hand", "Permanent Marker"), ("condensed", "Bebas Neue"), ("round", "Poppins ExtraBold"),
              ("", "Montserrat ExtraBold")]


def measure_cuts(path: Path) -> dict:
    """Objective pace of a reference reel: number of hard cuts per second."""
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "info", "-i", str(path), "-an", "-vf",
                        "scale=160:-2,select='gt(scene,0.3)',showinfo", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=600)
    cuts = len(re.findall(r"pts_time:", r.stderr))
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    seconds = int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3)) if dur else 0.0
    return {"cuts": cuts, "seconds": round(seconds, 1),
            "avg_shot": round(seconds / (cuts + 1), 2) if seconds else None}


def learn_style(cfg: Config, name: str, references: list[Path], page: str | None = None, log=print) -> dict:
    from .. import gemini

    measured = [measure_cuts(p) for p in references]
    with tempfile.TemporaryDirectory(prefix="crag-style-") as tmp:
        parts = []
        for p in references:
            parts += [f"Example reel: {p.name}", gemini.video_part(cfg, p, Path(tmp))]
        parts.append("These reels show the editing style the creator wants. Describe that style precisely so it "
                     "can be reproduced automatically. Measured hard cuts: "
                     + "; ".join(f"{p.name}: {m['cuts']} cuts in {m['seconds']} s" for p, m in zip(references, measured)))
        a = gemini.ask_json(cfg, parts, LEARN_SCHEMA,
                            system="You are a senior short-form video editor analysing reference reels.")
    font = next(f for key, f in FONT_GUESS if key in a["caption_font_style"].lower())
    sfx = set(a["sfx_used"]) - {"none"}
    style = _merge(DEFAULT_STYLE, {
        "name": name, "page": page, "description": a["description"], "analysis": a["analysis"],
        "references": [str(p) for p in references], "measured": measured,
        "pace": {"broll_every": round(max(1.0, a["broll_every"]), 2),
                 "broll_min": round(max(0.6, a["broll_length"] * 0.6), 2),
                 "broll_max": round(max(1.0, a["broll_length"] * 1.5), 2),
                 "jump_cut_silence": 0.3 if a["jump_cuts"] else 1.5},
        "hook": {"open_with_hook": a["opens_with_hook_clip"]},
        "captions": {"words_per_caption": max(1, min(6, a["caption_words"])), "uppercase": a["caption_uppercase"],
                     "font": font, "color": a["caption_color"], "highlight_color": a["caption_highlight_color"],
                     "stroke_width": 6 if a["caption_has_outline"] else 0,
                     "position": min(0.9, max(0.1, a["caption_position"])), "script": a["caption_language"]},
        "grade": {"contrast": a["grade_contrast"], "saturation": a["grade_saturation"], "warmth": a["grade_warmth"],
                  "brightness": a["grade_brightness"], "vignette": a["vignette"]},
        "transitions": {"style": "leak" if a["transition_style"] in ("leak", "mixed") else a["transition_style"],
                        "where": a["transitions_where"]},
        "sfx": {"on_broll": "whoosh" if "whoosh" in sfx else "none",
                "on_transition": "shutter" if "shutter" in sfx else ("whoosh" if "whoosh" in sfx else "none"),
                "before_payoff": "riser" if "riser" in sfx else "none",
                "on_hook": "impact" if "impact" in sfx else "none"},
        "music": {"volume_under_voice": 0.12 if a["music_under_voice"] else 0.0},
    })
    path = save_style(cfg, style)
    log(f"[style] saved {name} -> {path}")
    return style
