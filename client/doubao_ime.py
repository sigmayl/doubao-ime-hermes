#!/usr/bin/env python3
"""Doubao IME (豆包输入法) realtime speech recognition, as a library.

Vendored from ``scripts/doubao_ime_asr.py`` in fcitx5-vinput (protocol code
unchanged, command-line front end removed) so the Hermes plugin stays
dependency-free and standalone. Only the Python standard library is used;
decoding non-PCM audio needs ``ffmpeg`` or ``sox`` on PATH.

  1. An anonymous device identity is registered once (ttEncrypt request to the
     app-log endpoint) and cached, because the realtime socket authenticates
     with ``device_id`` alone.
  2. A WebSocket session runs ``StartTask`` -> ``StartSession`` -> 16 kHz mono
     PCM sent as 10 ms ``TaskRequest`` frames -> ``FinishSession``.
  3. Recognition updates arrive as binary Protobuf messages carrying JSON
     payloads; text inside one VAD segment is replaced, finished segments are
     appended.

Entry points: :func:`ensure_identity`, :class:`SessionConfig`,
:func:`transcribe` for one-shot file or buffer recognition, and
:class:`WebSocketClient` plus :func:`parse_update` for the live sessions in
``stt.py``.
"""

from __future__ import annotations

import base64
import fcntl
import gzip
import hashlib
import json
import logging
import os
import secrets
import select
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
import wave
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
APP_KEY = "OrnqKvSSrs"
DEFAULT_ENDPOINT = "wss://frontier-audio-ime-ws.doubao.com/ocean/api/v1/ws"
REGISTER_URL = "https://log.snssdk.com/service/2/device_register/"
DEFAULT_IDENTITY = Path.home() / ".cache/vinput/doubaoime/credentials.json"
IDENTITY_FIELDS = ("device_id", "install_id", "cdid", "openudid", "clientudid")
SAMPLE_RATE = 16000
FRAME_MS = 10
FRAME_BYTES = SAMPLE_RATE * 2 * FRAME_MS // 1000
RAW_SUFFIXES = {".pcm", ".raw", ".s16le"}
# Closing punctuation is dropped locally only when it is not worth keeping.
PRESERVED_PUNCTUATION = frozenset("]})>\u300b\u201d'\"\u3011\u2019\uff09%\u2103:\uff1a")

logger = logging.getLogger(__name__)


class UsageError(Exception):
    """Caller-side mistake: bad arguments, unreadable audio, bad identity."""


class ProtocolError(RuntimeError):
    """The peer or the protocol itself failed."""


# --------------------------------------------------------------------------- #
# Anonymous device identity (ttEncrypt registration)
# --------------------------------------------------------------------------- #


def _gf_mul(left: int, right: int) -> int:
    result = 0
    while right:
        if right & 1:
            result ^= left
        left = ((left << 1) ^ (0x11B if left & 0x80 else 0)) & 0xFF
        right >>= 1
    return result


def _gf_pow(value: int, exponent: int) -> int:
    result = 1
    while exponent:
        if exponent & 1:
            result = _gf_mul(result, value)
        value = _gf_mul(value, value)
        exponent >>= 1
    return result


def _sbox(value: int) -> int:
    inverse = 0 if value == 0 else _gf_pow(value, 254)
    result = inverse ^ ((inverse << 1) | (inverse >> 7)) ^ ((inverse << 2) | (inverse >> 6))
    result ^= ((inverse << 3) | (inverse >> 5)) ^ ((inverse << 4) | (inverse >> 4)) ^ 0x63
    return result & 0xFF


