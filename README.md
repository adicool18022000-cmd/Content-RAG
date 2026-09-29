# Content-RAG

Turns a personal photo/video archive (years of clips on external SSDs) into:

1. **A clip index**: every video is split into *moments* with in/out timestamps, and each
   moment gets a description, the Hindi/Hinglish speech with an English gist, shot type,
   B-roll quality score, place and date. You can search it by meaning ("me on the bike at
   night, vertical, good quality").
2. **A life vault**: an Obsidian vault (Markdown) with a timeline, event notes and thumbnails,
   plus Eras / People / Places / Stories notes that Claude fills in by interviewing you. It
   works as Claude's long-term memory of your life when it writes scripts and plans edits.

```
footage SSDs (never modified)
   │  crag scan        find files, remove duplicates, read date / GPS / orientation
   │  crag prep        sample frames, detect scene cuts, extract audio      (local, ffmpeg)
   │  crag transcribe  Whisper large-v3-turbo, Hindi          (optional, local, Apple GPU)
   │  crag describe    Gemini watches a 360p copy of each clip, audio included (default)
   │                   — or Claude looks at frame contact sheets (backend = "claude")
   │  crag embed       multilingual embeddings for search by meaning         (local)
   ▼  crag vault       Obsidian notes: timeline, events, B-roll cards
library_dir/  index.sqlite · frames/ · vectors.npz · LifeVault/
```

Only the `describe` step sends anything off your Mac. With Gemini that's a small temporary
360p copy of each clip, which is deleted after the request. With Claude it's frame contact
sheets. Your original files are never uploaded.

## The dashboard

```bash
export GEMINI_API_KEY=your-key     # the key stays in this terminal session, not in any file
crag ui                            # opens http://127.0.0.1:8765
```

A local page, reachable only from your own Mac, where you can:
- check that the key works and which Gemini models it can use
- run each step (or "Run everything") and watch progress
- see money spent so far and an estimate for the rest
- search your footage and play any result from its in-point

Closing the page doesn't stop a job, and quitting `crag ui` stops it safely. Run it again and
it continues where it left off.

## Setup (Mac)

```bash
brew install ffmpeg uv
git clone <this repo> && cd Content-RAG
uv venv && source .venv/bin/activate
uv pip install -e '.[mac]'          # mlx-whisper, bge-m3 embeddings, HEIC support, offline geocoding
cp contentrag.example.toml contentrag.toml   # then edit the paths
```

In `contentrag.toml`, add one `[[roots]]` entry for each footage folder, on any number of SSDs.
Use `kind = "brand"` for clips you shot on purpose for content. Set `library_dir` to a folder
on one SSD. Drives that aren't plugged in are skipped, and files are recognised by their
content, so they can be moved or renamed later without being re-indexed.

For the `describe` step you need a Gemini API key (aistudio.google.com). If you use
`backend = "claude"` instead, you need an Anthropic API key, which is billed separately from a
Claude subscription.

```bash
export GEMINI_API_KEY=...          # or ANTHROPIC_API_KEY for the Claude backend
```

## Running it (terminal alternative to the dashboard)

```bash
crag scan                      # a few minutes; then `crag status`
crag prep                      # thumbnails + scene cuts; overnight for ~1 TB (scene_detect=false is faster)
crag describe run --limit 5    # try 5 clips first, then check them with `crag search ...`
crag describe estimate         # rough cost for everything left
crag describe run              # the rest (4 requests in parallel; Ctrl+C and re-run to resume)
crag describe retry            # resend anything that failed or was blocked
crag embed
crag vault                     # then open library_dir/LifeVault as a vault in Obsidian
```

All steps can be re-run safely. They only process what is new, so after adding footage you
run the same sequence again. `crag transcribe` (local Whisper) is optional with Gemini, which
transcribes the audio itself. Enable it in `[transcribe]` if you want both transcripts.

### Cost and time for ~1 TB

Rough numbers; the dashboard's "Estimate remaining" gives the real figure after `prep`.
- 1 TB of phone video is about 100–250 hours of footage.
- `gemini-3.5-flash` at low media resolution costs about $0.4–0.6 per hour of footage:
  roughly 100 tokens per second of video, plus the descriptions it writes.
- `gemini-3.5-flash-lite` costs about $0.2 per hour.
- So the whole archive comes to about $50–150 on Flash and $20–50 on Flash-Lite, versus several
  hundred dollars with Claude Opus.
- `prep` runs locally for free. Most of the describe time goes on uploading the small proxies.

## Searching

```bash
crag search "typing on laptop late night" --vertical --min-broll 4 --html
crag search "college hostel friends" --from 2019 --to 2021 --kind video
crag search "sunset" --place Goa --exclude shaky --json
```

`--html` writes a contact sheet of thumbnails with in/out times and file paths.

## Using it with Claude Code

Open Claude Code in this repo on your Mac. Three skills are included:

- **find-clips**: "find me clips of the Nandi Hills ride, vertical"
- **broll-plan**: paste a script and get a beat-by-beat plan using your own footage, plus a
  shoot list for beats with nothing usable
- **life-interview**: Claude walks through your timeline era by era, asks you what really
  happened, and writes Eras/People/Stories notes

## Obsidian

Open `library_dir/LifeVault` as a vault and enable the **Bases** core plugin.
`_generated/Library.base` has a card gallery of your brand B-roll and a table of events.
Everything under `_generated/` is rebuilt by `crag vault`. Everything else belongs to you
and Claude and is never overwritten.

## Not built yet

- Face grouping (name a person once, find them everywhere).
- Direct export of a chosen clip list as a Premiere Pro XML timeline.
- Gemini Batch API (another 50% off, but results arrive hours later).
