# Doubao IME → Hermes Agent plugin

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that
transcribes speech with the Doubao IME (豆包输入法) realtime protocol — the same
unofficial protocol the `provider.doubaoime.streaming` script in
[vinput-registry](https://github.com/xifan2333/vinput-registry) and the
`integrations/doubao-ime-worker` Worker in
[fcitx5-vinput](https://github.com/xifan2333/fcitx5-vinput) speak.

It registers the `doubao-ime` speech-to-text backend, so voice messages arriving
on Telegram, Discord, Slack, WhatsApp, and Signal are transcribed automatically,
plus two tools for transcribing files on demand. No API key: the endpoint
authenticates with an anonymous device identity that the plugin registers once
and caches.

## Install

```bash
# From this repository
hermes plugins install sigmayl/doubao-ime-hermes

# Or from a local checkout
cp -r . ~/.hermes/plugins/doubao-ime
```

Enable it and point speech-to-text at it:

```bash
hermes plugins enable doubao-ime
```

```yaml
# ~/.hermes/config.yaml
stt:
  provider: doubao-ime
```

Nothing else is required. `ffmpeg` or `sox` on `PATH` extends the accepted
formats; without either, 16 kHz mono WAV and raw PCM still work.

## What it adds

| Surface | Name | Purpose |
| --- | --- | --- |
| STT backend | `doubao-ime` | Transcribes incoming voice messages (`stt.provider: doubao-ime`) |
| Tool | `doubao_transcribe` | Transcribes a local audio file: `audio_path`, `strip_trailing_punctuation` |
| Tool | `doubao_voice_status` | Readiness report: cached identity, decoder, endpoint; `refresh_identity` re-registers |
| Slash command | `/doubao <audio-path>` | Same as the tool, from the chat prompt |
| CLI | `hermes doubao-ime <audio>` | `--json`, `--strip-trailing-punctuation`, `--status` |
| Skill | `doubao-ime:doubao-voice` | Tells the agent when and how to use the tools |

The plugin also implements the live path (`streaming_capable`), so surfaces that
opt into `stt.streaming: true` can feed microphone PCM while the user speaks and
read partial transcripts as they arrive.

## Formats

16 kHz mono signed 16-bit WAV and raw PCM (`.pcm`, `.raw`, `.s16le`) are read
directly. Everything else — mp3, m4a, ogg/opus voice notes, flac, and WAV at any
other sample rate or channel count — is decoded through `ffmpeg` or `sox`.

## Limits and caveats

- **Real-time pacing.** Audio is streamed at playback speed, so a 60 s recording
  takes about 60 s. This is a property of the protocol, not a choice: sending the
  same audio as fast as the socket accepts it truncated a 56 s test sample to its
  first sentence, while playback-speed sending transcribed all five repetitions.
  Expect long files to hold the tool call open.
- **The audio leaves the machine.** Recordings are uploaded to Doubao's endpoint
  (`frontier-audio-ime-ws.doubao.com`) with an anonymous device identity. Say so
  before transcribing private audio.
- **No timestamps.** The protocol returns text per VAD segment without times, so
  `segments` holds text only.
- **No language hint.** The endpoint detects the language itself; `language` and
  `prompt` are ignored rather than forwarded.
- **Unofficial protocol.** Doubao IME is a mobile app protocol and can change
  without notice; a session that starts failing usually means the endpoint or the
  app version constants moved.
- **Registration is rate-limited by nothing but the endpoint.** `refresh_identity`
  discards the cached device id and registers a new one; there is no reason to use
  it unless the endpoint starts rejecting the old identity.

## Layout

```
.
├── plugin.yaml            # manifest: name, version, declared tools
├── __init__.py            # register(ctx): tools, STT backend, command, CLI, skill
├── stt.py                 # DoubaoIMEProvider + the live streaming session
├── schemas.py             # tool schemas the model reads
├── tools.py               # tool handlers
├── state.py               # plugin data directory (cached device identity)
├── client/
│   ├── __init__.py
│   └── doubao_ime.py      # vendored protocol client, standard library only
└── skills/doubao-voice/SKILL.md
```

`client/doubao_ime.py` is the protocol half of `scripts/doubao_ime_asr.py` from
[fcitx5-vinput](https://github.com/xifan2333/fcitx5-vinput), copied verbatim with
the command-line front end removed: identity registration (ttEncrypt), the
WebSocket client, the Protobuf framing, and the blocking one-shot session driver.
Update both together when the protocol changes.

## Verifying it works

```bash
hermes plugins list | grep doubao          # enabled, version 0.1.0
hermes doubao-ime --status                 # identity, decoder, endpoint
hermes doubao-ime recording.wav            # transcript on stdout
```

Inside a session, `doubao_voice_status` answers the same readiness question, and
a transcript that comes back empty means the endpoint heard no speech rather than
that the plugin is broken.

## License

GPL-3.0, matching [fcitx5-vinput](https://github.com/xifan2333/fcitx5-vinput),
whose `scripts/doubao_ime_asr.py` the vendored protocol client comes from.
