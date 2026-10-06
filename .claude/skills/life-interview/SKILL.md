---
name: life-interview
description: Interview the creator about a period of their life, using their footage timeline as prompts, and write the answers into the Obsidian life vault (Eras, Memories, People, Places, Stories). Use when the user wants to build their second brain, tell their story, says "interview me", wants to record what an event meant to them, or when the vault has open questions.
---

# Life interview

Vault: `library_dir/LifeVault` (see `contentrag.toml`). Its `CLAUDE.md` has the rules; read it first.
The creator can also tell stories on their own in the dashboard's **Memories** tab (`crag ui`):
events in date order with the photos/videos, voice or typed answers, saved as `Memories/` notes
(`source: memories tab`). When they've done that, read the new memories and ask only follow-up
questions (missing people, eras, what came after), then do steps 5-7.
The footage shows *what* happened. This interview captures *what it meant*, which only the
creator knows.

1. Read `Me.md`, `_generated/Timeline.md`, existing `Eras/` and `Memories/`. Choose what to cover:
   the era the user names, or the top of `_generated/Interview queue.md` (big events with no
   memory yet). Go in time order within an era.
2. For each event, open its note and look at 2-3 thumbnails. Tell the user what the footage
   shows in one line ("12 Aug 2023, Goa, night: 14 clips of a bike ride with two friends,
   lots of laughing"). Then ask 3-5 short questions, a few at a time:
   - What was going on in your life then? Why were you there?
   - Who are these people to you? (Name them; ask if they're OK to appear publicly.)
   - How did it feel? What do you remember that isn't on camera?
   - Did it change anything? What came after?
   - How much does it matter to you now, 1-5?
   The user may answer by voice in Hindi/Hinglish. Keep their words, and translate the gist.
   If they don't want to talk about something, skip it and don't ask again.
3. Save the raw Q&A to `Interviews/YYYY-MM-DD <topic>.md`.
4. For each event that matters (importance ≥ 2), write `Memories/<date> <short title>.md` from
   `Memories/_Memory template.md`: `event_id` (copied from the event note's properties), `date`,
   `era`, `people`, `feeling`, `importance`, and `## What it meant` as one or two sentences in
   the user's own words. Link the interview and the event.
5. Update or create `Eras/` (what life looked like, turning points, linked memories), `People/`
   (with `consent:`), `Places/`, and the "Story so far" in `Me.md`. Never invent facts,
   feelings or names beyond what the user said or the footage shows.
6. If a memory has a clear turning point, importance ≥ 4 and B-roll ≥ 4 footage, add it to
   `Stories/` with hook ideas.
7. Run `crag vault` so event and year notes show the memories, and the queue updates.
8. End with what's still unknown and what to cover next time.

## Review / clean up told memories

When the user asks to check or clean their memories (e.g. "review my memories till 2019"):

1. Read `Memories/**/*.md` for that period (skip `_merged/` and the template), oldest first,
   with each note's event note for context.
2. Fix voice-to-text problems without changing what they meant: misheard words, broken
   sentences, filler ("umm", "uh", "like", repeated words), Whisper's made-up lines on silence
   ("thanks for watching", "subscribe"). Keep Hinglish phrases where they matter, put names and
   places right only when the footage or other notes confirm them, and never add facts, feelings
   or names. Before the first change, keep the original text under `## As told (voice)` so nothing is lost.
3. If the note says it's not important, private, or not to be used ("not important", "don't use",
   "isko content mein mat daalna", "personal"), set `content: no` in the frontmatter (and
   `importance: 1` if empty). Their clips are then never suggested for videos.
4. Fill `people`, `era`, `feeling` and `importance` only from what the text says. Anything unclear
   goes under `## Open questions` in that note.
5. Run `crag vault`, then report: how many notes were cleaned, which are now `content: no`, the
   open questions, and which eras/people emerged (offer to write the `Eras/` and `People/` notes).
