"""Doubao IME speech-to-text backend: the plugin's Hermes STT provider.

Two paths share one protocol client:

* :meth:`DoubaoIMEProvider.transcribe` — one audio file in, transcript out. This
  is the path voice messages from Telegram, Discord, Slack, WhatsApp and Signal
  take, and what ``transcribe_audio()`` dispatches to when ``stt.provider`` is
  ``doubao-ime``.
* :class:`DoubaoIMEStreamSession` — live 16 kHz PCM while the user speaks, for
  the surfaces that opt into ``stt.streaming``.

The endpoint authenticates with an anonymous device ``device_id`` only, so the
plugin needs no API key: the identity is registered on first use and cached in
the plugin's data directory.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlencode

from agent.transcription_provider import TranscriptionProvider

from . import state
from .client import doubao_ime as ime

logger = logging.getLogger(__name__)

PROVIDER_NAME = "doubao-ime"
DISPLAY_NAME = "Doubao IME (豆包输入法)"
MAX_AUDIO_SECONDS = 300
# The worker always finishes within the protocol's finish grace, so a wait
# longer than that means it is wedged on the socket.
FINALIZE_TIMEOUT_SECONDS = 25.0
_HEADERS = {"proto-version": "v2", "sdk-version": "2", "x-custom-keepalive": "true"}

try:  # Hermes without the live-streaming surface (0.19.x and older)
    from agent.transcription_provider import TranscriptionStreamSession as _StreamSessionBase
except ImportError:  # pragma: no cover - depends on the host release

    class _StreamSessionBase:  # type: ignore[no-redef]
        """Stand-in base for releases that never open a live session."""


class DoubaoIMEProvider(TranscriptionProvider):
    """Transcribe with the Doubao IME realtime endpoint."""

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def display_name(self) -> str:
        return DISPLAY_NAME

    def is_available(self) -> bool:
        """Always true: the device identity is anonymous and needs no setup.

        No network call happens here — registration is lazy, on the first
        transcription, because this runs on every picker paint.
        """
        return True

    def default_model(self) -> str:
        return PROVIDER_NAME

    def list_models(self) -> list[dict[str, Any]]:
        return [
            {
                "id": PROVIDER_NAME,
                "display": "Doubao IME realtime",
                "languages": ["zh", "en"],
                "max_audio_seconds": MAX_AUDIO_SECONDS,
            }
        ]

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": DISPLAY_NAME,
            "badge": "no key",
            "tag": "Anonymous device identity — audio is sent to Doubao",
            "env_vars": [],
        }

    @property
    def streaming_capable(self) -> bool:
        return True

    def open_stream_session(self, *, language: str | None = None, prompt: str | None = None) -> Any:
        return DoubaoIMEStreamSession(language=language)

    def transcribe(
        self,
        file_path: str,
        *,
        model: str | None = None,
        language: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Transcribe ``file_path``; ``language`` and ``prompt`` are ignored.

        The endpoint picks the language itself and takes no vocabulary hint.
        """
        try:
            pcm = ime.load_pcm(str(file_path))
        except ime.UsageError as error:
            return _failure(f"cannot read audio: {error}")
        except ime.ProtocolError as error:
            return _failure(f"cannot decode audio: {error}")

        try:
            result = ime.transcribe(pcm, session_config())
        except ime.UsageError as error:
            return _failure(str(error))
        except ime.ProtocolError as error:
            return _failure(f"Doubao IME session failed: {error}")
        except OSError as error:
            return _failure(f"Doubao IME endpoint unreachable: {error}")

        return _envelope(result.text, result.updates, len(pcm))


class DoubaoIMEStreamSession(_StreamSessionBase):  # type: ignore[misc]
    """One live utterance over the Doubao IME realtime socket.

    ``push_audio`` hands 16 kHz mono s16le PCM to a worker thread that owns the
    socket, so a capture callback never blocks on the network.
    """

    provider = PROVIDER_NAME

    def __init__(self, *, language: str | None = None) -> None:
        self._language = language or ""
        self._audio: queue.Queue[bytes | None] = queue.Queue()
        self._transcript = ime.TranscriptState()
        self._lock = threading.Lock()
        self._partial = ""
        self._result: dict[str, Any] | None = None
        self._done = threading.Event()
        self._worker = threading.Thread(target=self._run, name="doubao-ime-stream", daemon=True)
        self._worker.start()

    # -- caller API ---------------------------------------------------------

    def push_audio(self, chunk: bytes) -> None:
        if chunk and not self._done.is_set():
            self._audio.put(bytes(chunk))

    def partial_transcript(self) -> str:
        with self._lock:
            return self._partial

    def finalize(self) -> dict[str, Any]:
        self._audio.put(None)
        if not self._done.wait(FINALIZE_TIMEOUT_SECONDS):
            logger.warning("Doubao IME live session did not finish; abandoning it")
            return _failure("Doubao IME live session timed out")
        return self._result or _failure("Doubao IME live session produced no result")

    # -- worker -------------------------------------------------------------

    def _run(self) -> None:
        client = None
        try:
            config = session_config()
            client = ime.WebSocketClient(_endpoint_url(config), _HEADERS, config.open_timeout)
            live = _LiveSession(client, config, str(uuid.uuid4()), self._transcript, self._publish)
            live.start()
            self._pump(live)
            if not live.terminal:
                live.finish()
            self._result = _envelope(self._transcript.text, live.updates, 0)
        except Exception as error:
            logger.debug("Doubao IME live session failed", exc_info=True)
            self._result = _failure(f"Doubao IME live session failed: {error}")
        finally:
            if client is not None:
                client.close()
            self._done.set()

    def _pump(self, live: _LiveSession) -> None:
        """Send queued audio as 10 ms frames until the stream ends."""
        pending = bytearray()
        sequence = 0
        while True:
            chunk = self._audio.get()
            if chunk is None:
                break
            pending.extend(chunk)
            while len(pending) >= ime.FRAME_BYTES and not live.terminal:
                sequence += 1
                live.send_audio(bytes(pending[: ime.FRAME_BYTES]), sequence)
                del pending[: ime.FRAME_BYTES]
                live.drain()
            if live.terminal:
                return
        if pending:
            sequence += 1
            live.send_audio(bytes(pending).ljust(ime.FRAME_BYTES, b"\0"), sequence)
            live.drain()

    def _publish(self, text: str) -> None:
        with self._lock:
            self._partial = text


