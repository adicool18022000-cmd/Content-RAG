"""Render an edit plan.

materialize(): cut every timeline piece once, at its exact length and 9:16 (media/NNN_track.mp4).
render_mp4(): layers them over a black base with ffmpeg (B-roll over A-roll, light leaks blended
on top), applies the grade, burns styled captions (ASS) and mixes voice, clip sound, SFX and music.
Captions are also written as captions.srt / captions.ass for Premiere and others.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ..pull import _encoder, cut_clip
from .plan import Caption, EditPlan


# ---------------------------------------------------------------- materialize

def _has_audio(path: Path) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(r.stdout.strip())


def materialize(plan: EditPlan, out_dir: Path, log=print) -> list[dict]:
    """[{clip_index, path, audio}] - each clip cut exactly to its range, normalised to the plan's frame."""
    media = out_dir / "media"
    media.mkdir(parents=True, exist_ok=True)
    pieces = []
    for i, c in enumerate(plan.clips):
        out = media / f"{i:03d}_{c.track}.mp4"
        want_audio = c.audio or c.track == "aroll"
        if not out.exists():
            ok, err = cut_clip(Path(c.file), c.src_in, c.src_out, out,
                               reframe="crop" if c.track != "broll" else (c.reframe or "crop"),
                               fps=plan.fps, audio=want_audio, width=plan.width, height=plan.height)
            if not ok:
                raise RuntimeError(f"couldn't cut {Path(c.file).name} {c.src_in}-{c.src_out}: {err}")
        pieces.append({"index": i, "path": out, "audio": want_audio and _has_audio(out)})
    log(f"[render] {len(pieces)} pieces ready in {media}")
    return pieces


# ---------------------------------------------------------------- captions

def _ass_color(hex_color: str, alpha: int = 0) -> str:
    h = (hex_color or "#FFFFFF").lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ts(t: float, ass: bool) -> str:
    h, rem = divmod(max(0.0, t), 3600)
    m, s = divmod(rem, 60)
    if ass:
        return f"{int(h)}:{int(m):02d}:{s:05.2f}"
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s - int(s)) * 1000)):03d}"


def write_srt(captions: list[Caption], path: Path) -> Path:
    path.write_text("\n".join(f"{i}\n{_ts(c.start, False)} --> {_ts(c.end, False)}\n{c.text}\n"
                              for i, c in enumerate(captions, 1)), encoding="utf-8")
    return path


