---
name: broll-plan
description: Make a reel from the creator's own footage - a talking-head video with B-roll, hook, captions, SFX and transitions, or a beat-synced montage to a song - and export it for Premiere, After Effects, HyperFrames, Remotion or as an MP4. Use when the user gives a talking-head recording, a script, a song, or an idea and wants an edit, B-roll, or "make the video".
---

# Footage -> edit

Everything runs through `crag` in this repo (`contentrag.toml` points at the library). The
library's Obsidian vault (`library_dir/LifeVault`) explains the life context: read its `CLAUDE.md`,
`_generated/Index.md`, `Me.md`, and the relevant `Eras/`, `Stories/`, `_generated/Collections/`.

## Pick the style first
`crag style list` - one or more styles per Instagram page (captions, grade, light leaks, SFX,
pace, hook structure). Ask which page/style if unclear. New page or new look: the user gives
example reels -> `crag style learn <name> ref1.mp4 ref2.mp4 --page <page>`. Variants for
experiments (`page1-fast`) are fine; results are compared with `crag videos --by-style`.
Assets (light leaks, risers, shutter, whoosh, LUTs, fonts) live in `library_dir/assets/`
(`crag assets` shows counts); if a folder is empty that effect is skipped - tell the user.
The creator's transition library (`crag assets transitions`: T01 Snap ... T30 Riser Whoosh, each
a flash/leak plate + SFX with exact offsets and levels from their Premiere template) is used
automatically on cuts. In `plan.json` these are `overlay` clips with `role: "transition"` and a
note starting with the recipe id, plus their sounds. To swap one, replace the clip and its
sounds together, or set the style's `transitions.prefer` list and re-plan.

## A. Talking head (the main workflow)
```
crag edit talking ~/Desktop/take3.mp4 --name "thailand-scam" --style <style> --page <page> \
    [--collection thailand] [--music <song>] [--notes "what the video is about"]
```
It cuts pauses/fillers, transcribes (Hindi/Hinglish), opens with a hook clip from the library,
adds B-roll per line (fresh, privacy-safe), replays the hook moment at the payoff, captions,
SFX, leaks, and writes `library_dir/exports/<name>/`: `preview.mp4`, `premiere/timeline.xml`
(+ `captions.srt`), `aftereffects/build_comp.jsx`, `hyperframes/`, `remotion/`, `plan.json`, `EDIT.md`.

Then review like an editor:
1. Read `EDIT.md` (timeline + shoot list). Look at frames of `preview.mp4`
   (`ffmpeg -i preview.mp4 -vf fps=1,scale=270:-2,tile=6x3 sheet.jpg`).
2. Improve `plan.json` directly when something is weak: swap a B-roll clip (find a better one
   with `crag search "<what should be on screen>" --vertical --fresh --json`, then set its
   `file/src_in/src_out/media_id/moment_id`), change timings, captions, add/remove SFX.
   The same moment may appear twice (e.g. the hook teased at the start, shown in full later),
   or different parts of one clip - that's intended; avoid the *same part* twice.
3. `crag edit render "<name>"` re-renders everything from the plan.
4. Tell the user where the files are and list the shoot list (lines without good footage).

## B. Beat-synced montage
```
crag music analyse ~/Music/song.mp3 --name <song>      # tempo, beats, calm/build/peak, drops
crag edit beat <song> --name "goa-montage" --collection goa [--role cinematic --role action] \
    [--start 30 --end 60] [--fresh] --style <style>
```
Cuts land on beats; calm parts get slower cuts and calm footage, drops/peaks fast cuts and
high-motion footage; the first shot is the strongest hook. Review/adjust `plan.json` the same way.

## Rules
- Never use hidden people/clips (search and pull already exclude them; don't re-add them by hand).
  The one exception: a held-back moment (`crag search ... --held-back --json`, a hidden person is
  in it) that the user explicitly approved in chat, for this video. Put it into `plan.json` like any
  swap (`file/src_in/src_out/media_id/moment_id` from the search result), then `crag edit render`.
  The render adds `blur` boxes to that clip automatically. Their face is blurred as a layer in
  HyperFrames (`.face-blur` divs) and Remotion (`blur` in src/plan.json), plus preview.mp4. The
  footage and the cut pieces stay untouched, so Premiere / After Effects show the face. Tell the user
  to post from HyperFrames / Remotion, and point to "Blurred faces" in EDIT.md. Check the
  blur in the preview frames, because faces are only checked once per second; if it misses, widen
  that clip's boxes in plan.json and render again.
- Prefer footage not used before (`used×N` in search/vault); check `crag videos` for recent reels.
- Vertical footage first; horizontal is cropped or blurred-fill per style.
- After posting, record results: `crag videos --posted "<name>" --views N --likes N --saves N
  --shares N --retention 45` so styles can be compared.
