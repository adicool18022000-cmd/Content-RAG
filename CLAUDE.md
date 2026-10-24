# Content-RAG — project memory

## Voiceovers: VoiceStudio with Adi's cloned voice

All voiceovers are generated with [VoiceStudio](https://github.com/debpalash/VoiceStudio)
(default engine: OmniVoice), using Adi's own cloned voice.

### Setup that works
- Runs locally on Adi's MacBook Air (Apple Silicon) through the VoiceStudio desktop app.
  The app must be open; it serves the API at `http://127.0.0.1:3900`.
- No API key is needed from the same Mac (loopback is unauthenticated).
  Docker is not used on the Mac: it cannot reach the Apple GPU.
- Voice profile: **`adi`**. It was created from a ~10 s clip plus an exact transcript.
  The older profile `aaadi` is broken (it has no transcript); never use it.

### Rule 1: always set the language to Hindi
Adi's accent comes out best with language **Hindi**, even when the script is
mostly or entirely English. Send `language: "hi"` (or `"Hindi"`) on every request.
Never leave it on Auto or set it to English.

### Rule 2: write each word in its own script
Write Hindi words in Devanagari and English words in Latin script, inside the
same line. This gives the best pronunciation for both languages.

- Never romanize Hindi (`koshish karte rehna` ✗ → `कोशिश करते रहना` ✓).
- Never write English words in Devanagari (`इम्पॉर्टेंट` ✗ → `important` ✓).
- Verified example that sounds right:
  `कोशिश करते रहना, वक़्त ज़रूर बदलेगा, आज नहीं तो कल तुम्हारा होगा। i wanted to say this for so long. कोशिश करते रहना is so important`

The same rules apply to fully English voiceovers: write them in English,
and still send language Hindi.

### Calling the API
```bash
curl -s http://127.0.0.1:3900/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"input":"<script>","voice":"adi","language":"hi","response_format":"wav"}' \
  --output voiceover.wav
```
- `/v1/audio/speech` (OpenAI-compatible) takes at most 4096 characters per request.
- For full-length scripts, use `POST /generate` (form fields `text`, `profile_id`,
  `language=hi`). It splits the text at sentence boundaries and crossfades the parts.
- WAV output is the safe default; mp3 depends on the app's bundled ffmpeg.

### Emotion and delivery
- The clone copies the energy of the reference clip. To get a different mood,
  record a new short clip in that mood and save it as its own profile
  (for example `adi-hype`, `adi-calm`).
- Tags that work: `[laughter]`, `[sigh]`, `[pause 500ms]`. Punctuation
  (`...`, `!`, dashes) shapes the pacing.
- Do not use ElevenLabs-style tags such as `[excited]` or `[whispers]`:
  OmniVoice does not know them and reads them out loud.

### New voice profiles
Always send the transcript (`ref_text`) with the reference clip, and keep the
clip under 15 s. This Mac has no PyTorch Whisper model, so a profile without a
transcript fails with "Automatic reference transcription needs an installed
speech-to-text model".

### Licence caveat
OmniVoice's model weights are CC-BY-NC (non-commercial). Before using these
voiceovers on monetized content (ads, sponsorships, client work), switch to an
engine labelled Apache-2.0 (VoxCPM2 supports Hindi) or check the licence terms.

## Accounts
- **elixir build** (upcoming): will need a lot of English voiceovers. Use the
  rules above.
