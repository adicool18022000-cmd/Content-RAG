"""Transition library: ready-made "flash / light leak + SFX" recipes imported from a Premiere template.

The creator's template sequence (FCP7 XML, e.g. FlashLeak_SFX_9x16_Vertical.xml) has one recipe per
marker ("T04 RISER INTO FLASH - CUT"): a plate on V2 (a range of a flash/light-leak clip, Screen
blend) and 1-3 sound effects on A1-A3, each placed relative to the cut with its own level.

    crag assets import <template.xml> [--media <folder with the plates and SFX>]

copies the plates and SFX into library/assets/transitions/ and writes transitions.json. The edit
engine then drops whole recipes onto cuts (hook out, B-roll changes, payoff, music drops) instead
of a random leak + a random SFX, keeping the exact offsets and levels from the template.
"""

from __future__ import annotations

import json
import random
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlparse

from ..config import Config
from .plan import Clip, EditPlan, Sound
from .style import assets_dir

MEDIA_EXT = {".mp4", ".mov", ".m4v", ".mp3", ".wav", ".aif", ".aiff", ".m4a", ".aac"}

# which plain sfx/<kind> folder a template sound also feeds (used when no recipe applies)
SFX_KIND = [("riser", "riser"), ("charge", "riser"), ("drop", "impact"), ("woosh", "whoosh"),
            ("whoosh", "whoosh"), ("camera", "shutter"), ("old cam", "shutter"), ("click", "pop"),
            ("beep", "pop"), ("flick", "pop"), ("flash", "impact")]

# Where in a reel each kind of recipe fits. Feels are derived from the recipe's sounds and plate.
USE_FOR = {
    "payoff": ["riser", "hard"],         # builds into the cut: payoff line, music drop
    "drop": ["riser", "hard"],
    "hook": ["hard", "quick"],           # out of the cold-open hook into the face
    "section": ["soft", "quick"],        # music energy change / new part of the story
    "broll": ["quick", "soft"],          # B-roll insert over the talking head
}


def library_dir(cfg: Config) -> Path:
    return assets_dir(cfg) / "transitions"


def library_path(cfg: Config) -> Path:
    return library_dir(cfg) / "transitions.json"


def load_library(cfg: Config) -> list[dict]:
    try:
        recipes = json.loads(library_path(cfg).read_text())["recipes"]
    except (OSError, ValueError, KeyError):
        return []
    base = library_dir(cfg)
    return [r for r in recipes if (base / r["plate"]).exists()]


# ---------------------------------------------------------------- import

def _num(el, tag, default=0.0) -> float:
    try:
        return float(el.findtext(tag))
    except (TypeError, ValueError):
        return default


def _level(clip) -> float:
    """Linear gain from Premiere's Audio Levels filter (0.794 = -2 dB)."""
    for p in clip.iter("parameter"):
        if (p.findtext("parameterid") or "").lower() == "level":
            try:
                return float(p.findtext("value"))
            except (TypeError, ValueError):
                pass
    return 1.0


def _feels(name: str, plate: str, sounds: list[dict]) -> list[str]:
    names = " ".join(s["name"].lower() for s in sounds)
    feels = []
    if any(s["name"].lower().startswith(("riser", "charge")) and s["offset"] < -0.3 for s in sounds):
        feels.append("riser")
    if "drop" in names or "flash" in names or len(sounds) >= 3 or "charge" in names:
        feels.append("hard")
    if "leak" in plate.lower() or "music box" in names or "soft" in name.lower() or "dream" in name.lower():
        feels.append("soft")
    span = max((s["offset"] + s["length"] for s in sounds), default=0) - min((s["offset"] for s in sounds), default=0)
    if not feels or (span <= 1.0 and "riser" not in feels):
        feels.append("quick")
    if any(k in names for k in ("hi-tech", "mouse", "beep")):
        feels.append("tech")
    if "old cam" in names or "film" in name.lower():
        feels.append("film")
    return sorted(set(feels))


def parse_template(xml_path: Path) -> tuple[list[dict], dict[str, str]]:
    """Recipes + {file name: original path} from a Premiere/FCP7 template sequence."""
    root = ET.parse(xml_path).getroot()
    seq = root.find(".//sequence")
    if seq is None:
        raise ValueError("no <sequence> in this XML")
    fps = _num(seq.find("rate"), "timebase", 30.0) or 30.0
    paths: dict[str, str] = {}
    by_id: dict[str, str] = {}
    for f in root.iter("file"):
        url = f.findtext("pathurl")
        if url:
            name = f.findtext("name") or Path(unquote(urlparse(url).path)).name
            by_id[f.get("id")] = name
            paths[name] = unquote(urlparse(url).path)

    def key(name: str) -> str | None:
        m = re.match(r"\s*(T\d+)\b", name or "")
        return m.group(1) if m else None

    cuts: dict[str, tuple[float, str]] = {}
    for m in seq.iter("marker"):
        k = key(m.findtext("name"))
        if k:
            title = re.sub(r"^\s*T\d+\s*", "", m.findtext("name") or "").replace(" - CUT", "").strip()
            cuts[k] = (_num(m, "in"), title)
    recipes: dict[str, dict] = {}
    media = seq.find("media")
    for track in media.find("video").findall("track") if media.find("video") is not None else []:
        for c in track.findall("clipitem"):
            k = key(c.findtext("name"))
            fid = c.find("file").get("id") if c.find("file") is not None else None
            if not k or k not in cuts or fid not in by_id:
                continue
            cut, title = cuts[k]
            recipes[k] = {"id": k, "name": title.title(), "plate": f"media/{by_id[fid]}",
                          "plate_in": round(_num(c, "in") / fps, 3), "plate_out": round(_num(c, "out") / fps, 3),
                          "offset": round((_num(c, "start") - cut) / fps, 3),
                          "blend": (c.findtext("compositemode") or "screen").lower(), "sounds": []}
    for track in media.find("audio").findall("track") if media.find("audio") is not None else []:
        for c in track.findall("clipitem"):
            k = key(c.findtext("name"))
            fid = c.find("file").get("id") if c.find("file") is not None else None
            if k not in recipes or fid not in by_id:
                continue
            cut = cuts[k][0]
            recipes[k]["sounds"].append({
                "name": re.sub(r"^\s*T\d+\s*", "", c.findtext("name") or "").strip(),
                "file": f"media/{by_id[fid]}", "offset": round((_num(c, "start") - cut) / fps, 3),
                "src_in": round(_num(c, "in") / fps, 3), "length": round((_num(c, "out") - _num(c, "in")) / fps, 3),
                "gain": round(_level(c), 4)})
    out = []
    for k in sorted(recipes, key=lambda x: int(x[1:])):
        r = recipes[k]
        r["sounds"].sort(key=lambda s: s["offset"])
        r["feels"] = _feels(r["name"], r["plate"], r["sounds"])
        out.append(r)
    return out, paths


