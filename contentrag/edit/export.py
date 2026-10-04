"""Project exports from one edit plan, for finishing in the editor of your choice.

premiere/   timeline.xml (Final Cut Pro 7 XML: File > Import in Premiere Pro) + captions.srt
            - references the ORIGINAL footage with in/out points, so every cut can still be trimmed
            - V1 talking head, V2 B-roll (scaled to fill 9:16), V3 light leaks; A1 voice, A2 clip
              sound, A3 SFX, A4 music. Import captions.srt as a caption track.
aftereffects/build_comp.jsx  File > Scripts > Run Script File... builds the comp (originals, scaled
            layers, leaks in Screen mode, caption text layers you can restyle, SFX/music levels)
hyperframes/  index.html + media/ (pre-cut clips) - `npx hyperframes preview` / `render`
remotion/     a Remotion project (pinned 4.0.532) - `npm i` then `npm run studio` / `npm run render`
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape

from .plan import EditPlan
from .render import write_srt

REMOTION_VERSION = "4.0.532"


def _probe(path: str, cache: dict) -> dict:
    if path not in cache:
        r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                           capture_output=True, text=True)
        info = json.loads(r.stdout or "{}")
        v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
        a = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
        w, h = (v or {}).get("width"), (v or {}).get("height")
        for sd in (v or {}).get("side_data_list", []) or []:
            if abs(int(float(sd.get("rotation", 0)))) % 180 == 90:
                w, h = h, w
        cache[path] = {"video": v is not None, "audio": a, "width": w or 1080, "height": h or 1920,
                       "duration": float(info.get("format", {}).get("duration", 0) or 0)}
    return cache[path]


# ---------------------------------------------------------------- Premiere (FCP7 XML)

def export_premiere(plan: EditPlan, out_dir: Path) -> Path:
    d = out_dir / "premiere"
    d.mkdir(parents=True, exist_ok=True)
    fps = plan.fps
    rate = f"<rate><timebase>{fps}</timebase><ntsc>FALSE</ntsc></rate>"
    fr = lambda s: int(round(s * fps))  # noqa: E731
    cache: dict = {}
    file_ids: dict[str, str] = {}

    def file_el(path: str) -> str:
        if path in file_ids:
            return f'<file id="{file_ids[path]}"/>'
        fid = file_ids[path] = f"file-{len(file_ids) + 1}"
        info = _probe(path, cache)
        media = ""
        if info["video"]:
            media += (f"<video><samplecharacteristics>{rate}<width>{info['width']}</width>"
                      f"<height>{info['height']}</height></samplecharacteristics></video>")
        if info["audio"]:
            media += "<audio><channelcount>2</channelcount></audio>"
        return (f'<file id="{fid}"><name>{escape(Path(path).name)}</name><pathurl>{escape(Path(path).as_uri())}'
                f"</pathurl>{rate}<duration>{fr(info['duration'])}</duration><media>{media}</media></file>")

    def scale_filter(path: str, mode: str | None) -> str:
        info = _probe(path, cache)
        s = (min if mode == "blur" else max)(plan.width / info["width"], plan.height / info["height"]) * 100
        return ("<filter><effect><name>Basic Motion</name><effectid>basic</effectid><effectcategory>motion"
                "</effectcategory><effecttype>motion</effecttype><mediatype>video</mediatype><parameter>"
                f"<parameterid>scale</parameterid><name>Scale</name><valuemin>0</valuemin><valuemax>1000</valuemax>"
                f"<value>{s:.2f}</value></parameter></effect></filter>")

    def clipitem(cid: str, path: str, at: float, src_in: float, src_out: float, extra: str = "",
                 audio_track: bool = False) -> str:
        start, length = fr(at), max(1, fr(src_out - src_in))
        body = (f'<clipitem id="{cid}"><name>{escape(Path(path).name)}</name><enabled>TRUE</enabled>'
                f"<duration>{fr(_probe(path, cache)['duration'])}</duration>{rate}<start>{start}</start>"
                f"<end>{start + length}</end><in>{fr(src_in)}</in><out>{fr(src_in) + length}</out>{file_el(path)}")
        if audio_track:
            body += "<sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>"
        return body + extra + "</clipitem>"

    vtracks = {"aroll": [], "broll": [], "overlay": []}
    atracks = {"voice": [], "clip": [], "sfx": [], "music": []}
    for i, c in enumerate(sorted(plan.clips, key=lambda c: c.at)):
        extra = scale_filter(c.file, c.reframe) if c.track != "overlay" else ""
        if c.track == "overlay":
            extra = ("<filter><effect><name>Opacity</name><effectid>opacity</effectid><effectcategory>motion"
                     "</effectcategory><effecttype>motion</effecttype><mediatype>video</mediatype><parameter>"
                     f"<parameterid>opacity</parameterid><name>opacity</name><value>{c.volume * 100:.0f}</value>"
                     "</parameter></effect></filter><compositemode>screen</compositemode>")
        vtracks[c.track].append(clipitem(f"v{i}", c.file, c.at, c.src_in, c.src_out, extra))
        if (c.track == "aroll" or c.audio) and _probe(c.file, cache)["audio"]:
            atracks["voice" if c.track == "aroll" else "clip"].append(
                clipitem(f"a{i}", c.file, c.at, c.src_in, c.src_out, audio_track=True))
    sfx_lanes: list[list[str]] = []
    lane_end: list[float] = []
    for k, s in sorted(enumerate(plan.sounds), key=lambda ks: ks[1].at):
        dur = _probe(s.file, cache)["duration"]
        end = min(s.src_out if s.src_out else dur, s.src_in + max(0.1, plan.duration - s.at))
        level = (f"<filter><effect><name>Audio Levels</name><effectid>audiolevels</effectid><effectcategory>"
                 f"audiolevels</effectcategory><effecttype>audiolevels</effecttype><mediatype>audio</mediatype>"
                 f"<parameter><parameterid>level</parameterid><name>Level</name><valuemin>0</valuemin><valuemax>"
                 f"3.98109</valuemax><value>{max(0.0, min(3.98, s.volume)):.4f}</value></parameter></effect></filter>")
        item = clipitem(f"s{k}", s.file, s.at, s.src_in, end, extra=level, audio_track=True)
        if s.kind == "music":
            atracks["music"].append(item)
            continue
        # clips on one track can't overlap: stacked SFX (riser + flash + click) get their own tracks
        lane = next((i for i, e in enumerate(lane_end) if e <= fr(s.at)), None)
        if lane is None:
            sfx_lanes.append([])
            lane_end.append(0)
            lane = len(sfx_lanes) - 1
        sfx_lanes[lane].append(item)
        lane_end[lane] = fr(s.at) + max(1, fr(end - s.src_in))
    plan.fix_duration()
    vxml = "".join(f"<track>{''.join(vtracks[t])}</track>" for t in ("aroll", "broll", "overlay"))
    axml = "".join(f"<track>{''.join(items)}</track>"
                   for items in [atracks["voice"], atracks["clip"], *(sfx_lanes or [[]]), atracks["music"]])
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4">'
           f'<sequence id="seq-1"><name>{escape(plan.name)}</name><duration>{fr(plan.duration)}</duration>{rate}'
           f"<media><video><format><samplecharacteristics>{rate}<width>{plan.width}</width>"
           f"<height>{plan.height}</height><pixelaspectratio>square</pixelaspectratio></samplecharacteristics>"
           f"</format>{vxml}</video><audio>{axml}</audio></media></sequence></xmeml>\n")
    (d / "timeline.xml").write_text(xml, encoding="utf-8")
    if plan.captions:
        write_srt(plan.captions, d / "captions.srt")
    (d / "README.txt").write_text(
        "Premiere Pro: File > Import > timeline.xml (opens as a sequence).\n"
        "Captions: File > Import > captions.srt, drag onto the sequence (caption track), then style them.\n"
        "Light leaks are on V3 with Screen blend; colour grade is not included - apply your LUT/Lumetri preset.\n",
        encoding="utf-8")
    return d / "timeline.xml"


# ---------------------------------------------------------------- After Effects (ExtendScript)

def _ps_font(name: str) -> str:
    parts = (name or "Montserrat ExtraBold").split()
    return parts[0] + ("-" + "".join(parts[1:]) if len(parts) > 1 else "")


def export_after_effects(plan: EditPlan, out_dir: Path) -> Path:
    d = out_dir / "aftereffects"
    d.mkdir(parents=True, exist_ok=True)
    plan.fix_duration()
    cs = plan.caption_style or {}
    j = json.dumps
    hexrgb = lambda h: [round(int((h or "#FFFFFF").lstrip("#")[k:k + 2], 16) / 255, 4) for k in (0, 2, 4)]  # noqa: E731
    lines = [
        "// Built by crag. In After Effects: File > Scripts > Run Script File... and pick this file.",
        "(function () {",
        f"var NAME = {j(plan.name)}, W = {plan.width}, H = {plan.height}, FPS = {plan.fps}, "
        f"DUR = {max(plan.duration, 0.1):.3f};",
        "app.beginUndoGroup('crag: ' + NAME);",
        "var proj = app.project || app.newProject();",
        "var folder = proj.items.addFolder(NAME + ' media');",
        "var cache = {};",
        "function item(p) {",
        "  if (cache[p]) return cache[p];",
        "  var f = new File(p);",
        "  if (!f.exists) { alert('Missing file: ' + p); return null; }",
        "  var it = proj.importFile(new ImportOptions(f)); it.parentFolder = folder; cache[p] = it; return it;",
        "}",
        "var comp = proj.items.addComp(NAME, W, H, 1, DUR, FPS);",
        "function addClip(p, at, inp, outp, mode, opts) {",
        "  var it = item(p); if (!it) return null;",
        "  var l = comp.layers.add(it);",
        "  l.startTime = at - inp; l.inPoint = at; l.outPoint = at + (outp - inp);",
        "  if (it.hasVideo && it.width) {",
        "    var s = (mode == 'blur' ? Math.min(W / it.width, H / it.height) : Math.max(W / it.width, H / it.height)) * 100;",
        "    l.property('Scale').setValue([s, s]);",
        "  }",
        "  if (opts.screen) { l.blendingMode = BlendingMode.SCREEN; l.property('Opacity').setValue(opts.opacity * 100); }",
        "  if (it.hasAudio) {",
        "    if (opts.mute) { l.audioEnabled = false; }",
        "    else if (opts.volume != 1) { var db = 20 * Math.log(Math.max(opts.volume, 0.001)) / Math.LN10;",
        "      l.property('Audio').property('Audio Levels').setValue([db, db]); }",
        "  }",
        "  if (opts.note) { l.comment = opts.note; }",
        "  return l;",
        "}",
        "function addCaption(txt, a, b, hi) {",
        "  var t = comp.layers.addText(txt);",
        "  var prop = t.property('Source Text'); var td = prop.value;",
        f"  td.font = {j(cs.get('ae_font') or _ps_font(cs.get('font', 'Montserrat ExtraBold')))};",
        f"  td.fontSize = {int(cs.get('size', 74))}; td.fillColor = {j(hexrgb(cs.get('color')))};",
        f"  td.applyStroke = {'true' if cs.get('stroke_width', 6) else 'false'}; "
        f"td.strokeColor = {j(hexrgb(cs.get('stroke_color', '#000000')))}; td.strokeWidth = {int(cs.get('stroke_width', 6))};",
        "  td.strokeOverFill = false; td.justification = ParagraphJustification.CENTER_JUSTIFY;",
        "  prop.setValue(td);",
        f"  t.property('Position').setValue([W / 2, H * {float(cs.get('position', 0.68)):.3f}]);",
        "  t.inPoint = a; t.outPoint = b; t.comment = hi;",
        "  return t;",
        "}",
        "// bottom to top: music, sfx, talking head, B-roll, leaks, captions",
    ]
    for s in plan.sounds:
        if s.kind == "music":
            lines.append(f"addClip({j(s.file)}, {s.at:.3f}, {s.src_in:.3f}, "
                         f"{(s.src_out or s.src_in + plan.duration):.3f}, 'fill', {{volume: {s.volume}}});")
    for s in plan.sounds:
        if s.kind != "music":
            out = s.src_out if s.src_out else s.src_in + 30
            lines.append(f"addClip({j(s.file)}, {s.at:.3f}, {s.src_in:.3f}, {out:.3f}, 'fill', "
                         f"{{volume: {s.volume}}});")
    for track in ("aroll", "broll", "overlay"):
        for c in sorted(plan.track(track), key=lambda c: c.at):
            opts = {"mute": not (c.audio or track == "aroll"), "volume": c.volume if track != "overlay" else 1,
                    "screen": track == "overlay", "opacity": c.volume, "note": c.note[:200]}
            lines.append(f"addClip({j(c.file)}, {c.at:.3f}, {c.src_in:.3f}, {c.src_out:.3f}, "
                         f"{j(c.reframe or 'crop')}, {j(opts)});")
    for c in plan.captions:
        lines.append(f"addCaption({j(c.text)}, {c.start:.3f}, {c.end:.3f}, {j(' '.join(c.emphasis))});")
    lines += ["comp.openInViewer();", "app.endUndoGroup();", "})();"]
    (d / "build_comp.jsx").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return d / "build_comp.jsx"


# ---------------------------------------------------------------- web-based: HyperFrames / Remotion

def _sound_length(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _materialize_sounds(plan: EditPlan, media: Path) -> list[str]:
    names = []
    for k, s in enumerate(plan.sounds):
        out = media / f"snd_{k:02d}.m4a"
        if not out.exists():
            cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{s.src_in:.3f}", "-i", s.file]
            if s.src_out:
                cmd += ["-t", f"{s.src_out - s.src_in:.3f}"]
            if s.volume > 1:  # browsers cap volume at 1: louder-than-source SFX (+6 dB clicks) are baked in
                cmd += ["-af", f"volume={s.volume:.3f}"]
            subprocess.run(cmd + ["-vn", "-c:a", "aac", "-b:a", "192k", str(out)], capture_output=True, timeout=600)
        names.append(out.name)
    return names


def _css_filter(grade: dict) -> str:
    g = grade or {}
    warm = float(g.get("warmth", 0) or 0)
    return (f"contrast({float(g.get('contrast', 1)):.2f}) saturate({float(g.get('saturation', 1)):.2f}) "
            f"brightness({1 + float(g.get('brightness', 0)):.2f})" + (f" sepia({max(0, warm) * 0.2:.2f})" if warm > 0 else ""))


def export_hyperframes(plan: EditPlan, out_dir: Path, pieces: list[dict]) -> Path:
    d = out_dir / "hyperframes"
    media = d / "media"
    media.mkdir(parents=True, exist_ok=True)
    for p in pieces:
        dst = media / p["path"].name
        if not dst.exists():
            shutil.copyfile(p["path"], dst)
    snd = _materialize_sounds(plan, media)
    cs = plan.caption_style or {}
    plan.fix_duration()
    order = {"aroll": 1, "broll": 2, "overlay": 3}
    els = []
    for p in pieces:
        c = plan.clips[p["index"]]
        cls = "clip leak" if c.track == "overlay" else "clip"
        audio = ' data-has-audio="true"' + f' data-volume="{c.volume:.2f}"' if p["audio"] else " muted"
        look = f' style="opacity:{max(0.0, min(1.0, c.volume)):.2f}"' if c.track == "overlay" else ""
        els.append(f'  <video id="{c.track}-{p["index"]:03d}" class="{cls}"{look} data-start="{c.at:.3f}" '
                   f'data-duration="{c.length:.3f}" data-track-index="{order[c.track]}"{audio} '
                   f'src="media/{p["path"].name}" playsinline></video>')
    for k, s in enumerate(plan.sounds):
        length = (s.src_out - s.src_in) if s.src_out else _sound_length(media / snd[k])
        length = min(length or 0.1, max(0.1, plan.duration - s.at))
        els.append(f'  <audio id="{s.kind}-{k:02d}" data-start="{s.at:.3f}" data-duration="{length:.3f}" '
                   f'data-track-index="{20 + k}" data-volume="{min(1.0, s.volume):.2f}" src="media/{snd[k]}"></audio>')
    for k, c in enumerate(plan.captions):
        text = escape(c.text)
        for w in c.emphasis:
            text = text.replace(escape(w), f"<b>{escape(w)}</b>", 1)
        els.append(f'  <div id="caption-{k:03d}" class="clip cap" data-start="{c.start:.3f}" '
                   f'data-duration="{c.end - c.start:.3f}" data-track-index="10">{text}</div>')
    font = cs.get("font", "Montserrat ExtraBold")
    fonts_dir = out_dir / "fonts"
    font_src = f"local('{font}')"
    if fonts_dir.is_dir():
        files = sorted(p for p in fonts_dir.iterdir() if p.suffix.lower() in (".ttf", ".otf", ".woff", ".woff2"))
        if files:
            (d / "fonts").mkdir(exist_ok=True)
            shutil.copyfile(files[0], d / "fonts" / files[0].name)
            font_src = f"url('fonts/{files[0].name}'), {font_src}"
    stroke = int(cs.get("stroke_width", 6))
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{escape(plan.name)}</title>
<style>
  @font-face {{ font-family: '{font}'; src: {font_src}; font-weight: 800; }}
  html, body {{ margin: 0; background: #000; }}
  #stage {{ position: relative; width: {plan.width}px; height: {plan.height}px; overflow: hidden; background: #000;
            filter: {_css_filter(plan.grade)}; }}
  #stage video.clip {{ position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }}
  #stage .leak {{ mix-blend-mode: screen; }}
  #stage .cap {{ position: absolute; left: 60px; right: 60px; top: {float(cs.get('position', 0.68)) * 100:.1f}%;
                 transform: translateY(-50%); text-align: center; font-family: '{font}', sans-serif;
                 font-weight: 800; font-size: {int(cs.get('size', 74))}px; line-height: 1.05; color: {cs.get('color', '#FFFFFF')};
                 -webkit-text-stroke: {stroke}px {cs.get('stroke_color', '#000000')}; paint-order: stroke fill; }}
  #stage .cap b {{ color: {cs.get('highlight_color', '#FFD400')}; }}
</style></head>
<body>
<div id="stage" data-composition-id="{escape(plan.name)}" data-no-timeline data-start="0" data-duration="{plan.duration:.3f}"
     data-width="{plan.width}" data-height="{plan.height}">
{chr(10).join(els)}
</div>
</body></html>
"""
    (d / "index.html").write_text(html, encoding="utf-8")
    (d / "README.txt").write_text("npx hyperframes preview   # live preview in the browser\n"
                                  "npx hyperframes render    # renders the MP4\n"
                                  "Everything is plain HTML: change caption CSS, add GSAP animations, etc.\n",
                                  encoding="utf-8")
    return d / "index.html"


