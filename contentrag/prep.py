"""Step 2: sample frames, detect scene cuts, extract audio; thumbnail photos."""

from __future__ import annotations

import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import Config
from .util import frame_times, source_path

FRAME_LONG_SIDE = 640
_SCALE = f"scale='if(gt(iw,ih),{FRAME_LONG_SIDE},-2)':'if(gt(iw,ih),-2,{FRAME_LONG_SIDE})'"
_PTS = re.compile(r"pts_time:([0-9.]+)")


def media_dir(cfg: Config, media_id: str) -> Path:
    return cfg.frames_dir / media_id[:2] / media_id


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def extract_frame(src: Path, t: float, out: Path) -> bool:
    r = _run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(src),
              "-frames:v", "1", "-vf", _SCALE, "-q:v", "4", str(out)])
    return r.returncode == 0 and out.exists() and out.stat().st_size > 0


def detect_cuts(src: Path, threshold: float = 0.3) -> list[float]:
    r = _run(["ffmpeg", "-nostdin", "-v", "info", "-hwaccel", "auto", "-i", str(src), "-an", "-sn",
              "-vf", f"scale=160:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"])
    return sorted({round(float(m), 2) for m in _PTS.findall(r.stderr)})


def extract_audio(src: Path, out: Path) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    r = _run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
              "-c:a", "pcm_s16le", str(out)])
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
    cuts = detect_cuts(src) if cfg.scene_detect else []
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
                _run(["sips", "-s", "format", "jpeg", "-Z", str(FRAME_LONG_SIDE), str(src), "--out", str(out)])
            else:
                _run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src), "-vf", _SCALE, str(out)])
    if not out.exists():
        raise RuntimeError("could not decode photo")
    return {"frames": [(0.0, out.relative_to(cfg.library_dir).as_posix())], "cuts": [], "audio": None}


def prep(cfg: Config, conn, limit: int | None = None, log=print) -> dict:
    rows = conn.execute("SELECT * FROM media WHERE prepped=0 ORDER BY taken_at").fetchall()
    if limit:
        rows = rows[:limit]
    stats = {"done": 0, "missing": 0, "failed": 0}
    jobs = {}
    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        for row in rows:
            src = source_path(cfg, conn, row["id"])
            if src is None:
                stats["missing"] += 1
                continue
            fn = prep_video if row["kind"] == "video" else prep_photo
            jobs[pool.submit(fn, cfg, row, src)] = row
        for i, fut in enumerate(as_completed(jobs), 1):
            row = jobs[fut]
            try:
                res = fut.result()
                if not res["frames"]:
                    raise RuntimeError("no frames could be extracted")
            except Exception as e:
                stats["failed"] += 1
                conn.execute("UPDATE media SET error=? WHERE id=?", (f"prep: {e}", row["id"]))
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
    conn.commit()
    return stats