def write_ass(plan: EditPlan, path: Path) -> Path:
    cs = plan.caption_style or {}
    w, h = plan.width, plan.height
    margin_v = int(h * (1 - float(cs.get("position", 0.68))))
    outline = max(0, int(cs.get("stroke_width", 6)) // 2)
    hi = _ass_color(cs.get("highlight_color", "#FFD400"))
    base = _ass_color(cs.get("color", "#FFFFFF"))
    pop = "{\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)}" if cs.get("animation", "pop") == "pop" else ""
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 0", "",
             "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
             "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
             "MarginL, MarginR, MarginV, Encoding",
             f"Style: Cap,{cs.get('font', 'Montserrat ExtraBold')},{int(cs.get('size', 74))},{base},{base},"
             f"{_ass_color(cs.get('stroke_color', '#000000'))},&H64000000,-1,0,0,0,100,100,0,0,1,{outline},0,2,"
             f"80,80,{margin_v},1", "", "[Events]",
             "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for c in plan.captions:
        text = c.text.replace("\n", " ")
        for word in c.emphasis:
            text = text.replace(word, f"{{\\c{hi}}}{word}{{\\c{base}}}", 1)
        lines.append(f"Dialogue: 0,{_ts(c.start, True)},{_ts(c.end, True)},Cap,,0,0,0,,{pop}{text}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- mp4

def _grade_filter(grade: dict, out_dir: Path) -> list[str]:
    f = []
    g = grade or {}
    if any(abs(float(g.get(k, d)) - d) > 1e-6 for k, d in (("contrast", 1.0), ("saturation", 1.0), ("brightness", 0.0))):
        f.append(f"eq=contrast={float(g.get('contrast', 1.0)):.3f}:saturation={float(g.get('saturation', 1.0)):.3f}"
                 f":brightness={float(g.get('brightness', 0.0)):.3f}")
    if abs(float(g.get("warmth", 0.0))) > 1e-6:
        f.append(f"colortemperature=temperature={int(6500 - 2500 * float(g['warmth']))}")
    if g.get("lut") and Path(g["lut"]).exists():
        shutil.copyfile(g["lut"], out_dir / "grade.cube")
        f.append("lut3d=grade.cube")
    if g.get("vignette"):
        f.append("vignette=PI/5")
    return f


def render_mp4(plan: EditPlan, out_dir: Path, pieces: list[dict], out_name: str = "preview.mp4", log=print) -> Path:
    plan.fix_duration()
    D, W, H, fps = plan.duration, plan.width, plan.height, plan.fps
    order = {"aroll": 0, "broll": 1, "overlay": 2}
    layered = sorted(pieces, key=lambda p: (order.get(plan.clips[p["index"]].track, 1), plan.clips[p["index"]].at))
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=black:s={W}x{H}:r={fps}:d={D:.3f}"]
    for p in layered:
        cmd += ["-i", str(p["path"].relative_to(out_dir))]
    snd_inputs = []
    for s in plan.sounds:
        cmd += ["-i", s.file]
        snd_inputs.append(s)
    graph, cur = [], "0:v"
    for j, p in enumerate(layered, 1):
        c = plan.clips[p["index"]]
        src = f"[{j}:v]"
        if c.track == "overlay":  # dark parts transparent ~ "screen" blend for light leaks
            src += (f"scale={W}:{H},setsar=1,format=rgba,colorkey=0x000000:0.28:0.25,"
                    f"colorchannelmixer=aa={max(0.0, min(1.0, c.volume)):.2f},")
        graph.append(f"{src}setpts=PTS-STARTPTS+{c.at:.3f}/TB[v{j}]")
        graph.append(f"[{cur}][v{j}]overlay=eof_action=pass:enable='between(t,{c.at:.3f},{c.end:.3f})'[o{j}]")
        cur = f"o{j}"
    post = _grade_filter(plan.grade, out_dir)
    if plan.captions:
        write_ass(plan, out_dir / "captions.ass")
        fonts = out_dir / "fonts"
        post.append("ass=captions.ass" + (":fontsdir=fonts" if fonts.is_dir() else ""))
    post.append("format=yuv420p")
    graph.append(f"[{cur}]{','.join(post)}[vout]")

    audio = []
    for j, p in enumerate(layered, 1):
        c = plan.clips[p["index"]]
        if p["audio"]:
            ms = int(c.at * 1000)
            audio.append(f"[{j}:a]aresample=48000,adelay={ms}|{ms},volume={c.volume:.2f}[a{j}]")
    for k, s in enumerate(snd_inputs):
        idx = 1 + len(layered) + k
        trim = f"atrim=start={s.src_in:.3f}" + (f":end={s.src_out:.3f}" if s.src_out else "") + ",asetpts=PTS-STARTPTS,"
        ms = int(s.at * 1000)
        audio.append(f"[{idx}:a]aresample=48000,{trim}adelay={ms}|{ms},volume={s.volume:.2f}[s{k}]")
    labels = [a.rsplit("[", 1)[1].rstrip("]") for a in audio]
    if labels:
        graph += audio
        graph.append("".join(f"[{x}]" for x in labels)
                     + f"amix=inputs={len(labels)}:normalize=0:duration=longest,alimiter=limit=0.95,"
                       f"apad,atrim=0:{D:.3f}[aout]")
    else:
        graph.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{D:.3f}[aout]")
    (out_dir / "render.filtergraph").write_text(";\n".join(graph), encoding="utf-8")
    cmd += ["-filter_complex_script", "render.filtergraph", "-map", "[vout]", "-map", "[aout]",
            "-t", f"{D:.3f}", "-r", str(fps), *_encoder(), "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            out_name]
    r = subprocess.run(cmd, cwd=out_dir, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg render failed: {r.stderr.strip()[-800:]}")
    log(f"[render] {out_dir / out_name}")
    return out_dir / out_name


def summary(plan: EditPlan) -> dict:
    return {"duration": plan.duration, "aroll_parts": len(plan.track("aroll")), "broll": len(plan.track("broll")),
            "overlays": len(plan.track("overlay")), "captions": len(plan.captions), "sounds": len(plan.sounds),
            "shoot_list": plan.shoot_list}


def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
