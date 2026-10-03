---
name: content-ideas
description: Suggest reel ideas from the creator's own footage and life story - story seeds, collections with strong hooks, unused footage, what worked before. Use when the user has no idea what to post, asks "what should I make", wants a content calendar from their archive, or asks what stories their footage could tell.
---

# Content ideas from the archive

Read, in the vault (`library_dir/LifeVault`):
1. `Me.md` (who they are, pillars, pages), `Eras/`, `Stories/` - the story layer.
2. `_generated/Ideas.md` - story seeds (things that happened, often told to camera) and unused
   strong moments, best first.
3. `_generated/Collections/<name>.md` - per trip/period: story seeds, best hooks, cinematic,
   spectacle, funny..., ready-made reel recipes (hook → story → B-roll → close), what's used.
4. `crag videos` - what was already made/posted and how it performed; prefer formats/styles that
   did well, avoid repeating the same collection back-to-back.

Then propose 3-7 ideas. For each: title/hook line, which page, format (talking head with B-roll,
beat montage, story-to-camera cut), the footage (`m<id>` list from the notes or
`crag search ... --fresh --json`), what the creator would need to record (talking head lines),
and why it should work. Never invent facts about their life; if the footage shows something but
the meaning is unknown, ask (or suggest a life-interview session).
When the user picks one, continue with the `broll-plan` skill.
