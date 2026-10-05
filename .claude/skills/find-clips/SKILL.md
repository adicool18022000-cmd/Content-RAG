---
name: find-clips
description: Search the creator's indexed footage archive for clips/moments matching a description (e.g. "me riding the bike at night", "typing on laptop, vertical", "funny moments from Thailand"). Use whenever the user asks to find, pull, or show footage, B-roll, clips or photos from their library.
---

# Find clips

The footage index is queried with the `crag` CLI (run from this repo, `contentrag.toml` in the repo root).
The Obsidian vault (`library_dir/LifeVault`) has the same moments organised for browsing:
`_generated/Collections/` (per trip/folder: hooks, cinematic, spectacle, funny, story seeds...),
`_generated/Themes/`, `_generated/Years/`, `_generated/Places/`, `_generated/Ideas.md`.

1. If the request names a trip/period/folder, read that Collection note first; it lists the best
   moments per role with ids. Then search for anything more specific.
2. Search, several phrasings if the first is thin:
   ```
   crag search "<query>" --json --limit 30 [filters]
   ```
   Filters: `--collection thailand`, `--role hook|cinematic|spectacle|story_to_camera|funny|emotional|
   friends|establishing|transition|action|food|calm|work` (repeatable), `--min-hook 4`, `--fresh`
   (never used before), `--vertical` / `--horizontal`, `--min-broll 4`, `--from 2022 --to 2023-06`,
   `--place Bangalore`, `--root brand|archive`, `--kind video|photo`, `--exclude shaky`.
3. Read the JSON: `roles`, `hook_score`, `broll_score`, `uses`/`used_in` (prefer fresh), `story_seed`,
   `trimmed_for_privacy` (a hidden person was cut out of this range - keep the given in/out).
   Hidden people/clips/collections never appear; don't try to work around that.
   **Held back.** Moments where a hidden person is on screen are left out by default. Run the same
   search with `--held-back` too. If one of those is clearly better than what you found, mention it
   briefly, without suggesting it as a pick: "m88 (Goa sunset) would fit well, but Richa is in it;
   I can use it with her face blurred if you want". Use one only when the user says so for that clip.
   Then it goes into an edit (broll-plan), never `crag pull`. The blur is a layer in the HyperFrames and
   Remotion exports (and preview.mp4); the footage itself is never changed. Photos with a hidden
   person are never offered.
4. Look at the `thumbnail` images of the top candidates before recommending them.
5. Reply with a short ranked list: `m<moment_id>`, in/out, what's in it, date/collection, used×N.
   For more than ~6 results also write a contact sheet (`--html`) and give the path.
6. To cut them out: `crag pull m12 m48:3.5-7 --name <folder> [--reframe crop]` (a range after the
   colon uses a different part of that clip). For a full edit use the broll-plan skill.

If results mention a drive "not mounted", tell the user which root to plug in.
