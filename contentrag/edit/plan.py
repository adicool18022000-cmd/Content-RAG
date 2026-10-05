"""The edit plan: a plain JSON timeline every exporter reads, and that Claude can read and adjust.

Tracks (bottom to top): "aroll" (the talking head), "broll" (footage from the library, covers the
A-roll picture while its voice keeps playing), "overlay" (light/colour leaks, blended on top).
Audio: A-roll voice, optional music bed, sound effects (risers, shutter clicks, whooshes).
All times are seconds. `at` = position on the output timeline; `src_in`/`src_out` = range in the source.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Clip:
    track: str                      # aroll | broll | overlay
    file: str
    src_in: float
    src_out: float
    at: float
    media_id: str | None = None
    moment_id: int | None = None
    role: str | None = None         # hook | story | broll | beat | leak ...
    audio: bool = False             # keep this clip's own sound (A-roll always does)
    volume: float = 1.0
    reframe: str | None = None      # crop | blur (for horizontal footage in a 9:16 reel)
    blend: str | None = None        # screen | add (overlays such as light leaks)
    note: str = ""                  # why this clip is here (the script line, the beat...)
    blur: list = field(default_factory=list)  # hidden people's faces to blur: [t (source s), x, y, w, h]

    @property
    def length(self) -> float:
        return self.src_out - self.src_in

    @property
    def end(self) -> float:
        return self.at + self.length


@dataclass
class Caption:
    start: float
    end: float
    text: str
    emphasis: list[str] = field(default_factory=list)  # words to highlight


@dataclass
class Sound:
    at: float
    file: str
    volume: float = 1.0
    kind: str = "sfx"               # sfx | music
    src_in: float = 0.0
    src_out: float | None = None


@dataclass
class EditPlan:
    name: str
    width: int = 1080
    height: int = 1920
    fps: int = 30
    duration: float = 0.0
    style: str | None = None
    page: str | None = None
    clips: list[Clip] = field(default_factory=list)
    captions: list[Caption] = field(default_factory=list)
    sounds: list[Sound] = field(default_factory=list)
    grade: dict = field(default_factory=dict)       # {"lut": path, "contrast": 1.05, "saturation": 1.1, ...}
    caption_style: dict = field(default_factory=dict)
    shoot_list: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def track(self, name: str) -> list[Clip]:
        return sorted((c for c in self.clips if c.track == name), key=lambda c: c.at)

    def fix_duration(self) -> float:
        ends = [c.end for c in self.clips] + [c.end for c in self.captions]
        self.duration = round(max(ends, default=0.0), 3)
        return self.duration

    def uses(self) -> list[dict]:
        """Library footage used, for usage tracking."""
        return [{"media_id": c.media_id, "start": c.src_in, "end": c.src_out, "moment_id": c.moment_id,
                 "role": c.role} for c in self.clips if c.media_id]

    def save(self, path: Path) -> Path:
        self.fix_duration()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "EditPlan":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        d["clips"] = [Clip(**c) for c in d.get("clips", [])]
        d["captions"] = [Caption(**c) for c in d.get("captions", [])]
        d["sounds"] = [Sound(**s) for s in d.get("sounds", [])]
        return cls(**d)
