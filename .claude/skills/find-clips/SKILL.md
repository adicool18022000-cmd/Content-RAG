---
name: find-clips
description: Search the creator's indexed footage archive for clips/moments matching a description (e.g. "me riding the bike at night", "typing on laptop, vertical"). Use whenever the user asks to find, pull, or show footage, B-roll, clips or photos from their library.
---

# Find clips

The footage index is queried with the `crag` CLI (run from this repo, `contentrag.toml` in the repo root).
The Obsidian vault (`library_dir/LifeVault`) has the same moments organised for browsing:
`_generated/Themes/` (best moments per theme), `_generated/Years/`, `_generated/Places/`.

1. If the request matches a theme ("founder grind", "bike rides", "college"), read that Theme page
   first; it lists the best moments with ids. Then search for anything more specific.
2. Turn the request into one or more search queries plus filters. Run several phrasings if the
   first is thin (e.g. "motorbike night ride", "bike highway dark", "riding POV").
   ```
   crag search "<query>" --json --limit 30 [filters]
   ```
   Filters: `--vertical` / `--horizontal` (Reels need vertical, or horizontal they can crop),
   `--min-broll 4`, `--from 2022 --to 2023-06`, `--place Bangalore`, `--root brand|archive`,
   `--kind video|photo`, `--min-seconds 3`, `--exclude shaky --exclude dark`.
3. Read the JSON. Drop weak matches yourself (the search is recall-oriented). Prefer higher
   `broll_score`, matching orientation, and no `quality_issues`.
4. Look at the `thumbnail` images of the top candidates before recommending them.
5. Reply with a short ranked list: `m<moment_id>`, in/out (`in_out`), what's in it, date/place.
   For more than ~6 results also write a contact sheet:
   `crag search "<query>" [filters] --html` and give the user the printed path.
6. If the user wants the clips: `crag pull m12 m48 --name <folder>` (see broll-plan).

If results mention a drive "not mounted", tell the user which root to plug in.