def _expand_key(key: bytes) -> list[list[int]]:
    if len(key) not in (16, 24, 32):
        raise ProtocolError("AES key must be 16, 24, or 32 bytes")
    nk = len(key) // 4
    rounds = nk + 6
    words = [list(key[index : index + 4]) for index in range(0, len(key), 4)]
    rcon = 1
    while len(words) < 4 * (rounds + 1):
        word = words[-1][:]
        if len(words) % nk == 0:
            word = [_sbox(item) for item in word[1:] + word[:1]]
            word[0] ^= rcon
            rcon = _gf_mul(rcon, 2)
        elif nk > 6 and len(words) % nk == 4:
            word = [_sbox(item) for item in word]
        word = [word[index] ^ words[-nk][index] for index in range(4)]
        words.append(word)
    return [
        [byte for word in words[index : index + 4] for byte in word]
        for index in range(0, len(words), 4)
    ]


def _aes_block(block: bytes, key: bytes) -> bytes:
    rounds = len(key) // 4 + 6
    round_keys = _expand_key(key)
    state = list(block)

    def add_round_key(round_key: list[int]) -> None:
        for index in range(16):
            state[index] ^= round_key[index]

    def substitute() -> None:
        for index in range(16):
            state[index] = _sbox(state[index])

    def shift_rows() -> None:
        original = state[:]
        for row in range(4):
            for column in range(4):
                state[4 * column + row] = original[4 * ((column + row) % 4) + row]

    def mix_columns() -> None:
        for column in range(4):
            offset = column * 4
            a = state[offset : offset + 4]
            state[offset] = _gf_mul(a[0], 2) ^ _gf_mul(a[1], 3) ^ a[2] ^ a[3]
            state[offset + 1] = a[0] ^ _gf_mul(a[1], 2) ^ _gf_mul(a[2], 3) ^ a[3]
            state[offset + 2] = a[0] ^ a[1] ^ _gf_mul(a[2], 2) ^ _gf_mul(a[3], 3)
            state[offset + 3] = _gf_mul(a[0], 3) ^ a[1] ^ a[2] ^ _gf_mul(a[3], 2)

    add_round_key(round_keys[0])
    for round_index in range(1, rounds):
        substitute()
        shift_rows()
        mix_columns()
        add_round_key(round_keys[round_index])
    substitute()
    shift_rows()
    add_round_key(round_keys[rounds])
    return bytes(state)


def _aes_cbc_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    if len(iv) != 16 or len(data) % 16:
        raise ProtocolError("AES-CBC requires a 16-byte IV and block-aligned data")
    result = bytearray()
    previous = iv
    for offset in range(0, len(data), 16):
        block = bytes(
            left ^ right for left, right in zip(data[offset : offset + 16], previous, strict=True)
        )
        previous = _aes_block(block, key)
        result.extend(previous)
    return bytes(result)


def encode_tt_encrypt(gzip_bytes: bytes, salt: bytes | None = None) -> bytes:
    """Wrap a gzipped body the way the app-log endpoint expects it."""
    salt = secrets.token_bytes(32) if salt is None else salt
    if len(salt) != 32:
        raise ProtocolError("ttEncrypt salt must contain exactly 32 bytes")
    constant = bytes.fromhex(
        "4dd4c2e6b83162090e52b3c7a6733ba41cb2462b829ab58a196b39db57177524"
        "f49baf7f08e8d68d26a72e37c1a95a2f1f05a51892aef2949732b62a38aadd58"
    )
    digest = hashlib.sha512(hashlib.sha512(salt).digest() + constant).digest()
    key, iv = digest[:16], digest[16:32]
    payload = hashlib.sha512(gzip_bytes).digest() + gzip_bytes
    padding = 16 - len(payload) % 16
    payload += bytes([padding]) * padding
    return bytes.fromhex("746305100000") + salt + _aes_cbc_encrypt(payload, key, iv)


def new_installation() -> dict:
    return {
        "openudid": secrets.token_hex(10),
        "clientudid": str(uuid.uuid4()),
        "cdid": str(uuid.uuid4()),
        "first_install_time": int(time.time() * 1000),
    }


