# Content-RAG

Personal footage archive -> searchable clip index + Obsidian "life vault", for making Instagram
content with Claude. Runs on the creator's Mac (M5, 32 GB), footage on external SSDs.

- Package: `contentrag/` (CLI `crag`, see README for the pipeline order).
- Config: `contentrag.toml` (git-ignored; template `contentrag.example.toml`).
- Index: `library_dir/index.sqlite`; vault: `library_dir/LifeVault` (its own `CLAUDE.md`).
- Skills: `.claude/skills/find-clips`, `broll-plan`, `life-interview`.
- Tests: `pytest -q` (needs ffmpeg; no network).
- AI description has two backends chosen by `[describe] backend`: `gemini` (default,
  `contentrag/gemini.py`: 360p proxy video+audio, concurrent requests, resumable) and `claude`
  (`contentrag/describe.py`: contact sheets via the Batches API; refusals retried with
  `describe.retry_model`). Both write through `describe.store_result`.
- Dashboard: `crag ui` (`contentrag/ui.py` + `ui.html`), bound to 127.0.0.1. API keys come from
  environment variables and must never be sent to the page.
- Unattended runs: `crag autopilot` (`contentrag/autopilot.py`): preflight checks, quota/network
  waits, MAX_ATTEMPTS (db.py) retries, spending cap, lock file, STOP file, state in
  `library_dir/autopilot.json`. Errors that aren't the file's fault (429, network, bad key,
  unplugged drive) must leave requests `pending` without using an attempt.
- `crag pull m<id> ...` (`contentrag/pull.py`) cuts moments + `selects.json` + FCP7 `timeline.xml`.
- Never write inside the footage roots; everything generated goes under `library_dir`.
