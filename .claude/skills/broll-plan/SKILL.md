---
name: broll-plan
description: Turn a reel/video script or idea into an edit made from the creator's own footage - beat-by-beat footage plan, clips pulled from the originals, and an edit (HyperFrames or Premiere timeline). Use when the user gives a script or idea and wants footage, B-roll, an edit plan, or "make the video".
---

# Script -> footage -> edit

Vault: `library_dir/LifeVault` (path in `contentrag.toml`). Read its `CLAUDE.md` and
`_generated/Index.md` first.

1. **Story.** Read `Me.md` and the Era/Story notes the script touches ("my first month in
   Bangalore" -> that era). Don't invent facts; ask if something is unclear.
2. **Beats.** Split the script into beats (one per spoken line or visual idea, ~1.5-4 s each for
   Reels). For each, decide what should be on screen (literal + emotional).
3. **Candidates per beat.** Matching `_generated/Themes/` page -> Events of the right period ->
   `crag search "<query>" --json --limit 15 --vertical --min-broll 3` (drop `--vertical` if
   nothing fits; horizontal can be cropped). Look at thumbnails.
4. **Choose.** One primary + one backup moment per beat. No reused moments, vary shot types
   (wide -> close-up -> POV), match energy to the line, prefer B-roll >= 4 and no quality issues.
5. **Show the plan** as a table: beat | line | `m<id>` | in-out | why | backup. Adjust with the
   user. Beats with no good footage -> "shoot list" for the brand B-roll folder.
6. **Pull.** `crag pull m12 m48 m7 ... --name <reel-name>` -> `library/exports/<reel-name>/`
   with numbered clips (original quality, 0.5 s handles), `selects.json`, `timeline.xml`.
7. **Edit.**
   - Premiere Pro: File -> Import `timeline.xml` -> sequence with the clips in order, trimmed.
   - HyperFrames / ffmpeg: build the composition from `selects.json` (`file`, `clip_in`,
     `clip_out` give the exact moment inside each clip), add captions from the script.
8. Save the plan to `Stories/<title> - edit plan.md` (beats, moment ids, export folder).
