"""Tool schemas — what the model reads when deciding to call these tools."""

TRANSCRIBE = {
    "name": "doubao_transcribe",
    "description": (
        "Transcribe a local audio file to text with Doubao IME (豆包输入法) realtime "
        "speech recognition. Supports Chinese and English, mixed within one "
        "recording. Reads 16 kHz mono WAV and raw PCM directly; other formats "
        "(mp3, m4a, ogg/opus, flac) need ffmpeg or sox on PATH. Use it for voice "
        "notes, meeting recordings, and any audio the user points at. No API key "
        "is needed, but the audio is uploaded to Doubao's endpoint."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "audio_path": {
                "type": "string",
                "description": "Path to the audio file to transcribe, e.g. '~/notes/talk.m4a'",
            },
            "strip_trailing_punctuation": {
                "type": "boolean",
                "description": (
                    "Drop the closing punctuation of the final transcript (default false: keep it)"
                ),
            },
        },
        "required": ["audio_path"],
    },
}

VOICE_STATUS = {
    "name": "doubao_voice_status",
    "description": (
        "Report whether Doubao IME speech recognition is ready: the cached "
        "anonymous device identity, the audio decoder found on PATH, and the "
        "endpoint in use. Contacts the endpoint to register a device identity "
        "when none is cached yet, or when refresh_identity is true. Use it when "
        "transcription fails and the cause is unclear."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "refresh_identity": {
                "type": "boolean",
                "description": (
                    "Discard the cached device identity and register a new one (default false)"
                ),
            },
        },
        "required": [],
    },
}
