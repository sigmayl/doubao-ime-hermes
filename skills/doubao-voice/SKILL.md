---
name: doubao-voice
description: Transcribe voice notes, meeting recordings, and other audio files with Doubao IME (豆包输入法) realtime speech recognition. Use when the user points at an audio file, asks what a recording says, or wants a voice message turned into text.
---

# Doubao IME voice transcription

Transcribe local audio with the Doubao IME realtime endpoint — the same
recognition the Doubao input method uses. Chinese and English, including mixed
speech in one recording, with no API key.

## When to use it

- The user points at an audio file and asks what it says.
- A voice note, call recording, or meeting audio needs text.
- `doubao_transcribe` is the tool; `/doubao <path>` is the same thing from the
  chat prompt.

## How to run it

1. Call `doubao_transcribe` with `audio_path`. Relative paths resolve against
   the working directory; `~` expands.
2. Read `transcript` from the JSON result. `segments` holds the per-VAD-segment
   text when you need to split or time-align it, and `duration_s` is the audio
   length.
3. On `error`, call `doubao_voice_status` to check readiness before retrying:
   it reports the cached device identity, the audio decoder on PATH, and the
   endpoint.

## What to know

- Formats: 16 kHz mono WAV and raw PCM (`.pcm`, `.raw`, `.s16le`) are read
  directly. mp3, m4a, ogg/opus, and flac need `ffmpeg` or `sox` on PATH; without
  one of them, transcription fails with a decoding error naming the format.
- The recording is sent to Doubao's servers. Say so before transcribing private
  audio, and prefer another backend when the user wants the audio to stay local.
- Transcription runs at playback speed, so a long recording takes about as long
  as it plays. Warn the user before starting anything longer than a few minutes.
- The endpoint decides the language itself; there is no language hint to pass.
- Voice messages already sent through a connected chat platform are transcribed
  automatically by this backend when `stt.provider` is `doubao-ime`; there is no
  need to call the tool for those.
