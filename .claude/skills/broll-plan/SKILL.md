---
name: broll-plan
description: Turn a reel/video script into a beat-by-beat footage plan using the creator's own indexed clips. Use when the user gives a script (or idea) and wants footage, B-roll, or an edit plan from their library.
---

# Script -> B-roll plan from the archive

1. Split the script into beats (one per spoken line or visual idea, ~1.5-4 s each for Reels).
2. For each beat, write 1-3 visual search queries: literal ("typing on laptop at night") and
   emotional/thematic ("late night grind", "founder stress"). Use the life vault
   (`library_dir/LifeVault`: `Me.md`, `Eras/`, `Stories/`) to pick footage from the right
   period of life when the script references a real event ("my first month in Bangalore").
3. Run `crag search "<query>" --json --limit 15 --vertical --min-broll 3` (drop `--vertical`
   if nothing fits; horizontal can be cropped). Check thumbnails.
4. Pick one primary and one backup moment per beat. Avoid reusing a clip, vary shot types
   (wide -> close-up -> POV), and trim to the moment's in/out.
5. Output a table: beat | line | clip file | in-out | why | backup. Save it as
   `library_dir/LifeVault/Stories/<title> - edit plan.md` if the user wants to keep it,
   and write a contact sheet with `--html` for the chosen clips.
6. Flag beats with no good footage as "shoot list" items (what to film for the brand B-roll
   folder).

For the actual edit, the plan can be handed to HyperFrames/ffmpeg or Premiere Pro.