def registration_request(state: dict, url: str = REGISTER_URL) -> dict:
    request_id = str(uuid.uuid4())
    header = {
        "aid": 401734,
        "app_name": "oime",
        "channel": "official",
        "package": "com.bytedance.android.doubaoime",
        "app_version": "1.5.1",
        "app_version_minor": "6",
        "version_code": 100501006,
        "update_version_code": 100501006,
        "manifest_version_code": 100501006,
        "not_request_sender": 1,
        "release_build": "",
        "custom": {},
        "device_platform": "android",
        "os": "Android",
        "os_version": "14",
        "os_api": 34,
        "device_model": "Python protocol test",
        "device_brand": "generic",
        "device_manufacturer": "generic",
        "cpu_abi": "arm64-v8a",
        "language": "zh",
        "timezone": 8,
        "tz_name": "Asia/Shanghai",
        "tz_offset": 28800,
        "access": "wifi",
        "openudid": state["openudid"],
        "clientudid": state["clientudid"],
        "cdid": state["cdid"],
        "git_hash": "b416399",
        "sdk_version_code": 4030263,
        "sdk_target_version": 30,
        "sdk_version": "4.3.2-rc.13",
        "sdk_flavor": "cnInner",
        "guest_mode": 0,
        "req_id": request_id,
        "apk_first_install_time": state["first_install_time"],
        "is_system_app": 0,
    }
    body = {"magic_tag": "ss_app_log", "_gen_time": int(time.time() * 1000), "header": header}
    return {
        "url": url,
        "params": {"req_id": request_id, "tt_data": "a"},
        "headers": {"Content-Type": "application/octet-stream;tt-data=a"},
        "body": body,
    }


def register_device(request: dict, timeout: float) -> tuple[dict, dict]:
    raw = json.dumps(request["body"], ensure_ascii=False, separators=(",", ":")).encode()
    content = encode_tt_encrypt(gzip.compress(raw, mtime=0))
    target = request["url"] + "?" + urlencode(request["params"])
    with urlopen(
        Request(target, data=content, headers=request["headers"]), timeout=timeout
    ) as response:
        body = json.loads(response.read())
    did = str(body.get("device_id_str") or body.get("device_id") or "")
    iid = str(body.get("install_id_str") or body.get("install_id") or "")
    if not did or not iid or did == "0" or iid == "0":
        raise ProtocolError("registration did not return nonzero device and installation IDs")
    return {"device_id": did, "install_id": iid}, body


