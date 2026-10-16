"""Doubao IME (豆包输入法) speech recognition plugin for Hermes Agent.

Registers the ``doubao-ime`` speech-to-text backend — so voice messages route
through the same realtime endpoint the Doubao input method uses — plus two
tools, a ``/doubao`` slash command, a ``hermes doubao-ime`` CLI subcommand, and
a bundled ``doubao-voice`` skill.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import schemas, tools
from .stt import DISPLAY_NAME, DoubaoIMEProvider

logger = logging.getLogger(__name__)

TOOLSET = "doubao-ime"
SKILL_PATH = Path(__file__).parent / "skills" / "doubao-voice" / "SKILL.md"


def register(ctx) -> None:
    """Wire the plugin into Hermes. Called once at startup."""
    ctx.register_tool(
        name=schemas.TRANSCRIBE["name"],
        toolset=TOOLSET,
        schema=schemas.TRANSCRIBE,
        handler=tools.transcribe,
        description="Transcribe an audio file with Doubao IME speech recognition",
        emoji="🎙️",
    )
    ctx.register_tool(
        name=schemas.VOICE_STATUS["name"],
        toolset=TOOLSET,
        schema=schemas.VOICE_STATUS,
        handler=tools.voice_status,
        description="Report Doubao IME speech recognition readiness",
        emoji="🔌",
    )
    ctx.register_transcription_provider(DoubaoIMEProvider())
    ctx.register_command(
        "doubao",
        _slash_transcribe,
        description="Transcribe an audio file with Doubao IME",
        args_hint="<audio-path>",
    )
    ctx.register_cli_command(
        "doubao-ime",
        help="Transcribe audio with Doubao IME (豆包输入法) speech recognition",
        setup_fn=_setup_cli,
        handler_fn=_run_cli,
        description=(
            "Speaks the same realtime protocol as the Doubao input method. Audio is "
            "decoded to 16 kHz mono PCM (ffmpeg or sox for non-PCM input) and the "
            "anonymous device identity is cached under the plugin data directory."
        ),
    )
    if SKILL_PATH.is_file():
        ctx.register_skill(
            "doubao-voice",
            SKILL_PATH,
            description="Transcribe voice notes and audio files with Doubao IME",
        )
    logger.debug("%s registered", DISPLAY_NAME)


def _slash_transcribe(raw_args: str) -> str:
    """Implement ``/doubao <audio-path>``."""
    audio_path = (raw_args or "").strip()
    if not audio_path:
        return "usage: /doubao <audio-path>"
    payload = json.loads(tools.transcribe({"audio_path": audio_path}))
    if payload.get("error"):
        return f"Doubao IME: {payload['error']}"
    return payload.get("transcript", "")


def _setup_cli(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("audio", nargs="?", help="audio file to transcribe")
    parser.add_argument("--json", action="store_true", help="print the full JSON result")
    parser.add_argument(
        "--strip-trailing-punctuation",
        action="store_true",
        help="drop the closing punctuation of the transcript",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="report readiness (identity, decoder, endpoint) instead of transcribing",
    )


def _run_cli(args: argparse.Namespace) -> int:
    if args.status:
        print(tools.voice_status({}))
        return 0
    if not args.audio:
        print("error: an audio file is required (or use --status)", file=sys.stderr)
        return 2

    payload = json.loads(
        tools.transcribe(
            {
                "audio_path": args.audio,
                "strip_trailing_punctuation": args.strip_trailing_punctuation,
            }
        )
    )
    if payload.get("error"):
        print(f"error: {payload['error']}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload["transcript"])
    return 0
