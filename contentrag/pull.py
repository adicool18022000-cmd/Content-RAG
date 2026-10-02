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
from .util import fmt_ts, source_path

HANDLES = 0.5  # seconds kept before/after each moment


def parse_ids(values: list[str]) -> list[int]:
    ids = []
    for v in values:
        for part in re.split(r"[,\s]+", v.strip()):
            if part:
                ids.append(int(part.lstrip("mM")))
    return ids


def _encoder() -> list[str]:
    if platform.system() == "Darwin":
        return ["-c:v", "h264_videotoolbox", "-b:v", "25M", "-allow_sw", "1"]
    return ["-c:v", "libx264", "-crf", "16", "-preset", "fast"]


def _slug(text: str) -> str:
    return re.sub(r"[^\w-]+", "_", (text or "clip")).strip("_")[:40] or "clip"


def pull(cfg: Config, conn, ids: list[int], name: str, handles: float = HANDLES, log=print) -> dict:
    out_dir = cfg.library_dir / "exports" / _slug(name)
    out_dir.mkdir(parents=True, exist_ok=True)
    selects, missing = [], []
    for n, mid in enumerate(ids, 1):
        row = conn.execute(
            "SELECT mo.*, md.kind, md.duration AS media_duration, md.fps, md.width, md.height, md.orientation, "
            "md.title, md.taken_at, md.place, md.has_audio FROM moments mo JOIN media md ON md.id=mo.media_id "
            "WHERE mo.id=?", (mid,)).fetchone()
        if row is None:
            missing.append(f"m{mid}: no such moment (the vault may be older than the index; rebuild it)")
            continue
        src = source_path(cfg, conn, row["media_id"])
        if src is None:
            missing.append(f"m{mid}: drive with this file is not plugged in")
            continue
        base = f"{n:02d}_{_slug(row['action'] or row['title'])}"
        if row["kind"] == "photo":
            out = out_dir / f"{base}{src.suffix.lower()}"
            shutil.copyfile(src, out)
            clip_in, clip_out, length = 0.0, 0.0, 0.0
        else:
            start = max(0.0, row["start"] - handles)
            end = min(row["media_duration"] or row["end"], row["end"] + handles)
            out = out_dir / f"{base}.mp4"
            cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(src),
                   "-t", f"{end - start:.3f}", *_encoder(), "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                ok = r.returncode == 0
                err = r.stderr.strip()[-200:]
            except subprocess.TimeoutExpired:
                ok, err = False, "timed out"
            if not ok:
                missing.append(f"m{mid}: ffmpeg failed: {err}")
                continue
            clip_in, clip_out, length = row["start"] - start, row["end"] - start, end - start
        selects.append({
            "order": n, "moment": f"m{mid}", "file": str(out), "source": str(src),
            "source_in": round(row["start"], 3), "source_out": round(row["end"], 3),
            "clip_in": round(clip_in, 3), "clip_out": round(clip_out, 3), "clip_length": round(length, 3),
            "kind": row["kind"], "fps": row["fps"], "width": row["width"], "height": row["height"],
            "orientation": row["orientation"], "has_audio": bool(row["has_audio"]),
            "description": row["description"], "speech_en": row["speech_en"],
            "taken_at": row["taken_at"], "place": row["place"],
        })
        log(f"[pull] {n}/{len(ids)} m{mid} {fmt_ts(row['start'])}-{fmt_ts(row['end'])} -> {out.name}")
    (out_dir / "selects.json").write_text(json.dumps(selects, ensure_ascii=False, indent=2), encoding="utf-8")
    videos = [s for s in selects if s["kind"] == "video"]
    if videos:
        (out_dir / "timeline.xml").write_text(fcp7_xml(name, videos), encoding="utf-8")
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
