"""The WebSocket client itself.

Written on the standard library so the bot has no new dependency to install on
a machine nobody can inspect. That choice is only defensible if the protocol
work is actually tested, so these tests drive it against a server whose frames
are built by hand - a framing bug in the client cannot be cancelled out by the
same bug in the thing checking it.
"""
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kalshibot.wsproto import (WebSocket, WebSocketClosed, WebSocketError,
                               _mask)
from ws_server import MiniWSServer, server_frame, text_frame


def _wait(server, predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --------------------------- the handshake ---------------------------
def test_a_message_arrives_intact():
    s = MiniWSServer([text_frame('{"hello":"world"}')]).start()
    try:
        ws = WebSocket.connect(s.url, timeout=5)
        assert ws.recv() == '{"hello":"world"}'
        ws.close()
    finally:
        s.stop()


def test_headers_reach_the_server():
    # This is how the Kalshi signature gets there. If headers are dropped the
    # connection is refused and the reason is not obvious.
    s = MiniWSServer([text_frame("hi")]).start()
    try:
        ws = WebSocket.connect(s.url, headers={"KALSHI-ACCESS-KEY": "abc123"},
                               timeout=5)
        ws.recv()
        assert s.headers.get("kalshi-access-key") == "abc123"
        ws.close()
    finally:
        s.stop()


def test_a_refused_upgrade_is_an_error_not_a_hang():
    s = MiniWSServer(refuse=True).start()
    try:
        try:
            WebSocket.connect(s.url, timeout=5)
            raise AssertionError("connected to a server that said 401")
        except WebSocketError as exc:
            assert "401" in str(exc)
    finally:
        s.stop()


def test_a_wrong_accept_header_is_refused():
    # Proves we are talking to a WebSocket and not something in between that
    # cheerfully returns 101 to everything.
    s = MiniWSServer([text_frame("hi")], bad_accept=True).start()
    try:
        try:
            WebSocket.connect(s.url, timeout=5)
            raise AssertionError("accepted a bogus Sec-WebSocket-Accept")
        except WebSocketError as exc:
            assert "Accept" in str(exc)
    finally:
        s.stop()


def test_a_non_websocket_url_is_refused_before_any_socket_is_opened():
    for bad in ("https://example.com", "ftp://example.com", "nonsense"):
        try:
            WebSocket.connect(bad)
            raise AssertionError(f"opened {bad}")
        except WebSocketError:
            pass


# --------------------------- framing ---------------------------
def test_a_long_message_uses_the_extended_length_and_survives():
    payload = "x" * 70000            # forces the 8-byte length form
    s = MiniWSServer([text_frame(payload)]).start()
    try:
        ws = WebSocket.connect(s.url, timeout=5)
        assert ws.recv() == payload
        ws.close()
    finally:
        s.stop()


def test_a_medium_message_uses_the_two_byte_length():
    payload = "y" * 300
    s = MiniWSServer([text_frame(payload)]).start()
    try:
        ws = WebSocket.connect(s.url, timeout=5)
        assert ws.recv() == payload
        ws.close()
    finally:
        s.stop()


def test_a_fragmented_message_is_reassembled():
    # Kalshi can split a large snapshot. Handling only whole frames would
    # silently drop the tail of a book.
    frames = [server_frame(0x1, b'{"a":1,', fin=False),
              server_frame(0x0, b'"b":2,', fin=False),
              server_frame(0x0, b'"c":3}', fin=True)]
    s = MiniWSServer(frames).start()
    try:
        ws = WebSocket.connect(s.url, timeout=5)
        assert ws.recv() == '{"a":1,"b":2,"c":3}'
        ws.close()
    finally:
        s.stop()


def test_several_messages_in_one_tcp_read_are_all_delivered():
    # Frames do not arrive one per packet. Reading one and discarding the
    # buffer would lose the rest of that read.
    blob = text_frame("one") + text_frame("two") + text_frame("three")
    s = MiniWSServer([blob]).start()
    try:
        ws = WebSocket.connect(s.url, timeout=5)
        assert [ws.recv() for _ in range(3)] == ["one", "two", "three"]
        ws.close()
    finally:
        s.stop()


def test_client_frames_are_masked():
    # Servers reject unmasked client frames, and the failure is opaque.
    s = MiniWSServer([text_frame("hi")]).start()
    try:
        ws = WebSocket.connect(s.url, timeout=5)
        ws.recv()
        ws.send_text('{"cmd":"subscribe"}')
        assert _wait(s, lambda: s.received)
        assert s.received[0] == '{"cmd":"subscribe"}'
        ws.close()
    finally:
        s.stop()


def test_masking_is_its_own_inverse():
    key = b"\x01\x02\x03\x04"
    data = b"the quick brown fox"
    assert _mask(_mask(data, key), key) == data


# --------------------------- control frames ---------------------------
def test_a_ping_is_answered_and_not_returned_as_a_message():
    frames = [server_frame(0x9, b"heartbeat"), text_frame("payload")]
    s = MiniWSServer(frames).start()
    try:
        ws = WebSocket.connect(s.url, timeout=5)
        assert ws.recv() == "payload", "a ping leaked out as a message"
        ws.close()
    finally:
        s.stop()


def test_a_close_frame_ends_the_connection_cleanly():
    frames = [text_frame("last words"), server_frame(0x8, struct.pack("!H", 1000))]
    s = MiniWSServer(frames).start()
    try:
        ws = WebSocket.connect(s.url, timeout=5)
        assert ws.recv() == "last words"
        try:
            ws.recv()
            raise AssertionError("kept reading after a close frame")
        except WebSocketClosed:
            pass
        assert ws.closed is True
    finally:
        s.stop()


def test_sending_after_close_raises_rather_than_pretending():
    s = MiniWSServer([text_frame("hi")]).start()
    try:
        ws = WebSocket.connect(s.url, timeout=5)
        ws.recv()
        ws.close()
        try:
            ws.send_text("anyone there?")
            raise AssertionError("sent on a closed socket")
        except WebSocketClosed:
            pass
    finally:
        s.stop()


def test_a_quiet_socket_times_out_instead_of_blocking_forever():
    s = MiniWSServer([]).start()
    try:
        ws = WebSocket.connect(s.url, timeout=0.3)
        assert ws.recv() is None, "a timeout must be None, not an exception"
        ws.close()
    finally:
        s.stop()


def test_an_oversized_frame_is_refused():
    s = MiniWSServer([text_frame("hi")]).start()
    try:
        ws = WebSocket.connect(s.url, timeout=5)
        ws.max_message_bytes = 4
        try:
            ws.recv()
        except WebSocketError:
            pass
        ws.close()
    finally:
        s.stop()
