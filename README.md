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

## Overnight / unattended run (Autopilot)

```bash
export GEMINI_API_KEY=...
crag autopilot            # or the "Start Autopilot" button in `crag ui`
```

Autopilot runs every step in order (scan → prepare → AI analysis → retries → search index →
Obsidian vault). It keeps going until everything is done or 24 hours pass (`--hours 48` for
longer). Before it starts it checks that ffmpeg is installed, the key works, the model exists and
at least one drive is plugged in. That way a setup mistake can't fail every file overnight.

**For a smooth night:**
- Keep the Mac on its charger, with the lid open or an external display attached. Autopilot runs
  `caffeinate` so the Mac doesn't sleep, but macOS still sleeps when the lid closes on battery.
- Running from Terminal (`crag autopilot`) is safest. It doesn't depend on the browser. You can
  still watch it in the dashboard: run `crag ui` in a second Terminal window. Stop works from
  either place.
- Set `[gemini] budget_usd` in `contentrag.toml` as a spending cap.
- Progress and a "done around HH:MM" estimate are shown on the dashboard, and the full log is in
  `library_dir/logs/`.
- If anything kills it (crash, power cut, closed Terminal), start it again. Nothing already done
  is redone.

### What can go wrong, and what happens

| Situation | What Autopilot does |
|---|---|
| Files still being copied onto the drive | Skips anything modified in the last 2 minutes; the next scan picks it up |
| Same clip in several folders / on both SSDs | Analysed once (recognised by content, not name) |
| Empty (0-byte) or damaged files | Skipped and listed under Problems; the run continues |
| A file that makes ffmpeg hang | Every ffmpeg call has a time limit; counts as one failed attempt |
| iPhone Live Photo videos, clips under 1.5 s | Indexed but not sent to the AI (no cost) |
| Huge 4K files / long clips | Only a small 360p copy is sent; clips over 6 min are split into parts |
| Metadata says a clip is longer than it is | The empty part is marked done instead of failing forever |
| Gemini answer too long (very talkative clip) | Asked again without the word-for-word transcript |
| Gemini quota / rate limit (429) | Waits 15 min → 30 min → 1 h and continues; no attempt used |
| Gemini overloaded / server error | Retried with back-off; the inline upload falls back to the Files API |
| Wi-Fi drops | Waits 5 min and continues; no attempt used |
| Wrong API key / model name | Stops at once with a clear message instead of failing every file |
| A clip keeps failing | Retried up to 3 times across runs, then listed under Problems (status "gave up") |
| Drive unplugged before or during the run | Its files wait (no attempt used); the summary names the drive |
| Library drive almost full (< 2 GB) | Preparation pauses with a message |
| Spending cap reached | Analysis pauses; raise `budget_usd` and start again |
| File deleted or replaced on a plugged-in drive | Removed or re-analysed on the next scan |
| Two runs at once (Terminal + dashboard) | The second one refuses to start (lock file) |

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

## From script to edit

```bash
crag search "late night laptop work" --vertical --min-broll 4 --json   # moment ids look like m1234
crag pull m1234 m88 m512 --name founder-reel
```

`library_dir/exports/founder-reel/` then contains:
- the moments cut from the originals at full quality, with 0.5 s handles
- `selects.json`, which records the order, in/out points and descriptions, for Claude or HyperFrames
- `timeline.xml`. Use File → Import in Premiere Pro to get a sequence with the clips in order.

On the dashboard you can tick search results and click **Export selected clips** to do the same.

## Using it with Claude Code

Open Claude Code in this repo on your Mac. Three skills are included:

- **find-clips**: "find me clips of the Nandi Hills ride, vertical"
- **broll-plan**: paste a script. Claude plans each beat with your own footage, pulls the clips
  and prepares the edit (HyperFrames, or a Premiere timeline), plus a shoot list for beats with
  nothing usable
- **life-interview**: Claude walks through your timeline era by era, asks you what really
  happened, and writes Eras/People/Stories notes

## Obsidian

Open `library_dir/LifeVault` as a vault and enable the **Bases** core plugin.
`_generated/Library.base` has a card gallery of your brand B-roll and a table of events.
Everything under `_generated/` is rebuilt by `crag vault`. Everything else belongs to you
and Claude and is never overwritten.

## Not built yet

- Face grouping (name a person once, find them everywhere).
- Gemini Batch API (another 50% off, but results arrive hours later).
