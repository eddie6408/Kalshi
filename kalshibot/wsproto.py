"""A small WebSocket client, on the standard library only.

Why not a library: the bot installs itself on a Mac by double-clicking a
`.command` file, and every dependency is one more thing that can fail to
install on a machine nobody can inspect. A feed that silently never connects
because `pip` had a bad day is worse than no feed, because it looks fine.

So this implements the part of RFC 6455 a market-data client actually needs:

    the opening handshake, with the Sec-WebSocket-Accept check
    client-to-server frames, masked, as the RFC requires
    server-to-client frames, including fragmented ones
    ping/pong, so the connection is not dropped for being quiet
    the closing handshake

It does NOT implement extensions, compression, or subprotocol negotiation.
None of them are needed here, and each would be another thing to get wrong.

This module knows nothing about Kalshi. It moves text frames.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
from urllib.parse import urlparse

#: RFC 6455 section 1.3. Concatenated with our key, hashed, and echoed back by
#: the server. Checking it is what proves we are talking to a WebSocket and
#: not, say, a proxy that cheerfully returned 200 to everything.
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY = 0x0, 0x1, 0x2
OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA


class WebSocketError(RuntimeError):
    pass


class WebSocketClosed(WebSocketError):
    """The peer closed the connection. Expected; not a fault."""


def _mask(payload: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % 4] for i, b in enumerate(payload))


class WebSocket:
    """One connection. Not thread-safe for sending; the feed owns it."""

    def __init__(self, sock, max_message_bytes: int = 8 * 1024 * 1024):
        self.sock = sock
        self.max_message_bytes = max_message_bytes
        self.closed = False
        self._buf = b""

    # ------------------------------------------------------------------
    @classmethod
    def connect(cls, url: str, headers: dict[str, str] | None = None,
                timeout: float = 15.0) -> "WebSocket":
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketError(f"not a websocket url: {url!r}")
        secure = parsed.scheme == "wss"
        host = parsed.hostname
        if not host:
            raise WebSocketError(f"no host in {url!r}")
        port = parsed.port or (443 if secure else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        raw = socket.create_connection((host, port), timeout=timeout)
        if secure:
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=host)

        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}" if parsed.port else f"Host: {host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        for name, value in (headers or {}).items():
            lines.append(f"{name}: {value}")
        raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        ws = cls(raw)
        ws._read_handshake(key)
        raw.settimeout(timeout)
        return ws

    def _read_handshake(self, key: str) -> None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during the handshake")
            data += chunk
            if len(data) > 64 * 1024:
                raise WebSocketError("handshake response is implausibly large")
        head, _, rest = data.partition(b"\r\n\r\n")
        self._buf = rest                      # frames may already be in here
        text = head.decode("latin-1")
        status_line, *header_lines = text.split("\r\n")
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or parts[1] != "101":
            raise WebSocketError(f"server refused the upgrade: {status_line}")

        got = ""
        for line in header_lines:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                got = value.strip()
        want = base64.b64encode(
            hashlib.sha1((key + GUID).encode()).digest()).decode()
        if got != want:
            raise WebSocketError(
                "Sec-WebSocket-Accept did not match; this is not a WebSocket "
                "server, or something is sitting in between")

    # ------------------------------------------------------------------
    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode())

    def ping(self, payload: bytes = b"") -> None:
        self._send_frame(OP_PING, payload)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise WebSocketClosed("socket is closed")
        header = bytearray()
        header.append(0x80 | opcode)          # FIN set; we never fragment
        n = len(payload)
        # The mask bit (0x80) is mandatory for a CLIENT. Servers reject
        # unmasked client frames, which is a confusing failure to debug.
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        key = os.urandom(4)
        header += key
        try:
            self.sock.sendall(bytes(header) + _mask(payload, key))
        except OSError as exc:
            self.closed = True
            raise WebSocketClosed(f"send failed: {exc}") from exc

    # ------------------------------------------------------------------
    def recv(self) -> str | None:
        """Next complete TEXT message, or None on a timeout.

        Control frames are handled here and never returned: a ping is answered,
        a close ends the connection.
        """
        chunks: list[bytes] = []
        expecting_continuation = False
        while True:
            try:
                fin, opcode, payload = self._read_frame()
            except socket.timeout:
                return None

            if opcode == OP_CLOSE:
                self._handle_close(payload)
                raise WebSocketClosed("peer closed the connection")
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue

            if opcode in (OP_TEXT, OP_BINARY):
                if expecting_continuation:
                    raise WebSocketError("new message began mid-fragment")
                chunks = [payload]
                expecting_continuation = not fin
            elif opcode == OP_CONT:
                if not expecting_continuation:
                    raise WebSocketError("continuation frame with nothing to continue")
                chunks.append(payload)
                expecting_continuation = not fin
            else:
                raise WebSocketError(f"unknown opcode {opcode}")

            total = sum(len(c) for c in chunks)
            if total > self.max_message_bytes:
                raise WebSocketError(f"message over {self.max_message_bytes} bytes")
            if not expecting_continuation:
                return b"".join(chunks).decode("utf-8", "replace")

    def _read_frame(self) -> tuple[bool, int, bytes]:
        head = self._read_exactly(2)
        b0, b1 = head[0], head[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exactly(8))[0]
        if length > self.max_message_bytes:
            raise WebSocketError(f"frame claims {length} bytes")
        key = self._read_exactly(4) if masked else b""
        payload = self._read_exactly(length) if length else b""
        if masked:
            payload = _mask(payload, key)
        return fin, opcode, payload

    def _read_exactly(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                self.closed = True
                raise WebSocketClosed("connection closed by peer")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    # ------------------------------------------------------------------
    def _handle_close(self, payload: bytes) -> None:
        if not self.closed:
            try:
                self._send_frame(OP_CLOSE, payload[:2] or b"\x03\xe8")
            except Exception:  # noqa: BLE001
                pass
        self.closed = True
        self._shutdown()

    def close(self, code: int = 1000) -> None:
        if not self.closed:
            try:
                self._send_frame(OP_CLOSE, struct.pack("!H", code))
            except Exception:  # noqa: BLE001
                pass
        self.closed = True
        self._shutdown()

    def _shutdown(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
