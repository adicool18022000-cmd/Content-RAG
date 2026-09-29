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
   │  crag transcribe  Whisper large-v3-turbo, Hindi                        (local, Apple GPU)
   │  crag describe    Claude looks at frame contact sheets + transcript    (Claude API, batch)
   │  crag embed       multilingual embeddings for search by meaning         (local)
   ▼  crag vault       Obsidian notes: timeline, events, B-roll cards
library_dir/  index.sqlite · frames/ · vectors.npz · LifeVault/
```

Only small frames (640 px, tiled 9 per image) and transcripts leave your Mac, and only in
the `describe` step. The videos themselves never leave it.

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

For the `describe` step you need an Anthropic API key from console.anthropic.com. API usage
is billed separately from a Claude Pro/Max subscription:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## Running it

```bash
crag scan                      # a few minutes; run `crag status` afterwards
crag prep                      # overnight job for ~1 TB (set scene_detect=false to speed it up)
crag transcribe                # overnight job too; audio temp files are deleted as it goes

crag describe sync --limit 3   # try 3 requests right away, then look at the results:
crag search "bike" --no-vectors
crag describe estimate         # cost preview for everything left, nothing is sent
crag describe submit           # sends everything as batches (50% cheaper), done within 24 h
crag describe collect          # run later (again until nothing is "still_running")
crag describe retry            # resends the few refused/failed requests with retry_model

crag embed
crag vault                     # then open library_dir/LifeVault as a vault in Obsidian
```

All commands can be re-run safely. They only process what is new, so after adding footage
you run the same sequence again.

### Cost and time for ~1 TB

Rough numbers; `crag describe estimate` gives the real figure after `prep`.
- 1 TB of phone video is about 100–250 hours of footage.
- Claude Opus 5.5 via batch costs about $1.5–2 per hour of footage (short clips cost more
  per hour than long ones).
- `model = "claude-sonnet-5-5"` costs about half.
- Photos cost roughly $0.004 each on Opus in batch, and half that on Sonnet.
- `prep` and `transcribe` run locally for free. On an M5 expect a night or two for 1 TB.
  Temporary audio needs about 115 MB of disk per hour of footage until it's transcribed.

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
