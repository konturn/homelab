"""Minimal Home Assistant websocket client, Python standard library only.

    with HassWS("http://172.20.0.5:8123", token) as ha:
        states = ha.call("get_states")
        ha.call("call_service", domain="zwave_js", service="ping",
                target={"entity_id": "button.x_ping"})

One request at a time: call() sends a message and returns its result,
skipping events and other ids. Raises HassError when HA answers
success: false. Shared by ci/ha-maintenance; written so other jobs can
import it (no HA-specific state).
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import urllib.parse
from typing import Any, Optional


class HassError(RuntimeError):
    def __init__(self, msg_type: str, error: dict):
        super().__init__(f"{msg_type}: {error.get('code')}: {error.get('message')}")
        self.code = error.get("code")


class HassWS:
    def __init__(self, base_url: str, token: str, timeout: float = 60):
        u = urllib.parse.urlparse(base_url)
        if u.scheme != "http":
            raise ValueError("only plain http to the container address is supported")
        self.host, self.port = u.hostname, u.port or 8123
        self.token, self.timeout = token, timeout
        self.sock: Optional[socket.socket] = None
        self.buf = b""
        self.next_id = 0

    # -- connection -----------------------------------------------------------------
    def __enter__(self) -> "HassWS":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /api/websocket HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake: connection closed")
            head += chunk
        head, self.buf = head.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("websocket handshake refused: " + head.split(b"\r\n", 1)[0].decode(errors="replace"))
        if self.recv().get("type") != "auth_required":
            raise ConnectionError("unexpected first message")
        self.send({"type": "auth", "access_token": self.token})
        r = self.recv()
        if r.get("type") != "auth_ok":
            raise PermissionError("Home Assistant refused the token")

    def close(self) -> None:
        if self.sock:
            try:
                self._send_frame(0x8, b"")
            except OSError:
                pass
            self.sock.close()
            self.sock = None

    # -- framing --------------------------------------------------------------------
    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + masked)

    def send(self, msg: dict) -> None:
        self._send_frame(0x1, json.dumps(msg).encode())

    def recv(self) -> dict:
        data = b""
        while True:
            b1, b2 = self._read(2)
            fin, opcode, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:                       # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:
                raise ConnectionError("websocket closed by Home Assistant")
            if opcode in (0x1, 0x2, 0x0):
                data += payload
                if fin:
                    return json.loads(data)

    # -- API ------------------------------------------------------------------------
    def call(self, msg_type: str, **fields: Any) -> Any:
        self.next_id += 1
        mid = self.next_id
        self.send(dict(fields, id=mid, type=msg_type))
        while True:
            r = self.recv()
            if r.get("id") != mid or r.get("type") != "result":
                continue
            if not r.get("success"):
                raise HassError(msg_type, r.get("error") or {})
            return r.get("result")
