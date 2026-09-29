"""Small helpers shared by the pipeline steps."""

from __future__ import annotations

from pathlib import Path

from .config import Config


def source_path(cfg: Config, conn, media_id: str) -> Path | None:
    """First existing copy of a media item across all mounted roots."""
    for loc in conn.execute("SELECT root, relpath FROM locations WHERE media_id=?", (media_id,)):
        p = cfg.resolve(loc["root"], loc["relpath"])
        if p is not None and p.exists():
            return p
    return None


def fmt_ts(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def frame_times(duration: float, min_interval: float, window_seconds: float, per_window: int) -> list[list[float]]:
    """Sample times grouped per window: evenly spaced, at most `per_window` per window."""
    if duration <= 0:
        return [[0.0]]
    windows: list[list[float]] = []
    start = 0.0
    while start < duration - 1e-6:
        end = min(duration, start + window_seconds)
        span = end - start
        n = max(1, min(per_window, int(span // min_interval)))
        windows.append([round(start + (i + 0.5) * span / n, 2) for i in range(n)])
        start = end
    return windows
