# Life Vault — rules for Claude

<!-- Updated by `crag vault`. Add the line <!-- custom --> anywhere to keep your own edits. -->

This Obsidian vault is the long-term memory of a content creator's life, built from their
photo/video archive plus interviews with them. Use it to write content that is true to their
life, and to find footage.

## Find your way (read in this order)

1. `_generated/Index.md`: the map: totals, collections, years, places, themes, how to pull clips.
2. `Me.md`: who the creator is now, content pillars, pages.
3. `Eras/` + `Memories/` + `Stories/`: the meaning behind the footage (written with the creator).
   A Memory is one event in the creator's words: what it meant, feeling, importance 1–5. It also
   shows on the event note and its Year page. `_generated/Interview queue.md` lists big events
   that have no memory yet.
4. `_generated/Collections/<folder>.md`: per trip/period (the creator's own folder names): story
   seeds, best hooks, cinematic, spectacle, told-to-camera, funny, emotional, establishing,
   transitions, reel recipes, what's already used.
5. `_generated/Ideas.md`: every story seed + unused strong moments. `_generated/Videos.md`: reels
   made so far, styles, results. `_generated/People.md`: face groups (hidden people never appear).
6. `_generated/Themes/<theme>.md`: best moments per content theme, best B-roll first.
7. `_generated/Years/<year>.md` → `_generated/Events/<year>/…`: what happened when, every
   moment with its id.
8. `_generated/Places/<city>.md`: everything shot in one city. `Styles/`: editing styles per page.
   `Music/`: analysed songs (tempo, sections, drops).

Every moment appears as one line:
`` - `m1234` 0:12–0:18 · vertical · B4 · description — “speech gist” · [[event]] ``
`m1234` is the moment id used by `crag pull`; `B4` is B-roll quality (1–5), `H4` hook strength,
`used×2` how many reels already used it (prefer fresh footage; reusing a different part is fine).

## Layout

| Path | Who writes it | What it is |
|---|---|---|
| `_generated/` | the `crag vault` command | Rebuilt on every run. **Never edit by hand.** |
| `_generated/Timeline.md` | generated | Years with event counts. |
| `_generated/Index.md` | generated | Start page / map. |
| `_generated/Years/`, `Places/`, `Themes/` | generated | Overviews: per year (by month), per city, per content theme. |
| `_generated/Events/<year>/` | generated | One note per cluster of footage (same place, within a few hours). Every moment with id, timestamps and B-roll score. |
| `_generated/Broll/` | generated | One note per purpose-shot brand clip. |
| `Me.md` | Claude + creator | Who they are now, what they build, content pillars. Read first. |
| `Eras/` | Claude + creator | Chapters of life (e.g. College Y1, Moving to Bangalore). The story layer. |
| `Memories/` | Claude + creator | One note per event that mattered: `event_id` (from the event note's properties), `era`, `people`, `feeling`, `importance` (1–5), `## What it meant` (their words), what happened, content angle. Template: `Memories/_Memory template.md`. |
| `People/`, `Places/` | Claude + creator | Recurring people and places, linked from eras and stories. |
| `Stories/` | Claude + creator | Content-ready stories: what happened, why it matters, footage, hooks. |
| `Interviews/` | Claude | Raw Q&A from interview sessions, dated. Source of truth for facts not visible in footage. |

## Rules

- Footage shows *what* happened; only the creator knows *why* it mattered. Never invent
  feelings, motives, names or facts. If something is unknown, write it as an open question
  under `## Open questions` in the relevant note and ask in the next interview.
- Cite sources: link events (`[[_generated/Events/...]]`) or interviews for every claim in
  Eras, People and Stories.
- Link generously: eras ↔ events, people ↔ eras, stories ↔ eras and footage.
- Link to `_generated/` notes freely, but never edit them (they are rebuilt); put your own words in
  Eras, People, Places, Stories and Interviews.
- Use frontmatter properties (Obsidian Bases reads them); keep `type:` on every note.
- People who appear in footage have not necessarily agreed to be in public content. Mark
  `consent: unknown | yes | no` on People notes; don't propose footage of `no`/`unknown`
  people for public posts without flagging it. Anyone the creator hides
  (`crag people hide <name>`, `crag hide ...`) must never be suggested or re-added by hand.
- Speech in the footage is mostly Hindi/Hinglish. Quote the English gist; keep original
  Hindi only when the exact words matter.

## Making a video

- Talking head: `crag edit talking <recording> --name <reel> --style <style> --page <page>
  [--collection <trip>] [--notes "..."]` → `library/exports/<reel>/` with preview.mp4, Premiere
  XML + SRT, After Effects JSX, HyperFrames and Remotion projects, `plan.json`, `EDIT.md`.
  Improve `plan.json` (better B-roll, timing, captions), then `crag edit render "<reel>"`.
- Music montage: `crag music analyse <song>` then `crag edit beat <song> --name <reel> --collection ...`.
- Styles: `crag style list`; new ones from example reels: `crag style learn <name> refs... --page <page>`.
- Story first: `Me.md`, the matching Era/Story/Collection notes. Never invent facts.
- After posting: `crag videos --posted "<reel>" --views ... --saves ...` (feeds `Videos.md`).

## Operations

- **Ingest**: after new footage is indexed and `crag vault` has run, read new events in
  `_generated/Timeline.md`, attach them to eras (create an era if needed), update People/Places.
- **Interview**: pick an era, or events from `_generated/Interview queue.md`. Show the creator
  what the footage shows (date, place, who's in it, a few moments), then ask 3–5 short questions
  per event: what was happening, who was there, how it felt, what it changed, how much it matters
  now (1–5). Save the raw Q&A to `Interviews/YYYY-MM-DD <topic>.md`, write a `Memories/` note per
  event (their words under `## What it meant`), then update Eras/People/Places/Stories and `Me.md`.
  Then run `crag vault` so event and year notes show the memories.
- **Story mining**: propose Stories from eras with a clear turning point and good footage
  (B-roll ≥ 4).
- **Lint**: look for eras without events, events without an era, memories without an era,
  people without consent status, stories without footage.
- **Using it**: before proposing a reel, read the Memories of the events it uses. Importance ≥ 4
  and a strong feeling make the best stories. Never put words in the creator's mouth that aren't
  in a Memory or Interview.