def _find(name: str, original: str, search: list[Path]) -> Path | None:
    p = Path(original)
    if p.is_file():
        return p
    for d in search:
        if (d / name).is_file():
            return d / name
        hit = next((q for q in d.rglob(name) if q.is_file()), None) if d.is_dir() else None
        if hit:
            return hit
    return None


def import_template(cfg: Config, xml_path: Path, media: Path | None = None, log=print) -> dict:
    """Copy a template's plates + SFX into the library and (re)write transitions.json."""
    xml_path = Path(xml_path).expanduser()
    recipes, paths = parse_template(xml_path)
    if not recipes:
        raise SystemExit("No transitions found (expected markers named like 'T01 SNAP - CUT').")
    dest = library_dir(cfg) / "media"
    dest.mkdir(parents=True, exist_ok=True)
    search = [d for d in (media and Path(media).expanduser(), xml_path.parent, xml_path.parent.parent) if d]
    missing = []
    for name, original in sorted(paths.items()):
        if (dest / name).exists():
            continue
        src = _find(name, original, search)
        if src is None:
            missing.append(name)
            continue
        shutil.copy2(src, dest / name)
        low = name.lower()
        kind = next((k for word, k in SFX_KIND if word in low), None)
        if kind and Path(name).suffix.lower() in {".mp3", ".wav", ".aif", ".aiff", ".m4a", ".aac"}:
            (assets_dir(cfg) / "sfx" / kind).mkdir(parents=True, exist_ok=True)
            if not (assets_dir(cfg) / "sfx" / kind / name).exists():
                shutil.copy2(src, assets_dir(cfg) / "sfx" / kind / name)
    library_path(cfg).write_text(json.dumps({"source": str(xml_path), "recipes": recipes}, indent=2,
                                            ensure_ascii=False))
    usable = load_library(cfg)
    log(f"[transitions] {len(recipes)} recipes, {len(usable)} usable now → {library_dir(cfg)}")
    if missing:
        log(f"[transitions] not found ({len(missing)}): {', '.join(missing)}. Copy them next to the XML "
            "or pass --media <folder>, then import again.")
    return {"recipes": len(recipes), "usable": len(usable), "missing": missing, "folder": str(library_dir(cfg))}


# ---------------------------------------------------------------- use in edits

def choose(recipes: list[dict], moment: str, seed: str, avoid: set[str] = frozenset(),
           prefer: list[str] | None = None) -> dict | None:
    """A recipe that fits this moment, varied but reproducible (same seed -> same pick)."""
    if not recipes:
        return None
    pool = [r for r in recipes if not prefer or r["id"] in prefer] or recipes
    for feel in USE_FOR.get(moment, ["quick"]):
        cands = [r for r in pool if feel in r["feels"] and r["id"] not in avoid] or \
                [r for r in pool if feel in r["feels"]]
        if cands:
            return random.Random(f"{moment}:{seed}").choice(cands)
    return random.Random(seed).choice(pool)


def apply(cfg: Config, plan: EditPlan, recipe: dict, cut: float, sfx_volume: float = 1.0,
          opacity: float = 1.0) -> None:
    """Place a recipe so its cut lands on `cut` (seconds on the output timeline)."""
    base = library_dir(cfg)
    at = cut + recipe["offset"]
    src_in = recipe["plate_in"] + max(0.0, -at)  # a transition at 0:00 loses only its pre-roll
    if recipe["plate_out"] - src_in > 0.05:
        plan.clips.append(Clip("overlay", str(base / recipe["plate"]), round(src_in, 3), recipe["plate_out"],
                               round(max(0.0, at), 3), role="transition", blend=recipe.get("blend", "screen"),
                               volume=opacity, note=f"{recipe['id']} {recipe['name']}"))
    for s in recipe["sounds"]:
        f = base / s["file"]
        if not f.exists():
            continue
        t = cut + s["offset"]
        skip = max(0.0, -t)
        if s["length"] - skip <= 0.05:
            continue
        plan.sounds.append(Sound(round(max(0.0, t), 3), str(f), round(s["gain"] * sfx_volume, 3), "sfx",
                                 round(s["src_in"] + skip, 3), round(s["src_in"] + s["length"], 3)))


def summary(cfg: Config) -> list[str]:
    return [f"{r['id']} {r['name']} [{', '.join(r['feels'])}]: " + " + ".join(s["name"] for s in r["sounds"])
            for r in load_library(cfg)]
