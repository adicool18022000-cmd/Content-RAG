"""Make the footage drive self-contained: plug it into any Mac, open Claude on it, ask for a video.

    crag drive setup            (run once from a computer that has this repo; again to update the code)

writes onto the drive (next to the library, so nothing here is ever scanned as footage):

    <drive>/ContentLibrary/app/          the code + a contentrag.toml with paths relative to the drive
    <drive>/ContentLibrary/models/       AI model downloads (face models, Whisper, embeddings), shared
    <drive>/crag                         runs crag from the drive (uses a small per-computer Python setup)
    <drive>/Set up this Mac.command      first time on a computer: double-click (installs ffmpeg/Python)
    <drive>/CLAUDE.md + .claude/skills/  what Claude needs to know to find clips and edit from this drive
    <drive>/START HERE.md                the same, for people

Index, vault, styles, transitions, assets and exports already live in ContentLibrary. API keys are
never written to the drive: they stay in each computer's environment (or use the Claude Code login).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import Config

REPO = Path(__file__).resolve().parents[1]
APP_FILES = ("contentrag", ".claude/skills", "pyproject.toml", "README.md", "CLAUDE.md",
             "contentrag.example.toml")
SKIP = {"__pycache__", ".DS_Store"}


def drive_of(cfg: Config) -> Path:
    """The drive the library is on (/Volumes/T7 for /Volumes/T7/ContentLibrary)."""
    lib = cfg.library_dir.resolve()
    parts = lib.parts
    if len(parts) > 2 and parts[1] in ("Volumes", "media", "mnt"):
        n = 4 if parts[1] == "media" and len(parts) > 4 else 3  # /media/<user>/<drive> on Linux
        return Path(*parts[:n])
    return lib.parent


def _copy_tree(src: Path, dst: Path) -> int:
    n = 0
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        return 1
    for p in src.rglob("*"):
        if any(part in SKIP for part in p.parts) or p.suffix == ".pyc" or not p.is_file():
            continue
        out = dst / p.relative_to(src)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, out)  # copyfile: exFAT can't take macOS permissions/xattrs
        n += 1
    return n


def _relative(value: str, drive: Path, app: Path) -> str:
    p = Path(value).expanduser()
    if not p.is_absolute():
        return value
    try:
        p.resolve().relative_to(drive.resolve())
    except ValueError:
        return value  # another drive: keep the absolute path
    return Path(os.path.relpath(p.resolve(), app.resolve())).as_posix()


def portable_config(text: str, drive: Path, app: Path, base: Path) -> str:
    """The user's contentrag.toml with every path on this drive made relative to the app folder."""
    def fix(m: re.Match) -> str:
        value = m.group(3)
        q = Path(value).expanduser()
        absolute = q if q.is_absolute() else (base / q)
        return f'{m.group(1)}{m.group(2)}"{_relative(str(absolute), drive, app)}"'

    return re.sub(r'^(\s*)((?:path|library_dir)\s*=\s*)"([^"]*)"', fix, text, flags=re.M)


def _version() -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=10).stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


CRAG_SH = r"""#!/bin/sh
# Runs crag from this drive. First time on a computer: double-click "Set up this Mac.command".
DRIVE="$(cd "$(dirname "$0")" && pwd)"
APP="$DRIVE/ContentLibrary/app"
VENV="${CRAG_VENV:-$HOME/.content-rag/venv}"
if [ ! -x "$VENV/bin/python" ]; then
  echo "crag isn't set up on this computer yet. Run:  sh \"$DRIVE/Set up this Mac.command\"" >&2
  exit 1
fi
export CONTENTRAG_CONFIG="${CONTENTRAG_CONFIG:-$APP/contentrag.toml}"
export PYTHONPATH="$APP${PYTHONPATH:+:$PYTHONPATH}"
export HF_HOME="${HF_HOME:-$DRIVE/ContentLibrary/models/huggingface}"
[ -f "$HOME/.content-rag/keys" ] && . "$HOME/.content-rag/keys"
exec "$VENV/bin/python" -m contentrag.cli "$@"
"""