def save_identity(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(state, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ensure_identity(path: Path, register_url: str = REGISTER_URL, timeout: float = 20.0) -> dict:
    """Return a cached anonymous identity, registering a device when needed."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        os.fchmod(lock.fileno(), 0o600)
        deadline = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ProtocolError("identity lock deadline expired") from None
                time.sleep(0.05)

        state = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else new_installation()
        )
        if not isinstance(state, dict):
            raise ProtocolError("identity cache must be a JSON object")
        if state.get("device_id") or state.get("install_id"):
            if not all(state.get(key) and str(state[key]) != "0" for key in IDENTITY_FIELDS):
                raise ProtocolError("identity cache holds an incomplete or zero identity")
            path.chmod(0o600)
            return state
        if not all(
            state.get(key) for key in ("cdid", "openudid", "clientudid", "first_install_time")
        ):
            raise ProtocolError("identity cache holds an incomplete installation")
        # Persist the installation first so a retry keeps the same identifiers.
        save_identity(path, state)
        logger.debug("registering an anonymous Doubao IME device (%s)", path)
        identifiers, _ = register_device(registration_request(state, register_url), timeout)
        state.update(identifiers)
        save_identity(path, state)
        return state


# --------------------------------------------------------------------------- #
# Protobuf messages carried inside WebSocket binary frames
# --------------------------------------------------------------------------- #


def encode_varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        to_write = value & 0x7F
        value >>= 7
        if value:
            out.append(to_write | 0x80)
        else:
            out.append(to_write)
            return bytes(out)


def decode_varint(data: bytes, index: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while True:
        if index >= len(data):
            raise ProtocolError("unexpected end of protobuf varint")
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, index
        shift += 7
        if shift >= 64:
            raise ProtocolError("protobuf varint is too long")


def encode_field_bytes(field_number: int, value: bytes) -> bytes:
    return encode_varint((field_number << 3) | 2) + encode_varint(len(value)) + value


def encode_field_string(field_number: int, value: str) -> bytes:
    return encode_field_bytes(field_number, value.encode("utf-8"))


def encode_field_int(field_number: int, value: int) -> bytes:
    return encode_varint((field_number << 3) | 0) + encode_varint(value)


def parse_protobuf_fields(data: bytes) -> dict[int, Any]:
    fields: dict[int, Any] = {}
    index = 0
    while index < len(data):
        key, index = decode_varint(data, index)
        field_number = key >> 3
        wire_type = key & 0x07
        if wire_type == 0:
            value, index = decode_varint(data, index)
        elif wire_type == 1:
            if index + 8 > len(data):
                raise ProtocolError("unexpected end of fixed64 field")
            value = data[index : index + 8]
            index += 8
        elif wire_type == 2:
            length, index = decode_varint(data, index)
            if index + length > len(data):
                raise ProtocolError("unexpected end of length-delimited field")
            value = data[index : index + length]
            index += length
        elif wire_type == 5:
            if index + 4 > len(data):
                raise ProtocolError("unexpected end of fixed32 field")
            value = data[index : index + 4]
            index += 4
        else:
            raise ProtocolError(f"unsupported protobuf wire type: {wire_type}")

        if field_number in fields:
            existing = fields[field_number]
            if isinstance(existing, list):
                existing.append(value)
            else:
                fields[field_number] = [existing, value]
        else:
            fields[field_number] = value
    return fields


def get_proto_string(fields: dict[int, Any], field_number: int) -> str:
    value = fields.get(field_number, b"")
    if isinstance(value, list):
        value = value[-1]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, int):
        return str(value)
    return ""


def get_proto_int(fields: dict[int, Any], field_number: int) -> int:
    value = fields.get(field_number, 0)
    if isinstance(value, list):
        value = value[-1]
    return value if isinstance(value, int) else 0


@dataclass
class WebSocketRequest:
    namespace: str = ""
    event: str = ""
    task_id: str = ""
    appkey: str = ""
    payload: str = ""
    audio: bytes = b""
    seq_id: int = 0

    def SerializeToString(self) -> bytes:
        result = b""
        for number, value in (
            (2, self.appkey),
            (3, self.namespace),
            (5, self.event),
            (6, self.payload),
            (8, self.task_id),
        ):
            if value:
                result += encode_field_string(number, value)
        if self.audio:
            result += encode_field_bytes(7, self.audio)
        if self.seq_id:
            result += encode_field_int(9, self.seq_id)
        return result


@dataclass
class WebSocketResponse:
    task_id: str = ""
    event: str = ""
    status_code: int = 0
    status_text: str = ""
    payload: str = ""
    seq_id: int = 0

    @classmethod
    def FromString(cls, data: bytes) -> WebSocketResponse:
        fields = parse_protobuf_fields(data)
        return cls(
            get_proto_string(fields, 1),
            get_proto_string(fields, 4),
            get_proto_int(fields, 5),
            get_proto_string(fields, 6),
            get_proto_string(fields, 7),
            get_proto_int(fields, 9),
        )


# --------------------------------------------------------------------------- #
# Minimal WebSocket client (RFC 6455 client side, binary frames only)
# --------------------------------------------------------------------------- #


class WebSocketClient:
    def __init__(self, url: str, headers: dict[str, str], timeout: float) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"ws", "wss"}:
            raise UsageError("the endpoint must use ws:// or wss://")
        if not parsed.hostname:
            raise UsageError("the endpoint is missing a hostname")

        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.scheme = parsed.scheme
        self.timeout = timeout
        self.headers = headers
        self._recv_buffer = b""
        self._closed = False
        self.socket = self._connect()

    def _connect(self) -> socket.socket:
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        raw.settimeout(self.timeout)
        sock = raw
        try:
            if self.scheme == "wss":
                sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)

            key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
            lines = [
                f"GET {self.path} HTTP/1.1",
                f"Host: {self.host}:{self.port}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
            ]
            for name, value in self.headers.items():
                lines.append(f"{name}: {value}")
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))

            response, overflow = self._read_http_response(sock)
            self._recv_buffer = overflow
            self._validate_handshake(response, key)
            return sock
        except BaseException:
            sock.close()
            raise

    def _read_http_response(self, sock: socket.socket) -> tuple[bytes, bytes]:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise ProtocolError("WebSocket handshake failed: empty response")
            data.extend(chunk)
            if len(data) > 65536:
                raise ProtocolError("WebSocket handshake failed: response too large")
        response, overflow = bytes(data).split(b"\r\n\r\n", 1)
        return response, overflow

    def _validate_handshake(self, response: bytes, key: str) -> None:
        lines = response.decode("utf-8", errors="replace").split("\r\n")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()

        if not lines or "101" not in lines[0]:
            status_line = lines[0] if lines else "invalid response"
            details = [f"WebSocket handshake failed: {status_line}"]
            log_id = headers.get("x-tt-logid")
            if log_id:
                details.append(f"log_id={log_id}")
            connect_id = headers.get("x-api-connect-id")
            if connect_id:
                details.append(f"connect_id={connect_id}")
            if " 403" in f" {status_line} ":
                details.append("Doubao IME rejected the anonymous WebSocket handshake")
            raise ProtocolError(", ".join(details))

        accept = headers.get("sec-websocket-accept")
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode("utf-8")).digest()).decode(
            "ascii"
        )
        if accept != expected:
            raise ProtocolError("WebSocket handshake failed: invalid Sec-WebSocket-Accept header")

    def close(self) -> None:
        self._closed = True
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()

    def send_binary(self, payload: bytes) -> None:
        try:
            self._send_frame(0x2, payload)
        except OSError as error:
            raise ConnectionError(f"the server closed the WebSocket: {error}") from error

    def recv_message(self, timeout: float | None = None) -> tuple[int, bytes] | None:
        """Return (opcode, payload), or None when nothing arrived before the timeout."""
        fragments = bytearray()
        current_opcode: int | None = None

        while True:
            frame = self._recv_frame(timeout if current_opcode is None else None)
            if frame is None:
                return None

            opcode, payload, fin = frame
            if opcode == 0x8:
                self._closed = True
                raise ConnectionError("the server closed the WebSocket")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode not in {0x0, 0x1, 0x2}:
                continue

            if opcode in {0x1, 0x2}:
                current_opcode = opcode
                fragments = bytearray(payload)
            else:
                if current_opcode is None:
                    continue
                fragments.extend(payload)

            if len(fragments) > 4 * 1024 * 1024:
                raise ProtocolError("WebSocket message exceeds 4 MB")
            if not fin:
                continue
            return current_opcode, bytes(fragments)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            raise ConnectionError("the WebSocket connection is closed")

        first = 0x80 | (opcode & 0x0F)
        mask_key = secrets.token_bytes(4)
        length = len(payload)

        header = bytearray([first])
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))

        masked = bytes(payload[i] ^ mask_key[i % 4] for i in range(length))
        self.socket.sendall(bytes(header) + mask_key + masked)

    def _recv_frame(self, timeout: float | None) -> tuple[int, bytes, bool] | None:
        if not self._recv_buffer and timeout is not None:
            if not select.select([self.socket], [], [], timeout)[0]:
                return None

        header = self._recv_exact(2)
        if header is None:
            raise ConnectionError("the server closed the WebSocket")

        first, second = header
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F

        if length == 126:
            raw_length = self._recv_exact(2)
            if raw_length is None:
                raise ConnectionError("the server closed the WebSocket")
            length = struct.unpack("!H", raw_length)[0]
        elif length == 127:
            raw_length = self._recv_exact(8)
            if raw_length is None:
                raise ConnectionError("the server closed the WebSocket")
            length = struct.unpack("!Q", raw_length)[0]

        if length > 4 * 1024 * 1024:
            raise ProtocolError("WebSocket frame exceeds 4 MB")

        mask_key = b""
        if masked:
            mask_key = self._recv_exact(4) or b""

        payload = self._recv_exact(length)
        if payload is None:
            raise ConnectionError("the server closed the WebSocket")
        if masked:
            payload = bytes(payload[i] ^ mask_key[i % 4] for i in range(length))
        return opcode, payload, fin

    def _recv_exact(self, size: int) -> bytes | None:
        while len(self._recv_buffer) < size:
            chunk = self.socket.recv(4096)
            if not chunk:
                if not self._recv_buffer and size > 0:
                    return None
                raise ProtocolError("WebSocket connection closed mid-frame")
            self._recv_buffer += chunk

        data = self._recv_buffer[:size]
        self._recv_buffer = self._recv_buffer[size:]
        return data


# --------------------------------------------------------------------------- #
# Session driver
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SessionConfig:
    endpoint: str = DEFAULT_ENDPOINT
    appkey: str = APP_KEY
    device_id: str = ""
    install_id: str = ""
    open_timeout: float = 4.0
    response_timeout: float = 10.0
    finish_grace: float = 9.0

    def __post_init__(self) -> None:
        if not self.device_id or self.device_id == "0":
            raise UsageError("a nonzero device_id is required")
        if not self.appkey:
            raise UsageError("the ASR application key is empty")
        if self.finish_grace <= 0:
            raise UsageError("finish grace must be greater than zero")


@dataclass(frozen=True)
class RecognitionUpdate:
    text: str
    vad_finished: bool
    stream_finished: bool
    sequence: int
    payload: dict


@dataclass(frozen=True)
class RecognitionResult:
    text: str
    task_id: str
    updates: tuple[RecognitionUpdate, ...]


class TranscriptState:
    """Append completed VAD segments; replace hypotheses inside one segment."""

    def __init__(self) -> None:
        self.committed = ""
        self.text = ""
        self.current = ""
        self.finished = False

    def apply(self, text: str, finished: bool) -> str | None:
        # Drop empty interim hypotheses and duplicate segment finals.
        if (not text and not finished) or (finished and self.finished and text == self.current):
            return None
        self.text = self.committed + text
        self.current = text
        self.finished = finished
        if finished:
            self.committed = self.text
        return self.text


def decode_response(data: bytes) -> WebSocketResponse:
    try:
        response = WebSocketResponse.FromString(data)
    except ProtocolError as error:
        raise ProtocolError("invalid ASR Protobuf response") from error
    if response.event in {"SessionFailed", "TaskFailed"}:
        raise ProtocolError(f"{response.event}: {response.status_code} {response.status_text}")
    return response


def parse_update(response: WebSocketResponse) -> RecognitionUpdate | None:
    if not response.payload:
        return None
    body = json.loads(response.payload)
    if not isinstance(body, dict):
        raise ProtocolError("ASR payload must be a JSON object")
    results = body.get("results", [])
    if results is None:
        return None
    if not isinstance(results, list):
        raise ProtocolError("ASR results must be an array")
    if (
        not results
        or not isinstance(results[0], dict)
        or not isinstance(results[0].get("text"), str)
    ):
        return None
    extra = body.get("extra")
    if extra is None:
        extra = {}
    if not isinstance(extra, dict):
        raise ProtocolError("ASR recognition extra must be an object")
    result = results[0]
    return RecognitionUpdate(
        result["text"],
        bool(result.get("is_vad_finished")),
        bool(result.get("stream_asr_finish")),
        extra.get("seq_id", response.seq_id),
        body,
    )


def transcribe(
    pcm: bytes,
    config: SessionConfig,
    on_update: Callable[[RecognitionUpdate], None] | None = None,
    pacing: bool = True,
) -> RecognitionResult:
    """Stream 16 kHz mono PCM through one Doubao IME session and return its text."""
    if len(pcm) % 2:
        raise UsageError("PCM data must contain complete 16-bit samples")
    if not pcm:
        raise UsageError("audio is empty")

    task_id = str(uuid.uuid4())
    query = {"device_id": config.device_id, "aid": "401734"}
    if config.install_id:
        query["iid"] = config.install_id
    separator = "&" if "?" in config.endpoint else "?"
    url = config.endpoint + separator + urlencode(query)
    headers = {"proto-version": "v2", "sdk-version": "2", "x-custom-keepalive": "true"}

    client = WebSocketClient(url, headers, config.open_timeout)

    def send(
        event: str, *, payload: dict | None = None, audio: bytes = b"", sequence: int = 0
    ) -> None:
        request = WebSocketRequest(
            namespace="ASR",
            event=event,
            task_id=task_id,
            appkey=config.appkey if event != "TaskRequest" else "",
            payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if payload is not None
            else "",
            audio=audio,
            seq_id=sequence,
        )
        client.send_binary(request.SerializeToString())

    def receive(timeout: float | None) -> WebSocketResponse | None:
        while True:
            message = client.recv_message(timeout)
            if message is None:
                return None
            opcode, data = message
            if opcode != 0x2:
                raise ProtocolError("expected a binary WebSocket message")
            response = decode_response(data)
            if response.task_id and response.task_id != task_id:
                continue
            if response.event in {"Ping", "Pong"}:
                continue
            return response

    def wait_for(event: str, seconds: float) -> WebSocketResponse:
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError(f"timed out waiting for {event}")
            response = receive(remaining)
            if response is None:
                raise ProtocolError(f"timed out waiting for {event}")
            if response.event == event:
                return response

    transcript = TranscriptState()
    updates: list[RecognitionUpdate] = []

    def handle(response: WebSocketResponse) -> None:
        update = parse_update(response)
        if update is None:
            return
        text = transcript.apply(update.text, update.vad_finished)
        if text is None:
            return
        update = replace(update, text=text)
        updates.append(update)
        if on_update is not None:
            on_update(update)

    terminal = ""
    try:
        send("StartTask")
        wait_for("TaskStarted", config.response_timeout)

        extra = {
            "enable_punctuation": True,
            "enable_asr_twopass": True,
            "enable_asr_threepass": True,
            "end_smooth_window_ms": 800,
            "did": config.device_id,
            "app_name": "oime",
            "app_version": "1.5.1",
        }
        send(
            "StartSession",
            payload={
                "audio_info": {"sample_rate": SAMPLE_RATE, "channel": 1, "format": "pcm"},
                "extra": extra,
            },
        )
        wait_for("SessionStarted", config.response_timeout)

        # Send one 10 ms frame per step, then collect whatever the server answered.
        began = time.monotonic()
        sequence = 0
        for offset in range(0, len(pcm), FRAME_BYTES):
            sequence += 1
            send(
                "TaskRequest",
                audio=pcm[offset : offset + FRAME_BYTES].ljust(FRAME_BYTES, b"\0"),
                sequence=sequence,
                payload={"extra": {}, "timestamp_ms": int(time.time() * 1000)},
            )
            while True:
                response = receive(0.0)
                if response is None:
                    break
                handle(response)
                if response.event in {"SessionFinished", "TaskFinished"}:
                    terminal = response.event
                    break
            if terminal:
                logger.debug(
                    "the server ended the session after %d of %d audio frames",
                    sequence,
                    -(-len(pcm) // FRAME_BYTES),
                )
                break
            if pacing:
                target = began + (offset + FRAME_BYTES) / (SAMPLE_RATE * 2)
                delay = target - time.monotonic()
                if delay > 0:
                    time.sleep(delay)

        if not terminal:
            send("FinishSession")
            deadline = time.monotonic() + config.finish_grace
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProtocolError("the session did not finish before the deadline")
                response = receive(remaining)
                if response is None:
                    raise ProtocolError("the session did not finish before the deadline")
                handle(response)
                if response.event in {"SessionFinished", "TaskFinished"}:
                    terminal = response.event
                    break
        if terminal != "TaskFinished":
            send("FinishTask")
        return RecognitionResult(transcript.text, task_id, tuple(updates))
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# Audio input and text output
# --------------------------------------------------------------------------- #


def remove_trailing_punctuation(text: str) -> str:
    """Drop closing punctuation, keeping brackets, quotes, and inner punctuation."""
    end = len(text)
    while (
        end
        and text[end - 1] not in PRESERVED_PUNCTUATION
        and unicodedata.category(text[end - 1]).startswith("P")
    ):
        end -= 1
    return text[:end]


def resolve_converter(converter: str) -> str | None:
    if converter:
        found = shutil.which(converter) or (converter if Path(converter).is_file() else None)
        if found is None:
            raise UsageError(f"decoder not found: {converter}")
        return found
    return shutil.which("ffmpeg") or shutil.which("sox")


def _read_wav(path: Path) -> bytes | None:
    """Return frames when the WAV already matches 16 kHz mono 16-bit."""
    try:
        with wave.open(str(path), "rb") as handle:
            if (
                handle.getnchannels() != 1
                or handle.getsampwidth() != 2
                or handle.getframerate() != SAMPLE_RATE
            ):
                return None
            return handle.readframes(handle.getnframes())
    except (wave.Error, EOFError):
        return None


def _convert(path: Path, converter: str) -> bytes:
    tool = resolve_converter(converter)
    if tool is None:
        raise UsageError(
            f"cannot decode {path.suffix or 'this file'} to {SAMPLE_RATE} Hz mono PCM: "
            "install ffmpeg or sox, or pass an already-converted .pcm file"
        )
    name = Path(tool).name
    if name.startswith("sox"):
        options = ["-t", "raw", "-r", str(SAMPLE_RATE), "-c", "1", "-e", "signed", "-b", "16", "-"]
        command = [tool, str(path), *options]
    else:
        options = ["-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]
        command = [tool, "-nostdin", "-v", "error", "-i", str(path), *options]
    logger.debug("decoding %s with %s", path.name, name)
    completed = subprocess.run(command, capture_output=True, check=False)
    if completed.returncode != 0:
        lines = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ProtocolError(f"{name} failed on {path.name}: {lines[-1] if lines else 'no output'}")
    return completed.stdout


def load_pcm(source: str, converter: str = "") -> bytes:
    """Read 16 kHz mono signed 16-bit little-endian PCM from a file, URL-free path, or stdin."""
    if source == "-":
        data, label = sys.stdin.buffer.read(), "<stdin>"
    else:
        path = Path(source)
        if not path.is_file():
            raise UsageError(f"audio file not found: {source}")
        label = str(path)
        suffix = path.suffix.lower()
        if suffix in RAW_SUFFIXES:
            data = path.read_bytes()
        elif suffix == ".wav":
            data = _read_wav(path)
            if data is None:
                data = _convert(path, converter)
        else:
            data = _convert(path, converter)

    if not data:
        raise UsageError(f"no audio samples read from {label}")
    if len(data) % 2:
        raise UsageError(f"PCM data from {label} must contain complete 16-bit samples")
    return data
