---
name: life-interview
description: Interview the creator about a period of their life, using their footage timeline as prompts, and write the answers into the Obsidian life vault (Eras, People, Places, Stories). Use when the user wants to build their second brain, tell their story, fill in an era, or when the vault has open questions.
---

# Life interview

Vault: `library_dir/LifeVault` (see `contentrag.toml`). Its `CLAUDE.md` has the rules; read it first.

1. Read `Me.md`, `_generated/Timeline.md`, and existing `Eras/`. Pick (or ask the user for)
   one era or cluster of events that has footage but little story.
2. Skim those event notes (moments, speech gists, places). Ask 5-10 short, specific questions
   anchored in the footage: "In Aug 2023 there are 14 clips from a night ride in Goa with two
   friends. Who were they and what was the trip about?" Ask a few at a time; the user may
   answer by voice in Hindi/Hinglish.
3. Save the raw Q&A to `Interviews/YYYY-MM-DD <topic>.md`.
4. Update or create `Eras/`, `People/` (with `consent:`), `Places/` notes, linking events and
   the interview. Never invent facts beyond what the user said or the footage shows.
5. If a story has a clear turning point and B-roll >= 4 footage, add it to `Stories/`
   with hook ideas.
6. End with what's still unknown and a suggestion for the next interview.
