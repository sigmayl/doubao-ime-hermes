"""Tool handlers.

Handlers return a JSON string, always — failures included — and accept
``**kwargs`` so Hermes can hand them more context over time.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import state
from .client import doubao_ime as ime
from .stt import DISPLAY_NAME, PROVIDER_NAME, DoubaoIMEProvider

logger = logging.getLogger(__name__)


def transcribe(args: dict, **kwargs: Any) -> str:
    """Transcribe one audio file."""
    audio_path = str(args.get("audio_path") or "").strip()
    if not audio_path:
        return json.dumps({"error": "audio_path is required"})

    path = Path(audio_path).expanduser()
    if not path.is_file():
        return json.dumps({"error": f"audio file not found: {path}"})

    result = DoubaoIMEProvider().transcribe(str(path))
    if not result.get("success"):
        return json.dumps(
            {"audio_path": str(path), "error": result.get("error", "transcription failed")},
            ensure_ascii=False,
        )

    transcript = result["transcript"]
    if args.get("strip_trailing_punctuation"):
        transcript = ime.remove_trailing_punctuation(transcript)

    return json.dumps(
        {
            "audio_path": str(path),
            "transcript": transcript,
            "segments": result.get("segments", []),
            "duration_s": result.get("duration_s"),
        },
        ensure_ascii=False,
    )


def voice_status(args: dict, **kwargs: Any) -> str:
    """Report readiness, registering a device identity when needed."""
    identity_file = state.identity_path()
    identity: dict[str, Any] = {}
    error = ""

    if args.get("refresh_identity") and identity_file.exists():
        identity_file.unlink()
    try:
        identity = ime.ensure_identity(identity_file)
    except (ime.UsageError, ime.ProtocolError, OSError) as failure:
        error = str(failure)

    decoder = ime.resolve_converter("")
    return json.dumps(
        {
            "provider": PROVIDER_NAME,
            "display_name": DISPLAY_NAME,
            "endpoint": ime.DEFAULT_ENDPOINT,
            "identity_file": str(identity_file),
            "device_id": identity.get("device_id", ""),
            "registered": bool(identity),
            "error": error,
            "audio_decoder": decoder or "none — install ffmpeg or sox for non-WAV input",
            "config": {"stt.provider": PROVIDER_NAME},
        },
        ensure_ascii=False,
    )