class _LiveSession:
    """Protocol plumbing for one live utterance, mirroring the client's driver."""

    def __init__(
        self,
        client: ime.WebSocketClient,
        config: ime.SessionConfig,
        task_id: str,
        transcript: ime.TranscriptState,
        on_text: Any,
    ) -> None:
        self.client = client
        self.config = config
        self.task_id = task_id
        self.transcript = transcript
        self.on_text = on_text
        self.terminal = False
        self.terminal_event = ""
        self.updates: list[ime.RecognitionUpdate] = []

    def start(self) -> None:
        self.send("StartTask")
        self.wait_for("TaskStarted", self.config.response_timeout)
        self.send(
            "StartSession",
            payload={
                "audio_info": {"sample_rate": ime.SAMPLE_RATE, "channel": 1, "format": "pcm"},
                "extra": {
                    "enable_punctuation": True,
                    "enable_asr_twopass": True,
                    "enable_asr_threepass": True,
                    "end_smooth_window_ms": 800,
                    "did": self.config.device_id,
                    "app_name": "oime",
                    "app_version": "1.5.1",
                },
            },
        )
        self.wait_for("SessionStarted", self.config.response_timeout)

    def send_audio(self, frame: bytes, sequence: int) -> None:
        self.send(
            "TaskRequest",
            audio=frame,
            sequence=sequence,
            payload={"extra": {}, "timestamp_ms": int(time.time() * 1000)},
        )

    def drain(self) -> None:
        """Apply every response that already arrived."""
        while True:
            response = self.receive(0.0)
            if response is None:
                return
            self.handle(response)

    def finish(self) -> None:
        self.send("FinishSession")
        deadline = time.monotonic() + self.config.finish_grace
        while not self.terminal:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ime.ProtocolError("the session did not finish before the deadline")
            response = self.receive(remaining)
            if response is None:
                raise ime.ProtocolError("the session did not finish before the deadline")
            self.handle(response)
        if self.terminal_event != "TaskFinished":
            self.send("FinishTask")

    # -- protocol -----------------------------------------------------------

    def send(
        self,
        event: str,
        *,
        payload: dict | None = None,
        audio: bytes = b"",
        sequence: int = 0,
    ) -> None:
        request = ime.WebSocketRequest(
            namespace="ASR",
            event=event,
            task_id=self.task_id,
            appkey=self.config.appkey if event != "TaskRequest" else "",
            payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if payload is not None
            else "",
            audio=audio,
            seq_id=sequence,
        )
        self.client.send_binary(request.SerializeToString())

    def receive(self, timeout: float | None) -> ime.WebSocketResponse | None:
        while True:
            message = self.client.recv_message(timeout)
            if message is None:
                return None
            opcode, data = message
            if opcode != 0x2:
                raise ime.ProtocolError("expected a binary WebSocket message")
            response = ime.decode_response(data)
            if response.task_id and response.task_id != self.task_id:
                continue
            if response.event in {"Ping", "Pong"}:
                continue
            return response

    def wait_for(self, event: str, seconds: float) -> ime.WebSocketResponse:
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ime.ProtocolError(f"timed out waiting for {event}")
            response = self.receive(remaining)
            if response is None:
                raise ime.ProtocolError(f"timed out waiting for {event}")
            if response.event == event:
                return response

    def handle(self, response: ime.WebSocketResponse) -> None:
        if response.event in {"SessionFinished", "TaskFinished"}:
            self.terminal = True
            self.terminal_event = response.event
        update = ime.parse_update(response)
        if update is None:
            return
        text = self.transcript.apply(update.text, update.vad_finished)
        if text is not None:
            self.updates.append(update)
            self.on_text(text)


def session_config() -> ime.SessionConfig:
    """Build a session config from the cached (or freshly registered) identity."""
    identity = ime.ensure_identity(state.identity_path())
    return ime.SessionConfig(
        device_id=str(identity["device_id"]),
        install_id=str(identity.get("install_id", "")),
    )


def _endpoint_url(config: ime.SessionConfig) -> str:
    query = {"device_id": config.device_id, "aid": "401734"}
    if config.install_id:
        query["iid"] = config.install_id
    separator = "&" if "?" in config.endpoint else "?"
    return config.endpoint + separator + urlencode(query)


def _envelope(text: str, updates: Any, pcm_bytes: int) -> dict[str, Any]:
    transcript = text.strip()
    if not transcript:
        return _failure("no speech was recognized")
    return {
        "success": True,
        "transcript": transcript,
        "provider": PROVIDER_NAME,
        "model": PROVIDER_NAME,
        "duration_s": round(pcm_bytes / 2 / ime.SAMPLE_RATE, 2) if pcm_bytes else None,
        "segments": [update.text for update in updates],
    }


def _failure(message: str) -> dict[str, Any]:
    return {"success": False, "transcript": "", "provider": PROVIDER_NAME, "error": message}
