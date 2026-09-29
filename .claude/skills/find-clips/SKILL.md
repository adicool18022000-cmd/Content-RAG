---
name: find-clips
description: Search the creator's indexed footage archive for clips/moments matching a description (e.g. "me riding the bike at night", "typing on laptop, vertical"). Use whenever the user asks to find, pull, or show footage, B-roll, clips or photos from their library.
---

# Find clips

The footage index is queried with the `crag` CLI (run from this repo, `contentrag.toml` in the repo root).

1. Turn the request into one or more search queries plus filters. Run several phrasings if the
   first is thin (e.g. "motorbike night ride", "bike highway dark", "riding POV").
   ```
   crag search "<query>" --json --limit 30 [filters]
   ```
   Filters: `--vertical` / `--horizontal` (Reels need vertical, or horizontal they can crop),
   `--min-broll 4`, `--from 2022 --to 2023-06`, `--place Bangalore`, `--root brand|archive`,
   `--kind video|photo`, `--min-seconds 3`, `--exclude shaky --exclude dark`.
2. Read the JSON. Drop weak matches yourself (the search is recall-oriented). Prefer higher
   `broll_score`, matching orientation, and no `quality_issues`.
3. Look at the `thumbnail` images of the top candidates before recommending them.
4. Reply with a short ranked list: in/out (`in_out`), what's in it, date/place, file path.
   For more than ~6 results also write a contact sheet:
   `crag search "<query>" [filters] --html` and give the user the printed path.

If results mention a drive "not mounted", tell the user which root to plug in.
