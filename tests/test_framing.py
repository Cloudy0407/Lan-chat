"""Unit tests for TCP framing"""

import socket

import pytest

from framing import (
    ByeFrame,
    ConnectionClosedError,
    FileHeaderFrame,
    HiFrame,
    MsgFrame,
    ProtocolError,
    encode_bye,
    encode_file_header,
    encode_hi,
    encode_msg,
    recv_chat_frame,
    recv_exact,
    recv_file_header,
    recv_hi,
    sanitize_filename,
)


class _SlowSocket:
    """Fake socket that returns at most max_chunk bytes per recv() call"""

    def __init__(self, data: bytes, max_chunk: int = 2) -> None:
        self._data = data
        self._pos = 0
        self._max_chunk = max_chunk

    def recv(self, bufsize: int) -> bytes:
        if self._pos >= len(self._data):
            return b""
        n = min(bufsize, self._max_chunk, len(self._data) - self._pos)
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk


def test_hi_roundtrip() -> None:
    s1, s2 = socket.socketpair()
    with s1, s2:
        s1.sendall(encode_hi("Nguyen Van A", 5000, "chat"))
        assert recv_hi(s2) == HiFrame(name="Nguyen Van A", tcp_port=5000, purpose="chat")


def test_msg_utf8_byte_length() -> None:
    text = "Chào A, sẵn sàng?"
    raw = encode_msg(text)
    assert raw.startswith(b"MSG|21|")

    sock = _SlowSocket(raw, max_chunk=2)
    assert recv_chat_frame(sock) == MsgFrame(text=text)


def test_merged_and_split_frames() -> None:
    stream = encode_msg("Tin 1") + encode_msg("Tin số 2 có dấu") + encode_bye()

    sock = _SlowSocket(stream, max_chunk=3)
    assert recv_chat_frame(sock) == MsgFrame(text="Tin 1")
    assert recv_chat_frame(sock) == MsgFrame(text="Tin số 2 có dấu")
    assert recv_chat_frame(sock) == ByeFrame()


def test_file_header_and_raw_bytes() -> None:
    payload = b"\x00\x01\x02hello-binary\xff"
    stream = (
        encode_hi("Quan", 5000, "file")
        + encode_file_header(r"D:\Docs\slides|v1.pdf", len(payload))
        + payload
    )
    sock = _SlowSocket(stream, max_chunk=4)

    assert recv_hi(sock) == HiFrame(name="Quan", tcp_port=5000, purpose="file")
    header = recv_file_header(sock)
    assert header == FileHeaderFrame(filename="slides_v1.pdf", size=len(payload))
    assert recv_exact(sock, header.size) == payload


def test_empty_file_allowed() -> None:
    sock = _SlowSocket(encode_file_header("empty.txt", 0))
    assert recv_file_header(sock) == FileHeaderFrame(filename="empty.txt", size=0)


@pytest.mark.parametrize(
    ("raw_name", "expected"),
    [
        ("slides.pdf", "slides.pdf"),
        ("/home/user/report.docx", "report.docx"),
        (r"C:\Users\Khang\a|b.txt", "a_b.txt"),
        ("../../etc/passwd", "passwd"),
        ("report:v1?.pdf", "report_v1_.pdf"),
        ('a*b<c>d"e.txt', "a_b_c_d_e.txt"),
    ],
)
def test_sanitize_file_name_valid(raw_name: str, expected: str) -> None:
    assert sanitize_filename(raw_name) == expected


@pytest.mark.parametrize("bad_name", ["", ".", "..", "folder/", r"dir\.."])
def test_sanitize_filename_rejected(bad_name: str) -> None:
    with pytest.raises(ProtocolError):
        sanitize_filename(bad_name)


def test_invalid_first_packet_rejected() -> None:
    sock = _SlowSocket(b"abc|123")
    with pytest.raises(ProtocolError):
        recv_hi(sock)


def test_header_field_over_255_bytes_rejected() -> None:
    sock = _SlowSocket(b"A" * 256 + b"|")
    with pytest.raises(ProtocolError):
        recv_chat_frame(sock)


@pytest.mark.parametrize(
    "bad_frame",
    [
        b"MSG|0|",
        b"MSG|-5|hello",
        b"MSG|abc|hello",
        b"MSG|65537|a",
        b"UNKNOWN|1|a",
    ],
)
def test_malformed_chat_frames(bad_frame: bytes) -> None:
    sock = _SlowSocket(bad_frame)
    with pytest.raises(ProtocolError):
        recv_chat_frame(sock)


def test_unexpected_eof_raise_connection_closed() -> None:
    sock = _SlowSocket(b"MSG|10|abc")
    with pytest.raises(ConnectionClosedError):
        recv_chat_frame(sock)


@pytest.mark.parametrize(
    ("name", "port", "purpose"),
    [
        ("", 5000, "chat"),
        (" Minh", 5000, "chat"),
        ("Minh  Anh", 5000, "chat"),
        ("a|b", 5000, "chat"),
        ("Quan", 0, "chat"),
        ("Quan", 65536, "chat"),
        ("Quan", True, "chat"),
        ("Quan", 5000, "voice"),
    ],
)
def test_encode_hi_rejects_invalid_inputs(name: str, port: int, purpose: str) -> None:
    with pytest.raises(ProtocolError):
        encode_hi(name, port, purpose)


def test_encode_msg_rejects_empty_text() -> None:
    with pytest.raises(ProtocolError):
        encode_msg("")


def test_invalid_utf8_payload_raises_protocol_error() -> None:
    sock = _SlowSocket(b"MSG|2|\xff\xfe")
    with pytest.raises(ProtocolError):
        recv_chat_frame(sock)


def test_eof_mid_header_raises_connection_closed() -> None:
    sock = _SlowSocket(b"HI|Quan")
    with pytest.raises(ConnectionClosedError):
        recv_hi(sock)