SETUP_SH = r"""#!/bin/bash
# First time on a Mac: installs what crag needs ON THIS COMPUTER (ffmpeg, Python packages).
# Nothing personal is stored on the computer; the index, vault and styles stay on the drive.
set -e
DRIVE="$(cd "$(dirname "$0")" && pwd)"
APP="$DRIVE/ContentLibrary/app"
VENV="$HOME/.content-rag/venv"
echo "Setting up crag for the drive at $DRIVE"
if ! command -v brew >/dev/null 2>&1; then
  echo "Installing Homebrew (it asks for your Mac password)..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"
fi
command -v ffmpeg >/dev/null 2>&1 || brew install ffmpeg
PY=""
for c in python3.13 python3.12 python3.11; do command -v $c >/dev/null 2>&1 && PY=$c && break; done
if [ -z "$PY" ]; then brew install python@3.12; PY=python3.12; fi
mkdir -p "$HOME/.content-rag"
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
EXTRA=""
[ "$(uname -m)" = "arm64" ] && EXTRA="[mac]"
"$VENV/bin/pip" install -q "$APP$EXTRA"
if [ ! -f "$HOME/.content-rag/keys" ]; then
  printf '# API keys for crag on this computer (never stored on the drive)\n# export GEMINI_API_KEY="..."\n' \
    > "$HOME/.content-rag/keys"
fi
command -v claude >/dev/null 2>&1 || echo "Tip: install Claude Code (or the Claude desktop app) to edit by chat."
echo
if sh "$DRIVE/crag" status >/dev/null; then echo "crag works."; else echo "crag couldn't open the library (see above)."; fi
echo "Done. Open the Claude app -> Code -> choose the folder $DRIVE, and ask for a video."
echo "Gemini analysis on this computer: put your key in ~/.content-rag/keys (or use: crag backend claude-code)."
"""

CLAUDE_MD = """# This drive: {name}

The creator's footage archive (read-only), its search index and Obsidian life vault, and the code to
find clips and edit Instagram reels from them. Everything needed is on this drive; nothing personal
lives on the computer.

## Running it
- Commands: `./crag <command>` from this folder (the skills say `crag ...`: run it as `./crag ...`).
  If it says it isn't set up on this computer: `sh "Set up this Mac.command"` (needs the user's OK:
  it installs ffmpeg/Python with Homebrew). If `./crag` gives "permission denied": `sh crag ...`.
- `./crag status` shows what's indexed; `./crag ui` opens the dashboard.
- Code: `ContentLibrary/app/` (its `CLAUDE.md` explains the architecture). Config:
  `ContentLibrary/app/contentrag.toml` (paths relative to the drive).

## Where things are
- Index `ContentLibrary/index.sqlite`, vault `ContentLibrary/LifeVault` (read its `CLAUDE.md`, `Me.md`,
  `_generated/Index.md`), styles `ContentLibrary/styles`, assets + transition library
  `ContentLibrary/assets` (`./crag assets transitions`), finished edits `ContentLibrary/exports/<name>/`.
- Skills in `.claude/skills/`: find-clips, broll-plan (make the video), content-ideas, life-interview.

## Rules
- Footage folders are read-only. Never delete, move or rename footage (only `crag organize` moves
  files, after the user reviews a plan). Never delete anything in ContentLibrary.
- Hidden people never appear. A held-back clip (`./crag search ... --held-back`) is used only when the
  user asks for that clip in chat; their face is then blurred as a layer in HyperFrames / Remotion.
- New footage copied onto the drive: `./crag autopilot --only "<that folder>"` analyses just it.
- AI analysis needs a key on this computer (`~/.content-rag/keys`) or the Claude subscription
  (`./crag --backend claude-code autopilot ...`). Searching and editing need neither.
"""

