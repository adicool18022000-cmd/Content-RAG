"""Step 2: sample frames, detect scene cuts, extract audio; thumbnail photos."""

from __future__ import annotations

import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import Config
from .db import MAX_ATTEMPTS
from .util import frame_times, source_path

# Stop preparing when the library drive has less free space than this.
MIN_FREE_BYTES = 2 * 1024**3

FRAME_LONG_SIDE = 640
_SCALE = f"scale='if(gt(iw,ih),{FRAME_LONG_SIDE},-2)':'if(gt(iw,ih),-2,{FRAME_LONG_SIDE})'"
_PTS = re.compile(r"pts_time:([0-9.]+)")


def media_dir(cfg: Config, media_id: str) -> Path:
    return cfg.frames_dir / media_id[:2] / media_id


def _run(cmd: list[str], timeout: float = 120) -> subprocess.CompletedProcess:
    """Run a tool; a damaged file can make ffmpeg hang, so everything has a time limit."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, -1, "", f"timed out after {int(timeout)} s")


def extract_frame(src: Path, t: float, out: Path) -> bool:
    r = _run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(src),
              "-frames:v", "1", "-vf", _SCALE, "-q:v", "4", str(out)], timeout=90)
    return r.returncode == 0 and out.exists() and out.stat().st_size > 0


def detect_cuts(src: Path, duration: float, threshold: float = 0.3) -> list[float]:
    # Cuts are only hints for the AI: on timeout or error we simply have none.
    r = _run(["ffmpeg", "-nostdin", "-v", "info", "-hwaccel", "auto", "-i", str(src), "-an", "-sn",
              "-vf", f"scale=160:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
             timeout=max(300, duration * 1.5))
    if r.returncode != 0:
        return []
    return sorted({round(float(m), 2) for m in _PTS.findall(r.stderr)})


def extract_audio(src: Path, out: Path) -> bool:
    from .probe import audio_stream

    out.parent.mkdir(parents=True, exist_ok=True)
    a = audio_stream(src)
    if a is None:
        return False
    r = _run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src), "-map", f"0:{a}", "-vn", "-ac", "1",
              "-ar", "16000", "-c:a", "pcm_s16le", str(out)], timeout=600)
    return r.returncode == 0 and out.exists()


def prep_video(cfg: Config, row, src: Path) -> dict:
    out_dir = media_dir(cfg, row["id"])
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for window in frame_times(row["duration"] or 0, cfg.min_interval, cfg.window_seconds, cfg.frames_per_window):
        for t in window:
            t = min(t, max(0.0, (row["duration"] or 0) - 0.05))
            out = out_dir / f"t{t:09.2f}.jpg"
            if out.exists() or extract_frame(src, t, out):
                frames.append((t, out.relative_to(cfg.library_dir).as_posix()))
    if not frames:  # some files only decode from the start
        out = out_dir / "t00000.00.jpg"
        if extract_frame(src, 0.0, out):
            frames.append((0.0, out.relative_to(cfg.library_dir).as_posix()))
    cuts = detect_cuts(src, row["duration"] or 0) if cfg.scene_detect else []
    audio = None
    if row["has_audio"] and not row["transcribed"] and cfg.transcribe_enabled:
        wav = cfg.audio_dir / f"{row['id']}.wav"
        if wav.exists() or extract_audio(src, wav):
            audio = wav
    return {"frames": frames, "cuts": cuts, "audio": audio}


def prep_photo(cfg: Config, row, src: Path) -> dict:
    from PIL import Image, ImageOps

    out_dir = media_dir(cfg, row["id"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "photo.jpg"
    if not out.exists():
        try:
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((FRAME_LONG_SIDE, FRAME_LONG_SIDE))
                im.save(out, "JPEG", quality=85)
        except Exception:
            # HEIC without pillow-heif, RAW, ...: let macOS `sips` or ffmpeg try
            if shutil.which("sips"):
                _run(["sips", "-s", "format", "jpeg", "-Z", str(FRAME_LONG_SIDE), str(src), "--out", str(out)], 120)
            else:
                _run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src), "-vf", _SCALE, str(out)], 120)
    if not out.exists():
        raise RuntimeError("could not decode photo")
    return {"frames": [(0.0, out.relative_to(cfg.library_dir).as_posix())], "cuts": [], "audio": None}


def disk_full(cfg: Config) -> bool:
    cfg.library_dir.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(cfg.library_dir).free < MIN_FREE_BYTES


def prep(cfg: Config, conn, limit: int | None = None, log=print, stop=None) -> dict:
    rows = conn.execute(
        "SELECT * FROM media WHERE prepped=0 AND coalesce(attempts,0) < ? ORDER BY taken_at", (MAX_ATTEMPTS,)
    ).fetchall()
    if limit:
        rows = rows[:limit]
    stats = {"done": 0, "missing": 0, "failed": 0}
    if rows and disk_full(cfg):
        log(f"[prep] less than {MIN_FREE_BYTES // 1024**3} GB free on {cfg.library_dir}; free some space first")
        stats["disk_full"] = True
        return stats
    jobs = {}
    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        for row in rows:
            src = source_path(cfg, conn, row["id"])
            if src is None:
                stats["missing"] += 1  # drive not plugged in: stays pending, no attempt used
                continue
            fn = prep_video if row["kind"] == "video" else prep_photo
            jobs[pool.submit(fn, cfg, row, src)] = (row, src)
        if stats["missing"]:
            log(f"[prep] {stats['missing']} files are on drives that aren't plugged in; they wait for next time")
        for i, fut in enumerate(as_completed(jobs), 1):
            row, src = jobs[fut]
            try:
                res = fut.result()
                if not res["frames"]:
                    raise RuntimeError("no frames could be extracted (damaged or unsupported file?)")
            except Exception as e:
                if not src.exists():  # drive unplugged mid-run: not the file's fault
                    stats["missing"] += 1
                    continue
                stats["failed"] += 1
                conn.execute("UPDATE media SET error=?, attempts=coalesce(attempts,0)+1 WHERE id=?",
                             (f"prep: {e}", row["id"]))
                log(f"[prep] failed {row['relpath']}: {e}")
                continue
            conn.execute("DELETE FROM frames WHERE media_id=?", (row["id"],))
            conn.executemany("INSERT INTO frames VALUES (?,?,?)", [(row["id"], t, p) for t, p in res["frames"]])
            conn.execute("DELETE FROM cuts WHERE media_id=?", (row["id"],))
            conn.executemany("INSERT INTO cuts VALUES (?,?)", [(row["id"], t) for t in res["cuts"]])
            no_speech = row["kind"] == "photo" or not row["has_audio"]
            conn.execute(
                "UPDATE media SET prepped=1, error=NULL, transcribed=CASE WHEN ? THEN 1 ELSE transcribed END WHERE id=?",
                (int(no_speech), row["id"]),
            )
            stats["done"] += 1
            if i % 25 == 0:
                conn.commit()
                log(f"[prep] {i}/{len(jobs)}")
                if disk_full(cfg) or (stop is not None and stop.is_set()):
                    log("[prep] stopping early (disk nearly full or stop requested)")
                    stats["disk_full"] = disk_full(cfg)
                    for f in jobs:
                        f.cancel()
                    break
    conn.commit()
    return stats
