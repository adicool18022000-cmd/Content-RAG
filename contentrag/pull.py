"""`crag pull m12 m48 ...`: cut chosen moments out of the original footage for editing.

Writes to library_dir/exports/<name>/:
  01_<title>.mp4 ...   each moment at original resolution, with a little extra ("handles")
                       before and after so transitions have room
  selects.json         what was pulled, in order (for Claude, HyperFrames, scripts)
  timeline.xml         Final Cut Pro 7 XML: File > Import in Premiere Pro gives a sequence with
                       the clips in order, trimmed to the moment, handles still available
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape

from .config import Config
from .usage import collection_hidden, hidden_ranges, hidden_sets, record_video, safe_parts
from .util import fmt_ts, source_path

HANDLES = 0.5  # seconds kept before/after each moment


def parse_ids(values: list[str]) -> list[int]:
    return [item["id"] for item in parse_items(values)]


_ITEM = re.compile(r"^[mM]?(\d+)(?::(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?))?$")


def parse_items(values: list[str]) -> list[dict]:
    """'m12', '12', 'm12:3.5-7' (seconds 3.5-7 of that clip; for using another part of it)."""
    items = []
    for v in values:
        for part in re.split(r"[,\s]+", v.strip()):
            if not part:
                continue
            m = _ITEM.match(part)
            if not m:
                raise ValueError(f"not a moment id: {part!r} (use m12 or m12:3.5-7)")
            item = {"id": int(m.group(1))}
            if m.group(2):
                item["start"], item["end"] = float(m.group(2)), float(m.group(3))
            items.append(item)
    return items


def _encoder() -> list[str]:
    if platform.system() == "Darwin":
        return ["-c:v", "h264_videotoolbox", "-b:v", "25M", "-allow_sw", "1"]
    return ["-c:v", "libx264", "-crf", "16", "-preset", "fast"]


def _slug(text: str) -> str:
    return re.sub(r"[^\w-]+", "_", (text or "clip")).strip("_")[:40] or "clip"


def reframe_filter(mode: str | None, width: int = 1080, height: int = 1920) -> str | None:
    """9:16 for Reels. crop = fill the frame (cuts the sides); blur = whole picture on a blurred copy."""
    if mode == "crop":
        return (f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1")
    if mode == "blur":
        return (f"split[a][b];[a]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},boxblur=20:2[bg];[b]scale={width}:{height}:"
                f"force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")
    return None


def cut_clip(src: Path, start: float, end: float, out: Path, reframe: str | None = None,
             fps: int | None = None, audio: bool = True) -> tuple[bool, str]:
    vf = reframe_filter(reframe)
    if fps:
        vf = f"{vf},fps={fps}" if vf else f"fps={fps}"
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(src),
           "-t", f"{end - start:.3f}"]
    if vf:
        cmd += ["-filter_complex" if ";" in vf or "[" in vf else "-vf", vf]
    cmd += [*_encoder(), "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"] if audio else ["-an"]
    cmd += ["-movflags", "+faststart", str(out)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        return r.returncode == 0, r.stderr.strip()[-300:]
    except subprocess.TimeoutExpired:
        return False, "timed out"


def resolve_item(cfg: Config, conn, item: dict, handles: float = HANDLES) -> dict:
    """Moment row + the exact, privacy-safe range to use. Raises ValueError with a reason."""
    row = conn.execute(
        "SELECT mo.*, md.kind, md.duration AS media_duration, md.fps, md.width, md.height, md.orientation, "
        "md.title, md.taken_at, md.place, md.has_audio, md.collection FROM moments mo "
        "JOIN media md ON md.id=mo.media_id WHERE mo.id=?", (item["id"],)).fetchone()
    if row is None:
        raise ValueError("no such moment (the vault may be older than the index; rebuild it)")
    hidden = hidden_sets(conn)
    if str(row["id"]) in hidden["moment"] or row["media_id"] in hidden["media"] \
            or collection_hidden(row["collection"], hidden["collection"]):
        raise ValueError("this clip is on the hide list")
    src = source_path(cfg, conn, row["media_id"])
    if src is None:
        raise ValueError("drive with this file is not plugged in")
    duration = row["media_duration"] or row["end"]
    start = max(0.0, item.get("start", row["start"]))
    end = min(duration, item.get("end", row["end"])) if row["kind"] == "video" else 0.0
    if row["kind"] == "video":
        if end - start < 0.3:
            raise ValueError("range is empty")
        blocked = hidden_ranges(conn, row["media_id"], hidden["person"])
        if blocked:
            parts = safe_parts(start, end, blocked, min_len=0.5)
            if not parts:
                raise ValueError("a hidden person is on screen for this whole range")
            start, end = max(parts, key=lambda p: p[1] - p[0])
            # handles must not reach into hidden parts either
            room = safe_parts(max(0.0, start - handles), min(duration, end + handles), blocked, min_len=0)
            around = next(((s, e) for s, e in room if s <= start and e >= end), (start, end))
        else:
            around = (max(0.0, start - handles), min(duration, end + handles))
    else:
        around = (0.0, 0.0)
    return {"row": row, "src": src, "start": start, "end": end, "cut_start": around[0], "cut_end": around[1]}


def pull(cfg: Config, conn, items: list, name: str, handles: float = HANDLES, reframe: str | None = None,
         page: str | None = None, style: str | None = None, log=print) -> dict:
    items = [{"id": i} if isinstance(i, int) else i for i in items]
    out_dir = cfg.library_dir / "exports" / _slug(name)
    out_dir.mkdir(parents=True, exist_ok=True)
    selects, missing, uses = [], [], []
    for n, item in enumerate(items, 1):
        mid = item["id"]
        try:
            r = resolve_item(cfg, conn, item, handles)
        except ValueError as e:
            missing.append(f"m{mid}: {e}")
            continue
        row, src = r["row"], r["src"]
        base = f"{n:02d}_{_slug(row['action'] or row['title'])}"
        if row["kind"] == "photo":
            out = out_dir / f"{base}{src.suffix.lower()}"
            shutil.copyfile(src, out)
            clip_in, clip_out, length = 0.0, 0.0, 0.0
        else:
            out = out_dir / f"{base}.mp4"
            ok, err = cut_clip(src, r["cut_start"], r["cut_end"], out,
                               reframe if row["orientation"] != "vertical" or reframe == "crop" else None)
            if not ok:
                missing.append(f"m{mid}: ffmpeg failed: {err}")
                continue
            clip_in, clip_out = r["start"] - r["cut_start"], r["end"] - r["cut_start"]
            length = r["cut_end"] - r["cut_start"]
        uses.append({"media_id": row["media_id"], "start": r["start"], "end": r["end"], "moment_id": mid,
                     "role": item.get("role", "broll")})
        selects.append({
            "order": n, "moment": f"m{mid}", "file": str(out), "source": str(src),
            "source_in": round(r["start"], 3), "source_out": round(r["end"], 3),
            "clip_in": round(clip_in, 3), "clip_out": round(clip_out, 3), "clip_length": round(length, 3),
            "kind": row["kind"], "fps": row["fps"],
            "width": 1080 if reframe and row["kind"] == "video" else row["width"],
            "height": 1920 if reframe and row["kind"] == "video" else row["height"],
            "orientation": "vertical" if reframe and row["kind"] == "video" else row["orientation"],
            "has_audio": bool(row["has_audio"]),
            "description": row["description"], "speech_en": row["speech_en"],
            "taken_at": row["taken_at"], "place": row["place"], "collection": row["collection"],
        })
        log(f"[pull] {n}/{len(items)} m{mid} {fmt_ts(r['start'])}-{fmt_ts(r['end'])} -> {out.name}")
    (out_dir / "selects.json").write_text(json.dumps(selects, ensure_ascii=False, indent=2), encoding="utf-8")
    videos = [s for s in selects if s["kind"] == "video"]
    if videos:
        (out_dir / "timeline.xml").write_text(fcp7_xml(name, videos), encoding="utf-8")
    if uses:
        record_video(conn, name, page=page, style=style, uses=uses)
    for m in missing:
        log(f"[pull] skipped {m}")
    return {"folder": str(out_dir), "pulled": len(selects), "skipped": missing}


def fcp7_xml(name: str, clips: list[dict]) -> str:
    """Final Cut Pro 7 XML (xmeml v4), which Premiere Pro imports as a sequence."""
    vertical = sum(1 for c in clips if c["orientation"] == "vertical") * 2 >= len(clips)
    width, height = (1080, 1920) if vertical else (1920, 1080)
    fps = max((round(c["fps"] or 30) for c in clips), key=lambda f: sum(1 for c in clips if round(c["fps"] or 30) == f))
    ntsc = "TRUE" if any(c["fps"] and abs(c["fps"] - round(c["fps"])) > 0.01 for c in clips) else "FALSE"
    rate = f"<rate><timebase>{fps}</timebase><ntsc>{ntsc}</ntsc></rate>"
    fr = lambda seconds: int(round(seconds * fps))  # noqa: E731

    vitems, aitems, t = [], [], 0
    for i, c in enumerate(clips, 1):
        total = fr(c["clip_length"])
        cin, cout = fr(c["clip_in"]), fr(c["clip_out"])
        dur = cout - cin
        fname = escape(Path(c["file"]).name)
        file_xml = (f'<file id="file{i}"><name>{fname}</name><pathurl>{escape(Path(c["file"]).as_uri())}</pathurl>'
                    f"{rate}<duration>{total}</duration><media><video><samplecharacteristics>"
                    f"<width>{c['width'] or width}</width><height>{c['height'] or height}</height>"
                    "</samplecharacteristics></video><audio><channelcount>2</channelcount></audio></media></file>")
        common = (f"<name>{fname}</name><enabled>TRUE</enabled><duration>{total}</duration>{rate}"
                  f"<start>{t}</start><end>{t + dur}</end><in>{cin}</in><out>{cout}</out>")
        vitems.append(f'<clipitem id="v{i}">{common}{file_xml}</clipitem>')
        if c["has_audio"]:
            aitems.append(f'<clipitem id="a{i}">{common}<file id="file{i}"/><sourcetrack><mediatype>audio'
                          "</mediatype><trackindex>1</trackindex></sourcetrack></clipitem>")
        t += dur
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4">'
        f'<sequence id="sequence-1"><name>{escape(name)}</name><duration>{t}</duration>{rate}'
        f"<media><video><format><samplecharacteristics>{rate}<width>{width}</width><height>{height}</height>"
        f"</samplecharacteristics></format><track>{''.join(vitems)}</track></video>"
        f"<audio><track>{''.join(aitems)}</track></audio></media></sequence></xmeml>\n"
    )
