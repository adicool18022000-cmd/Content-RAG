# Life Vault — rules for Claude

This Obsidian vault is the long-term memory of a content creator's life, built from their
photo/video archive plus interviews with them. Use it to write content that is true to their
life, and to find footage.

## Layout

| Path | Who writes it | What it is |
|---|---|---|
| `_generated/` | the `crag vault` command | Rebuilt on every run. **Never edit by hand.** |
| `_generated/Timeline.md` | generated | Every event by month. Start here to orient in time. |
| `_generated/Events/` | generated | One note per cluster of footage (same place, within a few hours). Moments with timestamps and B-roll scores. |
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
- Do not link to `_generated/` notes from inside other `_generated/` notes; they are rebuilt.
- Use frontmatter properties (Obsidian Bases reads them); keep `type:` on every note.
- People who appear in footage have not necessarily agreed to be in public content. Mark
  `consent: unknown | yes | no` on People notes; don't propose footage of `no`/`unknown`
  people for public posts without flagging it.
- Speech in the footage is mostly Hindi/Hinglish. Quote the English gist; keep original
  Hindi only when the exact words matter.

## Operations

- **Ingest**: after new footage is indexed and `crag vault` has run, read new events in
  `_generated/Timeline.md`, attach them to eras (create an era if needed), update People/Places.
- **Interview**: pick an era or event with open questions, ask the creator 5–10 short
  questions, save the Q&A to `Interviews/YYYY-MM-DD <topic>.md`, then update the notes.
- **Story mining**: propose Stories from eras with a clear turning point and good footage
  (B-roll ≥ 4).
- **Lint**: look for eras without events, events without an era, people without consent
  status, stories without footage.