REMOTION_EDIT = """import React from 'react';
import {AbsoluteFill, Audio, OffthreadVideo, Sequence, staticFile} from 'remotion';
import planJson from './plan.json';

const plan: any = planJson;
const f = (s: number) => Math.round(s * plan.fps);

const Caption: React.FC<{text: string; emphasis: string[]}> = ({text, emphasis}) => {
  const cs = plan.caption_style || {};
  return (
    <AbsoluteFill style={{justifyContent: 'center', alignItems: 'center'}}>
      <div style={{position: 'absolute', top: `${(cs.position ?? 0.68) * 100}%`, left: 60, right: 60,
        transform: 'translateY(-50%)', textAlign: 'center', fontFamily: `'${cs.font || 'Montserrat'}', sans-serif`,
        fontWeight: 800, fontSize: cs.size || 74, lineHeight: 1.05, color: cs.color || '#FFFFFF',
        WebkitTextStroke: `${cs.stroke_width ?? 6}px ${cs.stroke_color || '#000000'}`, paintOrder: 'stroke fill'}}>
        {text.split(' ').map((w, i) => (
          <span key={i} style={{color: emphasis.includes(w) ? (cs.highlight_color || '#FFD400') : undefined}}>
            {w}{' '}
          </span>
        ))}
      </div>
    </AbsoluteFill>
  );
};

export const Edit: React.FC = () => {
  const g = plan.grade || {};
  const filter = `contrast(${g.contrast ?? 1}) saturate(${g.saturation ?? 1}) brightness(${1 + (g.brightness ?? 0)})`;
  return (
    <AbsoluteFill style={{backgroundColor: 'black'}}>
      <AbsoluteFill style={{filter}}>
        {plan.clips.map((c: any, i: number) => (
          <Sequence key={`c${i}`} from={f(c.at)} durationInFrames={Math.max(1, f(c.src_out - c.src_in))}>
            <OffthreadVideo src={staticFile(c.media)} muted={!c.has_audio} volume={c.track === 'overlay' ? 0 : c.volume}
              style={{width: '100%', height: '100%', objectFit: 'cover',
                mixBlendMode: c.track === 'overlay' ? 'screen' : undefined,
                opacity: c.track === 'overlay' ? c.volume : 1}} />
          </Sequence>
        ))}
      </AbsoluteFill>
      {plan.sounds.map((s: any, i: number) => (
        <Sequence key={`s${i}`} from={f(s.at)} durationInFrames={Math.max(1, f(s.length))}>
          <Audio src={staticFile(s.media)} volume={s.volume} />
        </Sequence>
      ))}
      {plan.captions.map((c: any, i: number) => (
        <Sequence key={`t${i}`} from={f(c.start)} durationInFrames={Math.max(1, f(c.end - c.start))}>
          <Caption text={c.text} emphasis={c.emphasis} />
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};
"""

