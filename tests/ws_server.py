"""A tiny WebSocket server for the tests.

Deliberately does NOT import kalshibot.wsproto. Frames are built and parsed by
hand here, so a framing bug in the client cannot be cancelled out by the same
bug in the thing testing it.
"""
from __future__ import annotations

import base64
import hashlib
import socket
import struct
import threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def server_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    """Server frames are NOT masked."""
    out = bytearray([(0x80 if fin else 0x00) | opcode])
    n = len(payload)
    if n < 126:
        out.append(n)
    elif n < (1 << 16):
        out.append(126)
        out += struct.pack("!H", n)
    else:
        out.append(127)
        out += struct.pack("!Q", n)
    return bytes(out) + payload


def text_frame(text: str, fin: bool = True, opcode: int = 0x1) -> bytes:
    return server_frame(opcode, text.encode(), fin)


class MiniWSServer:
    """Accepts one client, replays a script, records what it was sent."""

    def __init__(self, script=None, *, bad_accept=False, refuse=False,
                 close_after=False):
        self.script = list(script or [])
        self.bad_accept = bad_accept
        self.refuse = refuse
        self.close_after = close_after
        self.received: list[str] = []
        self.headers: dict[str, str] = {}
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.ready = threading.Event()
        self.done = threading.Event()
        self._conn = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/trade-api/ws/v2"

    def start(self) -> "MiniWSServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.done.set()
        for s in (self._conn, self.sock):
            try:
                s and s.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    def _run(self) -> None:
        try:
            self.sock.settimeout(10)
            conn, _ = self.sock.accept()
            self._conn = conn
            conn.settimeout(10)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            head = data.split(b"\r\n\r\n")[0].decode("latin-1")
            key = ""
            for line in head.split("\r\n")[1:]:
                name, _, value = line.partition(":")
                self.headers[name.strip().lower()] = value.strip()
                if name.strip().lower() == "sec-websocket-key":
                    key = value.strip()

            if self.refuse:
                conn.sendall(b"HTTP/1.1 401 Unauthorized\r\n\r\n")
                return

            accept = base64.b64encode(
                hashlib.sha1((key + GUID).encode()).digest()).decode()
            if self.bad_accept:
                accept = "obviously-wrong"
            conn.sendall(
                ("HTTP/1.1 101 Switching Protocols\r\n"
                 "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                 f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())

            for frame in self.script:
                conn.sendall(frame)

            self.ready.set()
            buf = b""
            while not self.done.is_set():
                try:
                    chunk = conn.recv(4096)
                except (socket.timeout, OSError):
                    break
                if not chunk:
                    break
                buf += chunk
                buf = self._drain(conn, buf)
            if self.close_after:
                try:
                    conn.sendall(server_frame(0x8, struct.pack("!H", 1000)))
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.ready.set()

    def _drain(self, conn, buf: bytes) -> bytes:
        while len(buf) >= 2:
            b1 = buf[1]
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            i = 2
            if length == 126:
                if len(buf) < 4:
                    return buf
                length = struct.unpack("!H", buf[2:4])[0]
                i = 4
            elif length == 127:
                if len(buf) < 10:
                    return buf
                length = struct.unpack("!Q", buf[2:10])[0]
                i = 10
            key = b""
            if masked:
                if len(buf) < i + 4:
                    return buf
                key = buf[i:i + 4]
                i += 4
            if len(buf) < i + length:
                return buf
            payload = buf[i:i + length]
            if masked:
                payload = bytes(b ^ key[j % 4] for j, b in enumerate(payload))
            opcode = buf[0] & 0x0F
            buf = buf[i + length:]

            if opcode == 0x1:
                self.received.append(payload.decode())
            elif opcode == 0x9:                     # ping -> pong
                try:
                    conn.sendall(server_frame(0xA, payload))
                except OSError:
                    pass
            elif opcode == 0x8:
                self.done.set()
        return buf

    def send(self, frame: bytes) -> None:
        if self._conn:
            self._conn.sendall(frame)
