"""Load contentrag.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = "contentrag.toml"
BACKENDS = {
    "gemini": "Gemini API (GEMINI_API_KEY, pay per use)",
    "claude-code": "Claude subscription via Claude Code (Pro/Max plan, no API key)",
    "claude": "Claude API (ANTHROPIC_API_KEY, pay per use, Batches API)",
}


@dataclass
class Root:
    name: str
    path: Path
    kind: str = "archive"  # "archive" or "brand"
    skip: list[str] = field(default_factory=list)  # sub-folders (relative to path) never indexed

    @property
    def mounted(self) -> bool:
        return self.path.is_dir()


@dataclass
class Config:
    library_dir: Path
    roots: list[Root]
    timezone: str = "Asia/Kolkata"
    whisper_model: str = "mlx-community/whisper-large-v3-turbo"
    language: str = "hi"
    transcribe_enabled: bool = True
    backend: str = "gemini"  # gemini | claude (API key) | claude-code (Claude subscription)
    gemini_model: str = "gemini-3.5-flash"
    gemini_photo_model: str = ""  # empty = same as gemini_model
    gemini_api_key_env: str = "GEMINI_API_KEY"
    gemini_resolution: str = "low"  # low | medium | high
    gemini_thinking: str = "low"  # minimal | low | medium | high
    gemini_fps: float = 1.0
    gemini_workers: int = 4
    gemini_budget_usd: float = 0.0  # 0 = no cap
    cc_model: str = "opus"
    cc_retry_model: str = "sonnet"
    cc_effort: str = "low"
    cc_workers: int = 2
    model: str = "claude-opus-5-5"
    effort: str = "low"
    retry_model: str = "claude-sonnet-5-5"
    min_interval: float = 2.0
    window_seconds: float = 360.0
    frames_per_window: int = 36
    scene_detect: bool = True
    workers: int = 4
    faces_enabled: bool = True
    embed_model: str = "BAAI/bge-m3"
    source: Path | None = field(default=None, repr=False)

    @property
    def photo_model(self) -> str:
        return self.gemini_photo_model or self.gemini_model

    @property
    def db_path(self) -> Path:
        return self.library_dir / "index.sqlite"

    @property
    def frames_dir(self) -> Path:
        return self.library_dir / "frames"

    @property
    def audio_dir(self) -> Path:
        return self.library_dir / "audio"

    @property
    def vault_dir(self) -> Path:
        return self.library_dir / "LifeVault"

    @property
    def vectors_path(self) -> Path:
        return self.library_dir / "vectors.npz"

    def root(self, name: str) -> Root | None:
        return next((r for r in self.roots if r.name == name), None)

    def resolve(self, root_name: str, relpath: str) -> Path | None:
        root = self.root(root_name)
        if root is None:
            return None
        return root.path / relpath


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("CONTENTRAG_CONFIG", DEFAULT_CONFIG)).expanduser()
    if not path.exists():
        raise SystemExit(
            f"Config not found: {path}\n"
            "Copy contentrag.example.toml to contentrag.toml and edit the paths."
        )
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    roots = [
        Root(name=r["name"], path=Path(r["path"]).expanduser(), kind=r.get("kind", "archive"),
             skip=[str(x).strip("/") for x in r.get("skip", [])])
        for r in raw.get("roots", [])
    ]
    names = [r.name for r in roots]
    if len(names) != len(set(names)):
        raise SystemExit("Each [[roots]] entry needs a unique name.")
    for r in roots:
        if r.kind not in ("archive", "brand"):
            raise SystemExit(f"Root {r.name}: kind must be 'archive' or 'brand'.")

    t = raw.get("transcribe", {})
    d = raw.get("describe", {})
    p = raw.get("prep", {})
    e = raw.get("embed", {})
    g = raw.get("gemini", {})
    cc = raw.get("claude_code", {})
    backend = os.environ.get("CRAG_BACKEND") or d.get("backend", Config.backend)
    if backend not in BACKENDS:
        raise SystemExit(f"[describe] backend must be one of: {', '.join(BACKENDS)}.")
    return Config(
        library_dir=Path(raw["library_dir"]).expanduser(),
        roots=roots,
        timezone=raw.get("timezone", "Asia/Kolkata"),
        whisper_model=t.get("model", Config.whisper_model),
        language=t.get("language", Config.language),
        transcribe_enabled=bool(t.get("enabled", Config.transcribe_enabled)),
        backend=backend,
        gemini_model=g.get("model", Config.gemini_model),
        gemini_photo_model=g.get("photo_model", Config.gemini_photo_model),
        gemini_api_key_env=g.get("api_key_env", Config.gemini_api_key_env),
        gemini_resolution=g.get("media_resolution", Config.gemini_resolution),
        gemini_thinking=g.get("thinking_level", Config.gemini_thinking),
        gemini_fps=float(g.get("fps", Config.gemini_fps)),
        gemini_workers=int(g.get("workers", Config.gemini_workers)),
        gemini_budget_usd=float(g.get("budget_usd", Config.gemini_budget_usd)),
        cc_model=cc.get("model", Config.cc_model),
        cc_retry_model=cc.get("retry_model", Config.cc_retry_model),
        cc_effort=cc.get("effort", Config.cc_effort),
        cc_workers=max(1, int(cc.get("workers", Config.cc_workers))),
        model=d.get("model", Config.model),
        effort=d.get("effort", Config.effort),
        retry_model=d.get("retry_model", Config.retry_model),
        min_interval=float(p.get("min_interval", Config.min_interval)),
        window_seconds=float(p.get("window_seconds", Config.window_seconds)),
        frames_per_window=int(p.get("frames_per_window", Config.frames_per_window)),
        scene_detect=bool(p.get("scene_detect", Config.scene_detect)),
        workers=int(p.get("workers", Config.workers)),
        faces_enabled=bool(raw.get("faces", {}).get("enabled", Config.faces_enabled)),
        embed_model=e.get("model", Config.embed_model),
        source=path,
    )


def set_value(path: str | os.PathLike, section: str, key: str, value: str) -> None:
    """Set `key = "value"` in [section] of contentrag.toml, keeping everything else (and comments) as is."""
    import re

    path = Path(path)
    text = path.read_text(encoding="utf-8")
    line = f'{key} = "{value}"'
    m = re.search(rf"^\[{re.escape(section)}\][^\S\n]*$", text, flags=re.M)
    if m is None:
        text = text.rstrip("\n") + f"\n\n[{section}]\n{line}\n"
    else:
        nxt = re.search(r"^\[", text[m.end():], flags=re.M)
        end = m.end() + (nxt.start() if nxt else len(text) - m.end())
        body = text[m.end():end]
        pattern = rf"^[ \t]*{re.escape(key)}[ \t]*=.*$"
        body = re.sub(pattern, line, body, count=1, flags=re.M) if re.search(pattern, body, flags=re.M) \
            else "\n" + line + body
        text = text[:m.end()] + body + text[end:]
    path.write_text(text, encoding="utf-8")


def set_backend(path: str | os.PathLike, backend: str) -> None:
    """Switch [describe] backend in contentrag.toml."""
    if backend not in BACKENDS:
        raise ValueError(f"backend must be one of: {', '.join(BACKENDS)}")
    set_value(path, "describe", "backend", backend)
