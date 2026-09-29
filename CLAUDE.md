# Content-RAG

Personal footage archive -> searchable clip index + Obsidian "life vault", for making Instagram
content with Claude. Runs on the creator's Mac (M5, 32 GB), footage on external SSDs.

- Package: `contentrag/` (CLI `crag`, see README for the pipeline order).
- Config: `contentrag.toml` (git-ignored; template `contentrag.example.toml`).
- Index: `library_dir/index.sqlite`; vault: `library_dir/LifeVault` (its own `CLAUDE.md`).
- Skills: `.claude/skills/find-clips`, `broll-plan`, `life-interview`.
- Tests: `pytest -q` (needs ffmpeg; no network).
- Claude calls live only in `contentrag/describe.py`. Bulk work uses the Batches API; don't
  switch it to per-request calls. Refusals are retried with `describe.retry_model`.
- Never write inside the footage roots; everything generated goes under `library_dir`.
