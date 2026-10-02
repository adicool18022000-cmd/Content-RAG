# Life Vault — rules for Claude

<!-- Updated by `crag vault`. Add the line <!-- custom --> anywhere to keep your own edits. -->

This Obsidian vault is the long-term memory of a content creator's life, built from their
photo/video archive plus interviews with them. Use it to write content that is true to their
life, and to find footage.

## Find your way (read in this order)

1. `_generated/Index.md`: the map: totals, years, places, themes, and how to pull clips.
2. `Me.md`: who the creator is now, content pillars.
3. `Eras/` + `Stories/`: the meaning behind the footage (written with the creator).
4. `_generated/Themes/<theme>.md`: best moments per content theme, best B-roll first.
5. `_generated/Years/<year>.md` → `_generated/Events/<year>/…`: what happened when, every
   moment with its id.
6. `_generated/Places/<city>.md`: everything shot in one city.

Every moment appears as one line:
`` - `m1234` 0:12–0:18 · vertical · B4 · description — “speech gist” · [[event]] ``
`m1234` is the moment id used by `crag pull`; `B4` is B-roll quality (1–5).

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
  people for public posts without flagging it.
- Speech in the footage is mostly Hindi/Hinglish. Quote the English gist; keep original
  Hindi only when the exact words matter.

## Making a video from a script

1. Understand the story: `Me.md`, the matching Era/Story notes. Never invent facts.
2. Split the script into beats (1.5–4 s each for a Reel). For each beat decide what should be
   on screen.
3. Find candidates: the matching `Themes/` page first, then Events of the right period, then
   `crag search "<what's on screen>" --vertical --min-broll 3 --json` (add `--from/--to`,
   `--place`, `--root brand`). Prefer vertical, B-roll ≥ 4, no quality issues; vary shot types;
   don't reuse a moment.
4. Show the creator the plan (beat → `m<id>`, why) and fix it with them.
5. `crag pull m12 m48 m7 ... --name <reel-name>` cuts the moments from the originals into
   `library/exports/<reel-name>/`: numbered clips, `selects.json` (order, in/out, description)
   and `timeline.xml` (File → Import in Premiere Pro gives the sequence).
6. Edit: hand the clips + `selects.json` to HyperFrames/ffmpeg, or open `timeline.xml` in
   Premiere. List beats with no good footage as a shoot list for the brand B-roll folder.

## Operations

- **Ingest**: after new footage is indexed and `crag vault` has run, read new events in
  `_generated/Timeline.md`, attach them to eras (create an era if needed), update People/Places.
- **Interview**: pick an era or event with open questions, ask the creator 5–10 short
  questions, save the Q&A to `Interviews/YYYY-MM-DD <topic>.md`, then update the notes.
- **Story mining**: propose Stories from eras with a clear turning point and good footage
  (B-roll ≥ 4).
- **Lint**: look for eras without events, events without an era, people without consent
  status, stories without footage.
