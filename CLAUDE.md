# Content-RAG

Personal footage archive -> searchable clip index + Obsidian "life vault", for making Instagram
content with Claude. Runs on the creator's Mac (M5, 32 GB), footage on external SSDs.

- Package: `contentrag/` (CLI `crag`, see README for the pipeline order).
- Config: `contentrag.toml` (git-ignored; template `contentrag.example.toml`).
- Index: `library_dir/index.sqlite`; vault: `library_dir/LifeVault` (its own `CLAUDE.md`).
  Vault meaning layer: `Memories/` notes (event_id → what it meant, importance) are read by
  `vault.read_memories` and shown on generated event/year notes; `_generated/Interview queue.md`.
- Skills: `.claude/skills/find-clips`, `broll-plan`, `life-interview`.
- Tests: `pytest -q` (needs ffmpeg; no network).
- AI description has three backends chosen by `[describe] backend` (`crag backend <name>`, the
  dashboard Setup switch, or `crag --backend <name>` / `CRAG_BACKEND` for one run): `gemini`
  (default, `contentrag/gemini.py`: 360p proxy video+audio, concurrent, resumable), `claude-code`
  (`contentrag/claude_code.py`: the same contact sheets as `claude`, one headless `claude -p`
  call each, billed to the Claude subscription; ANTHROPIC_API_KEY is stripped from its env;
  usage limits wait until `resetsAt`) and `claude` (`contentrag/describe.py`: Batches API;
  refusals retried with `describe.retry_model`). All write through `describe.store_result`.
- Dashboard: `crag ui` (`contentrag/ui.py` + `ui.html`), bound to 127.0.0.1. API keys come from
  environment variables and must never be sent to the page.
- Unattended runs: `crag autopilot` (`contentrag/autopilot.py`): preflight checks, quota/network
  waits, MAX_ATTEMPTS (db.py) retries, spending cap, lock file, STOP file, state in
  `library_dir/autopilot.json`. Errors that aren't the file's fault (429, network, bad key,
  unplugged drive) must leave requests `pending` without using an attempt.
- `crag pull m<id> ...` (`contentrag/pull.py`) cuts moments + `selects.json` + FCP7 `timeline.xml`.
- Collections = the creator's folder names (`collections.py`); usage + hide list (`usage.py`);
  local face grouping with OpenCV YuNet/SFace (`faces.py`); a person = all face groups sharing a
  name (groups are never merged; `split_person` re-separates one); hidden people are removed by time
  range, never by dropping whole clips. A held-back moment (`search --held-back`) is used only when
  the creator asks in chat; their face is then a blur layer in HyperFrames/Remotion (+ preview), never
  burned into footage or pulled clips.
- Edit engine (`contentrag/edit/`): one `EditPlan` (plan.py) built by `talking.py` / `beat.py`,
  rendered by `render.py` (ffmpeg MP4, real Screen blend for plates) and `export.py` (Premiere FCP7 XML, AE JSX, HyperFrames,
  Remotion 4.0.532). Styles + assets in `style.py`. Transition
  recipes (plate + timed/levelled SFX) imported from the creator's Premiere template by
  `crag assets import` live in `transitions.py` / `library/assets/transitions/`. Song analysis in `music.py`.
- Portable drive: `crag drive setup` (`drive.py`) copies the code + a drive-relative config to
  `library_dir/app`, and `crag` launcher, `Set up this Mac.command`, CLAUDE.md, skills to the drive
  root (config paths may be relative to the toml). `crag autopilot/scan --only <folder>` = new footage
  in one folder (no pruning).
- Footage roots are read-only for everything except `crag organize apply/undo` (explicit
  plan -> review -> apply; moves within a drive only, never deletes). Everything else that is
  generated goes under `library_dir`.