START_HERE = """# Start here

This drive holds your footage, its searchable index, your life vault, editing styles, transitions,
and the app that edits with them.

## On any Mac
1. Plug in the drive.
2. First time on this Mac: double-click **Set up this Mac.command** (or in Terminal:
   `sh "/Volumes/{name}/Set up this Mac.command"`). It takes a few minutes, once.
3. Open the **Claude** app → **Code** → choose the folder **{name}** (the drive itself).
4. Ask: "make a reel from my Goa trip", "find clips of me riding at night", "interview me about 2019".

Terminal instead: `cd "/Volumes/{name}" && claude`. Dashboard: `./crag ui`.

## Keys
Gemini analysis of NEW footage needs your key on that computer: put
`export GEMINI_API_KEY="..."` in `~/.content-rag/keys`. Or use your Claude plan: `./crag backend claude-code`.
Finding clips and editing don't need any key.

## New footage
Copy it onto this drive, then: `./crag autopilot --only "/Volumes/{name}/<that folder>"`.

App version: {version}. To update: on a computer with the Content-RAG repo, `git pull`, then `crag drive setup`.
"""


def setup(cfg: Config, drive: Path | None = None, log=print) -> dict:
    drive = Path(drive).expanduser() if drive else drive_of(cfg)
    if not drive.is_dir():
        raise SystemExit(f"Drive not found: {drive}")
    lib = cfg.library_dir.resolve()
    try:
        lib.relative_to(drive.resolve())
    except ValueError:
        raise SystemExit(f"The library ({lib}) isn't on {drive}. Put library_dir on the drive first.")
    app = lib / "app"
    if REPO.resolve() == app.resolve():
        raise SystemExit("Run this from the copy on your computer (e.g. ~/Content-RAG), not from the drive.")
    n = sum(_copy_tree(REPO / f, app / f) for f in APP_FILES if (REPO / f).exists())
    for stale in app.joinpath("contentrag").rglob("*.py"):  # files removed from the code since last time
        if not (REPO / stale.relative_to(app)).exists():
            stale.unlink()
    log(f"[drive] code: {n} files -> {app}")

    if not cfg.source or not Path(cfg.source).exists():
        raise SystemExit("No contentrag.toml to copy.")
    text = Path(cfg.source).read_text(encoding="utf-8")
    (app / "contentrag.toml").write_text(portable_config(text, drive, app, Path(cfg.source).resolve().parent),
                                         encoding="utf-8")
    off = [r.name for r in cfg.roots if not _on(r.path, drive)]
    log("[drive] config -> " + str(app / "contentrag.toml")
        + (f" (on other drives, kept as is: {', '.join(off)})" if off else ""))

    (lib / "models" / "huggingface").mkdir(parents=True, exist_ok=True)
    hf = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    if hf.is_dir() and not any((lib / "models" / "huggingface" / "hub").glob("models--*")):
        log(f"[drive] tip: to reuse downloaded models instead of downloading again on other Macs, copy "
            f"{hf} to {lib / 'models' / 'huggingface' / 'hub'}")

    version = _version()
    files = {
        drive / "crag": CRAG_SH,
        drive / "Set up this Mac.command": SETUP_SH,
        drive / "CLAUDE.md": CLAUDE_MD.format(name=drive.name),
        drive / "START HERE.md": START_HERE.format(name=drive.name, version=version),
    }
    for path, body in files.items():
        path.write_text(body, encoding="utf-8", newline="\n")
        try:
            path.chmod(0o755 if path.suffix in ("", ".command") else 0o644)
        except OSError:
            pass  # exFAT: no permissions; `sh crag` always works
    skills = drive / ".claude" / "skills"
    if skills.exists():
        shutil.rmtree(skills)
    _copy_tree(REPO / ".claude" / "skills", skills)
    log(f"[drive] {drive}: crag, Set up this Mac.command, CLAUDE.md, START HERE.md, .claude/skills")
    return {"drive": str(drive), "app": str(app), "version": version, "files": n}


def _on(path: Path, drive: Path) -> bool:
    try:
        path.resolve().relative_to(drive.resolve())
        return True
    except ValueError:
        return False
