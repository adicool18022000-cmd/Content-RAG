"""Song analysis for beat-synced edits (local, librosa).

`crag music analyse song.mp3` stores, in library/music/<name>.json and a vault note:
  - tempo (BPM), every beat time, bar starts (downbeats, assuming 4/4)
  - energy per beat (0-1) and sections: calm / build / peak, each with a cutting pace
  - drops: the beats where energy jumps (where a riser should end and the fastest cuts start)
`crag music edit <name> ...` then builds an edit plan that cuts on those beats (see edit/beat.py).
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .config import Config

# cutting pace per section: cut every N beats
PACE_BEATS = {"peak": 1, "build": 2, "calm": 4}
MIN_SECTION_BEATS = 8


@dataclass
class Section:
    start: float
    end: float
    kind: str           # calm | build | peak
    energy: float
    cut_every_beats: int


@dataclass
class SongInfo:
    name: str
    file: str
    duration: float
    bpm: float
    beats: list[float]
    downbeats: list[float]
    energy: list[float]                       # per beat, 0-1
    sections: list[Section] = field(default_factory=list)
    drops: list[float] = field(default_factory=list)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "SongInfo":
        d = json.loads(path.read_text(encoding="utf-8"))
        d["sections"] = [Section(**s) for s in d.get("sections", [])]
        return cls(**d)


def _slug(name: str) -> str:
    return re.sub(r"[^\w-]+", "-", name.lower()).strip("-")[:60] or "song"


def music_dir(cfg: Config) -> Path:
    return cfg.library_dir / "music"


def analyse(cfg: Config, path: Path, name: str | None = None, log=print) -> SongInfo:
    try:
        import librosa
    except ImportError:
        raise SystemExit("librosa is not installed. Run: uv pip install librosa soundfile")
    name = _slug(name or path.stem)
    y, sr = librosa.load(str(path), sr=22050, mono=True)
    duration = float(len(y) / sr)
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames", trim=False)
    beats = librosa.frames_to_time(beat_frames, sr=sr)
    if len(beats) >= 4:
        period = float(np.polyfit(np.arange(len(beats)), beats, 1)[0])  # precise despite frame rounding
        bpm = 60.0 / period
    else:  # no clear beat (speech, ambient): fall back to a steady grid
        bpm = float(np.atleast_1d(tempo)[0]) or 100.0
        period = 60.0 / bpm
        beats = np.arange(0, duration, period)
    # quiet intros/outros often get no detected beats: continue the grid to both ends
    head = np.arange(beats[0] - period, -1e-6, -period)[::-1]
    tail = np.arange(beats[-1] + period, duration - 0.05, period)
    beats = np.concatenate([head, beats, tail])
    beat_frames = librosa.time_to_frames(beats, sr=sr)
    onset = librosa.onset.onset_strength(y=y, sr=sr)
    rms = librosa.feature.rms(y=y)[0]
    times = librosa.frames_to_time(np.arange(len(rms)), sr=sr)

    # energy per beat: loudness + onset density, normalised to 0-1 across the song
    edges = list(beats) + [duration]
    raw = []
    for a, b in zip(edges[:-1], edges[1:]):
        sel = (times >= a) & (times < b)
        loud = float(rms[sel].mean()) if sel.any() else 0.0
        on = float(onset[:len(sel)][sel[:len(onset)]].mean()) if sel[:len(onset)].any() else 0.0
        raw.append(loud * (1 + 0.5 * on))
    raw = np.array(raw)
    smooth = np.convolve(raw, np.ones(4) / 4, mode="same") if len(raw) >= 4 else raw
    lo, hi = np.percentile(smooth, 5), np.percentile(smooth, 95)
    energy = np.clip((smooth - lo) / ((hi - lo) or 1.0), 0, 1)

    # downbeats: the beat phase (0-3) with the strongest onsets
    on_at_beat = onset[np.clip(beat_frames, 0, len(onset) - 1)] if len(beat_frames) else np.zeros(len(beats))
    phase = int(np.argmax([on_at_beat[k::4].sum() for k in range(4)])) if len(on_at_beat) >= 4 else 0
    downbeats = [float(t) for t in beats[phase::4]]

    sections = _sections(beats, energy, duration)
    drops = [float(beats[i]) for i in range(4, len(energy))
             if energy[i] - energy[max(0, i - 4):i].mean() > 0.35 and energy[i] > 0.6]
    drops = [d for k, d in enumerate(drops) if k == 0 or d - drops[k - 1] > 8]
    info = SongInfo(name=name, file=str(path), duration=round(duration, 3), bpm=round(bpm, 1),
                    beats=[round(float(b), 3) for b in beats], downbeats=[round(d, 3) for d in downbeats],
                    energy=[round(float(e), 3) for e in energy], sections=sections, drops=[round(d, 3) for d in drops])
    out = music_dir(cfg)
    out.mkdir(parents=True, exist_ok=True)
    keep = out / f"{name}{path.suffix.lower()}"
    if path.resolve() != keep.resolve():
        shutil.copyfile(path, keep)
    info.file = str(keep)
    info.save(out / f"{name}.json")
    _note(cfg, info)
    log(f"[music] {name}: {info.bpm} BPM, {len(info.beats)} beats, "
        + ", ".join(f"{s.kind} {s.start:.0f}-{s.end:.0f}s" for s in sections))
    return info


def _sections(beats, energy, duration) -> list[Section]:
    kinds = ["calm" if e < 0.4 else "build" if e < 0.7 else "peak" for e in energy]
    # merge into runs, then absorb runs shorter than MIN_SECTION_BEATS into the previous one
    runs: list[list] = []
    for i, k in enumerate(kinds):
        if runs and runs[-1][0] == k:
            runs[-1][2] = i
        else:
            runs.append([k, i, i])
    merged: list[list] = []
    for r in runs:
        if merged and (r[2] - r[1] + 1) < MIN_SECTION_BEATS:
            merged[-1][2] = r[2]
        elif merged and merged[-1][0] == r[0]:
            merged[-1][2] = r[2]
        else:
            merged.append(r)
    out = []
    for k, (kind, a, b) in enumerate(merged):
        start = float(beats[a]) if k else 0.0
        end = float(beats[b + 1]) if b + 1 < len(beats) else duration
        e = float(np.mean(energy[a:b + 1]))
        kind = "calm" if e < 0.4 else "build" if e < 0.7 else "peak"
        out.append(Section(round(start, 3), round(end, 3), kind, round(e, 3), PACE_BEATS[kind]))
    return out


def _note(cfg: Config, info: SongInfo) -> None:
    d = cfg.vault_dir / "Music"
    d.mkdir(parents=True, exist_ok=True)
    lines = ["---", "type: song", f"bpm: {info.bpm}", f"duration: {info.duration}", "---", f"# {info.name}", "",
             f"*{info.bpm} BPM, {info.duration:.0f} s. Build an edit: `crag music edit {info.name} --collection ...`*",
             "", "## Sections"]
    lines += [f"- {s.start:.1f}–{s.end:.1f} s · **{s.kind}** · energy {s.energy:.2f} · cut every "
              f"{s.cut_every_beats} beat(s) ≈ {60 / info.bpm * s.cut_every_beats:.2f} s per shot" for s in info.sections]
    if info.drops:
        lines += ["", "## Drops (put a riser before, fastest cuts after)", *[f"- {t:.2f} s" for t in info.drops]]
    (d / f"{info.name}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def load(cfg: Config, name: str) -> SongInfo:
    path = music_dir(cfg) / f"{_slug(name)}.json"
    if not path.exists():
        raise SystemExit(f"Song '{name}' not analysed yet: crag music analyse <file> --name {name}")
    return SongInfo.load(path)


def songs(cfg: Config) -> list[dict]:
    return [{"name": p.stem, **{k: v for k, v in json.loads(p.read_text()).items()
                                 if k in ("bpm", "duration", "file")}}
            for p in sorted(music_dir(cfg).glob("*.json"))]
