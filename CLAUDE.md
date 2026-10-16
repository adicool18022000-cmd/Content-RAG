# Content-RAG

Personal footage archive -> searchable clip index + Obsidian "life vault", for making Instagram
content with Claude. Runs on the creator's Mac (M5, 32 GB), footage on external SSDs.

- Package: `contentrag/` (CLI `crag`, see README for the pipeline order).
- Config: `contentrag.toml` (git-ignored; template `contentrag.example.toml`).
- Index: `library_dir/index.sqlite`; vault: `library_dir/LifeVault` (its own `CLAUDE.md`).
  Vault meaning layer: `Memories/` notes (event_id → what it meant, importance) are read by
  `vault.read_memories` and shown on generated event/year notes; `_generated/Interview queue.md`.
  Dashboard Memories tab (`memories.py`): same event ids as the vault, media + voice (local
  mlx-whisper via `/api/transcribe`) → `save_memory` (keeps sections it doesn't manage).
  Story mode (`story.py`): chunked recording + timeline → `voice.py` on the whole recording in ≤28 s speech
  pieces, each segment → event on screen (language always set: mixed=hi / en / translate; quality fast = turbo
  `whisper_model`, best = large-v3 `memory_model`; temperatures (0, 0.4); prompt = people, places, folders,
  `library_dir/vocabulary.txt`, events on screen; cleaner drops wrong scripts/languages, loops, "foreign", phantom
  lines) → AI drafts = full retelling, not a summary (claude-code login, else Gemini, else
  words as said; size-based batches, summaries retried) → review (Belongs to / move) → `approve`; audio kept in
  `library_dir/story/audio/`. `crag memories repair` (`memory_repair.py`) fixes notes from the old pipeline (text
  only): AI placement by sentence ranges moves what was told on the wrong event, then every old Story-mode note
  is rewritten in full (`## Earlier versions (voice-to-text errors)`, `needs_retelling`); notes are backed up to
  `Memories/_before repair/<time>/` with a manifest (`--undo`). `move_memory` / `suggest_move` for single moves.
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
  range, never by dropping whole clips. Wrong hidden tags: `faces.retag_faces` / `recheck_hidden` / `name_faces` (dense matches must
  also beat every non-hidden group); every decision goes to `face_reviews` (undoable). `decision='allow'` =
  hidden person allowed in that photo/video (`usage.allowed_here`, honoured by all hidden-range helpers). `'present'` =
  hidden person tagged by hand (no face found; whole item counts, `usage.tagged_here`); `'skip'` = not for videos
  (search/pull). A held-back moment (`search --held-back`) is used only when
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
