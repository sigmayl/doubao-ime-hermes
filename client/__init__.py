"""Vendored Doubao IME realtime protocol client (Python standard library only).

Copied from ``scripts/doubao_ime_asr.py`` in fcitx5-vinput: the protocol code is
verbatim, the command-line front end is dropped. Re-run that script's vendoring
step when the protocol changes.
"""

from .doubao_ime import (
    APP_KEY,
    DEFAULT_ENDPOINT,
    FRAME_BYTES,
    REGISTER_URL,
    SAMPLE_RATE,
    ProtocolError,
    RecognitionResult,
    RecognitionUpdate,
    SessionConfig,
    TranscriptState,
    UsageError,
    WebSocketClient,
    WebSocketRequest,
    WebSocketResponse,
    decode_response,
    ensure_identity,
    load_pcm,
    parse_update,
    remove_trailing_punctuation,
    resolve_converter,
    transcribe,
)

__all__ = [
    "APP_KEY",
    "DEFAULT_ENDPOINT",
    "FRAME_BYTES",
    "REGISTER_URL",
    "SAMPLE_RATE",
    "ProtocolError",
    "RecognitionResult",
    "RecognitionUpdate",
    "SessionConfig",
    "TranscriptState",
    "UsageError",
    "WebSocketClient",
    "WebSocketRequest",
    "WebSocketResponse",
    "decode_response",
    "ensure_identity",
    "load_pcm",
    "parse_update",
    "remove_trailing_punctuation",
    "resolve_converter",
    "transcribe",
]
