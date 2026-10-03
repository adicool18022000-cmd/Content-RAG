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

## Collections, usage and privacy

- **Collections:** your folder names ("Thailand Trip", "Summer Stay Hostel", "Sem 5") become
  collections. Generic folders like DCIM, WhatsApp or 2023 are ignored. Search with
  `crag search ... --collection thailand`. In the brain, each collection gets a note:
  - what happened, and its story seeds
  - the best clips by role (hooks, cinematic, spectacle, told to camera, funny, emotional,
    establishing, transitions, food, friends)
  - ready-made reel recipes, and which clips have been used already
- **Every moment is tagged** with roles, a hook score (H1–H5), a story seed and a motion level
  (for matching music).
- **Usage:** every export records which part of which clip went into which reel. Search shows
  `used×N` and ranks fresh footage first; `--fresh` hides anything already used. Using a
  different part of the same clip (`m12:3-7`) is fine.
- **Hide list:** `crag hide moment|media|collection <ref>`. Hidden items never appear in search,
  the brain, exports or edits.
- **People:** `crag faces` groups faces on your Mac; nothing is uploaded.
  - `crag people` lists the groups.
  - `crag people name 7 Riya` names a group; giving two groups the same name merges them.
  - `crag people hide Riya` hides that person everywhere.
  - Their clips are rechecked second by second, and only the parts where they appear are
    removed. The rest of those clips stays usable.
  - The dashboard has a People panel for all of this.

## Tidy the footage folders

```bash
crag organize plan     # nothing moves; opens a review page (library/organize/plan-*.html)
crag organize apply    # moves exactly as planned
crag organize undo     # puts everything back
```
- Duplicates go to `_Duplicates/`.
- Everything else goes into `Year/<YYYY-MM> <your folder name>/`.
- Big mixed folders (phone dumps) are split by trip: `2025/2025-11-14 Phuket - Fire show night/`.
- Files only move within the same drive and nothing is ever deleted. iPhone sidecar files
  (`.AAE`) travel with their videos, and the index follows, so nothing is analysed again.

## Making reels

### Talking head + B-roll
```bash
crag edit talking ~/Desktop/take3.mp4 --name thailand-scam --style page1 --page page1 \
     [--collection thailand] [--music song] [--notes "what it's about"]
```
1. Cuts pauses and filler words from your take.
2. Transcribes it (Hindi/Hinglish).
3. Opens with a hook clip from your library.
4. Adds B-roll for each line: fresh, privacy-safe and matched by meaning.
5. Shows the hook moment again in full at the payoff line.
6. Adds Hinglish captions with highlighted words, plus light leaks and sound effects from your
   assets folder.

Results go to `library/exports/<name>/`:

| File | Use |
|---|---|
| `preview.mp4` | Watch it right away |
| `premiere/timeline.xml` + `captions.srt` | Premiere Pro: File → Import. V1 talk, V2 B-roll, V3 leaks. Uses your originals, so every cut can still be trimmed |
| `aftereffects/build_comp.jsx` | After Effects: File → Scripts → Run Script File. Builds the comp with editable caption layers |
| `hyperframes/` | `npx hyperframes preview` / `render` |
| `remotion/` | `npm i && npm run studio` / `npm run render` |
| `plan.json` + `EDIT.md` | The timeline. Change it (or ask Claude to), then `crag edit render <name>` |

### Music montage
```bash
crag music analyse ~/Music/song.mp3 --name song     # BPM, beats, calm/build/peak sections, drops
crag edit beat song --name goa-montage --collection goa --start 30 --end 60
```
Cuts land on the beats. Calm parts get slower cuts with calm footage; drops get fast cuts with
high-motion footage. A riser plays into each drop and a leak marks each section change.

### Styles (one or more per Instagram page)
```bash
crag style learn page1 ref1.mp4 ref2.mp4 --page page1   # from example reels you like
crag style list ; crag style show page1                 # tweak library/styles/page1.json
```
A style covers: pace, caption look, colour grade, transition type, which sound effects to use
and where, and the hook structure.

### Assets
Your light leaks, sound effects, LUTs and fonts go in `library/assets/` (`crag assets` shows
the folders):
- `light_leaks/`
- `sfx/riser/`, `sfx/shutter/`, `sfx/whoosh/`, `sfx/impact/`, `sfx/pop/`
- `luts/`
- `fonts/`

If a folder is empty, that effect is simply skipped.

### Experiments
After posting, run `crag videos --posted thailand-scam --views 12000 --saves 340 --retention 41`.
Then `crag videos --by-style` (and the brain's `Videos.md`) show which styles work best on each
page.

## Using it with Claude Code

Open Claude Code in this repo on your Mac. Four skills are included:

- **find-clips**: "find me clips of the Nandi Hills ride, vertical"
- **broll-plan**: give your talking-head take (or a song) and Claude builds the edit with the
  commands above, reviews the preview, improves the plan and re-renders
- **content-ideas**: no idea what to post? Claude goes through the story seeds, collections,
  unused footage and past results and proposes reels
- **life-interview**: Claude walks through your timeline era by era, asks you what really
  happened, and writes Eras/People/Stories notes

## Obsidian

Open `library_dir/LifeVault` as a vault and enable the **Bases** core plugin.
`_generated/Library.base` has a card gallery of your brand B-roll and a table of events.
Everything under `_generated/` is rebuilt by `crag vault`. Everything else belongs to you
and Claude and is never overwritten.

## Not built yet

- Gemini Batch API (another 50% off, but results arrive hours later).
- Automatic colour grade in the Premiere / After Effects exports (the MP4, HyperFrames and Remotion
  outputs apply it; in Premiere/AE apply your LUT or preset).