REMOTION_ROOT = """import React from 'react';
import {Composition} from 'remotion';
import {Edit} from './Edit';
import planJson from './plan.json';

const plan: any = planJson;

export const RemotionRoot: React.FC = () => (
  <Composition id="Reel" component={Edit} durationInFrames={Math.max(1, Math.ceil(plan.duration * plan.fps))}
    fps={plan.fps} width={plan.width} height={plan.height} />
);
"""


def export_remotion(plan: EditPlan, out_dir: Path, pieces: list[dict]) -> Path:
    d = out_dir / "remotion"
    media = d / "public" / "media"
    media.mkdir(parents=True, exist_ok=True)
    (d / "src").mkdir(parents=True, exist_ok=True)
    order = {"aroll": 0, "broll": 1, "overlay": 2}
    clips = []
    for p in sorted(pieces, key=lambda p: (order[plan.clips[p["index"]].track], plan.clips[p["index"]].at)):
        c = plan.clips[p["index"]]
        dst = media / p["path"].name
        if not dst.exists():
            shutil.copyfile(p["path"], dst)
        clips.append({"track": c.track, "at": c.at, "src_in": 0.0, "src_out": c.length, "media": f"media/{dst.name}",
                      "has_audio": p["audio"], "volume": c.volume})
    snd = _materialize_sounds(plan, media)
    sounds = [{"at": s.at, "volume": min(1.0, s.volume), "media": f"media/{snd[k]}",
               "length": min((s.src_out - s.src_in) if s.src_out else (_sound_length(media / snd[k]) or 0.1),
                             max(0.1, plan.duration - s.at))}
              for k, s in enumerate(plan.sounds)]
    plan.fix_duration()
    data = {"name": plan.name, "fps": plan.fps, "width": plan.width, "height": plan.height, "duration": plan.duration,
            "grade": plan.grade, "caption_style": plan.caption_style, "clips": clips, "sounds": sounds,
            "captions": [{"start": c.start, "end": c.end, "text": c.text, "emphasis": c.emphasis} for c in plan.captions]}
    (d / "src" / "plan.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "src" / "Edit.tsx").write_text(REMOTION_EDIT, encoding="utf-8")
    (d / "src" / "Root.tsx").write_text(REMOTION_ROOT, encoding="utf-8")
    (d / "src" / "index.ts").write_text("import {registerRoot} from 'remotion';\nimport {RemotionRoot} from './Root';\n\n"
                                        "registerRoot(RemotionRoot);\n", encoding="utf-8")
    (d / "package.json").write_text(json.dumps({
        "name": "crag-reel", "private": True,
        "scripts": {"studio": "remotion studio src/index.ts", "render": "remotion render src/index.ts Reel out/reel.mp4"},
        "dependencies": {"remotion": REMOTION_VERSION, "@remotion/cli": REMOTION_VERSION,
                         "react": "^19.0.0", "react-dom": "^19.0.0"},
        "devDependencies": {"typescript": "^5.6.0", "@types/react": "^19.0.0"}}, indent=2), encoding="utf-8")
    (d / "tsconfig.json").write_text(json.dumps({"compilerOptions": {
        "target": "ES2020", "module": "ESNext", "moduleResolution": "bundler", "jsx": "react-jsx", "strict": True,
        "resolveJsonModule": True, "esModuleInterop": True, "skipLibCheck": True, "noEmit": True},
        "include": ["src"]}, indent=2), encoding="utf-8")
    return d


def export_all(plan: EditPlan, out_dir: Path, pieces: list[dict], targets: set[str]) -> dict:
    out = {}
    if "premiere" in targets:
        out["premiere"] = str(export_premiere(plan, out_dir))
    if "aftereffects" in targets:
        out["aftereffects"] = str(export_after_effects(plan, out_dir))
    if "hyperframes" in targets:
        out["hyperframes"] = str(export_hyperframes(plan, out_dir, pieces))
    if "remotion" in targets:
        out["remotion"] = str(export_remotion(plan, out_dir, pieces))
    return out

