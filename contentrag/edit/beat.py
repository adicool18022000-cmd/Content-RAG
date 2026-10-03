"""Beat-synced montage: cut library footage on the beats of an analysed song.

Pace follows the music: calm sections cut every 4 beats with calm/cinematic footage, builds every 2,
peaks/drops every beat with high-motion footage. The first shot is the strongest hook. Long moments
can feed several cuts with *different parts* of the same clip; usage across earlier videos and
hidden people/clips are respected (via search).
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..music import SongInfo, load
from ..search import Filters, search
from .plan import Clip, EditPlan, Sound

MIN_SHOT = 0.35
MOTION_WANT = {"calm": (1, 3), "build": (2, 4), "peak": (4, 5)}


def slots(song: SongInfo, start: float, end: float) -> list[tuple[float, float, str]]:
    """(t0, t1, section kind) for every cut, following each section's pace."""
    out = []
    beats = [b for b in song.beats if start - 1e-6 <= b < end] or [start]
    if beats[0] > start + 0.05:
        beats = [start] + beats
    i = 0
    while i < len(beats):
        t0 = beats[i]
        sec = next((s for s in song.sections if s.start <= t0 < s.end), song.sections[-1] if song.sections else None)
        kind, step = (sec.kind, sec.cut_every_beats) if sec else ("build", 2)
        j = min(i + step, len(beats))
        t1 = beats[j] if j < len(beats) else end
        if t1 - t0 >= MIN_SHOT:
            out.append((round(t0, 3), round(min(t1, end), 3), kind))
        i = j
    return out


def plan_beat_edit(cfg: Config, conn, song_name: str, name: str, query: str = "", filters: Filters | None = None,
                   start: float = 0.0, end: float | None = None, reframe: str = "crop",
                   use_vectors: bool = True) -> EditPlan:
    song = load(cfg, song_name)
    end = min(end or song.duration, song.duration)
    pool = [r for r in search(cfg, conn, query, filters or Filters(kind="video"), limit=400, use_vectors=use_vectors)
            if r["kind"] == "video" and r["end"] - r["start"] >= MIN_SHOT]
    if not pool:
        raise SystemExit("No footage matches these filters; loosen them (collection/roles/query).")
    cursor: dict[int, float] = {}       # moment -> next unused second (different parts of one clip)
    media_uses: dict[str, int] = {}
    plan = EditPlan(name=name, page=None)
    plan.sounds.append(Sound(at=0.0, file=song.file, kind="music", src_in=start, src_out=end))
    shoot = []
    for k, (t0, t1, kind) in enumerate(slots(song, start, end)):
        need = t1 - t0
        lo, hi = MOTION_WANT.get(kind, (1, 5))

        def score(r):
            left = r["end"] - cursor.get(r["moment_id"], r["start"])
            if left < need:
                return None
            motion = r.get("motion") or 3
            miss = 0 if lo <= motion <= hi else min(abs(motion - lo), abs(motion - hi))
            s = -2.0 * miss - 1.5 * media_uses.get(r["media_id"], 0) - 0.7 * r["uses"]
            s += 0.4 * (r["broll_score"] or 0)
            if k == 0:
                s += 1.5 * (r["hook_score"] or 0)  # open on the strongest hook
            elif kind == "calm" and set(r["roles"]) & {"cinematic", "calm", "establishing"}:
                s += 1.0
            elif kind == "peak" and set(r["roles"]) & {"action", "spectacle", "hook", "funny"}:
                s += 1.0
            return s

        scored = [(score(r), r) for r in pool]
        scored = [(s, r) for s, r in scored if s is not None]
        if not scored:
            shoot.append(f"{t0:.1f}-{t1:.1f}s ({kind}): no unused footage long enough")
            continue
        _, best = max(scored, key=lambda x: x[0])
        src_in = cursor.get(best["moment_id"], best["start"])
        cursor[best["moment_id"]] = src_in + need
        media_uses[best["media_id"]] = media_uses.get(best["media_id"], 0) + 1
        plan.clips.append(Clip(track="broll", file=best["file"], src_in=round(src_in, 3),
                               src_out=round(src_in + need, 3), at=round(t0 - start, 3), media_id=best["media_id"],
                               moment_id=best["moment_id"], role="hook" if k == 0 else "beat",
                               reframe=None if best["orientation"] == "vertical" else reframe,
                               note=f"{kind} section, motion {best.get('motion')}: {best['description']}"))
    plan.shoot_list = shoot
    plan.notes.append(f"Song {song.name}: {song.bpm} BPM, {start:.1f}-{end:.1f}s; "
                      + ", ".join(f"{s.kind} {s.start:.0f}-{s.end:.0f}s" for s in song.sections))
    plan.fix_duration()
    return plan


def export_dir(cfg: Config, name: str) -> Path:
    from ..pull import _slug

    return cfg.library_dir / "exports" / _slug(name)
