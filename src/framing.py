"""TCP framing encode and decode for the LAN chat p2p protocol"""

import socket
from dataclasses import dataclass

DELIMITER = b"|"
MAX_HEADER_FIELD_BYTES = 255
MAX_NAME_BYTES = 32
MIN_PORT = 1
MAX_PORT = 65_535
MIN_MSG_BYTES = 1
MAX_MSG_BYTES = 65_536
MIN_FILE_BYTES = 0
MAX_FILE_BYTES = 4 * 1024**3  # 4GiB
FILE_CHUNK_SIZE = 64 * 1024  # 64KiB
VALID_PURPOSES = frozenset({"chat", "file"})
INVALID_FILENAME_CHARS = '<>:"/\\|?*'


class FramingError(Exception):
    """Base exception for framing error"""


class ProtocolError(FramingError):
    """Raised when incoming bytes violate the protocol specification"""


class ConnectionClosedError(FramingError):
    """Raised when the peer closes the connection before expected"""


@dataclass(frozen=True)
class HiFrame:
    """Initial handshake packet sent on every new TCP connection"""

    name: str
    tcp_port: int
    purpose: str


@dataclass(frozen=True)
class MsgFrame:
    """Chat message packet"""

    text: str


@dataclass(frozen=True)
class FileHeaderFrame:
    """File metadata header preceding raw file bytes"""

    filename: str
    size: int


@dataclass(frozen=True)
class ByeFrame:
    """Graceful chat termination packet"""


def validate_name(name: str) -> str:
    """Validate a username against protocol rules."""
    if not name or name != name.strip() or "  " in name:
        raise ProtocolError(f"Invalid username spacing: {name!r}")
    if "|" in name or "\n" in name or "\r" in name:
        raise ProtocolError(f"Invalid character in username: {name!r}")
    name_bytes = name.encode("utf-8")
    if not (1 <= len(name_bytes) <= MAX_NAME_BYTES):
        raise ProtocolError(f"Username byte length out of range: {len(name_bytes)}")

    return name


def sanitize_filename(raw_name: str) -> str:
    """Extract basename, replace unsafe characters, and validate filename"""
    base = raw_name.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    cleaned = "".join("_" if ch in INVALID_FILENAME_CHARS else ch for ch in base)
    if cleaned in {"", ".", ".."}:
        raise ProtocolError(f"Invalid filename after sanitization: {raw_name!r}")
    if len(cleaned.encode("utf-8")) > MAX_HEADER_FIELD_BYTES:
        raise ProtocolError("Filename exceeds 255 UTF-8 bytes")
    return cleaned


def _parse_uint(raw: str, *, min_val: int, max_val: int, field_name: str) -> int:
    """Parse an unsigned ASCII decimal integer within [min_val, max_val]"""
    if not (raw and raw.isascii() and raw.isdigit()):
        raise ProtocolError(f"Invalid integer for {field_name}: {raw!r}")
    value = int(raw)
    if not (min_val <= value <= max_val):
        raise ProtocolError(f"{field_name} out of range [{min_val}, {max_val}]: {value}")

    return value


def recv_field(sock: socket.socket) -> str:
    """Read bytes one by one until '|' (max 255 bytes before delimiter)"""
    buf = bytearray()
    for _ in range(MAX_HEADER_FIELD_BYTES + 1):
        byte = sock.recv(1)
        if not byte:
            raise ConnectionClosedError("Connection closed while reading header field")
        if byte == DELIMITER:
            try:
                return buf.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProtocolError("Header field is not valid UTF-8") from exc
        if len(buf) == MAX_HEADER_FIELD_BYTES:
            raise ProtocolError("Header field exceeds 255 bytes without delimiter")
        buf.extend(byte)
    raise ProtocolError("Header field exceeds 255 bytes without delimiter")


def recv_exact(sock: socket.socket, num_bytes: int) -> bytes:
    """Read exactly num_bytes from sock, looping across partial recv() call"""
    if num_bytes < 0:
        raise ProtocolError(f"Negative read length: {num_bytes}")
    if num_bytes == 0:
        return b""
    buf = bytearray()
    while len(buf) < num_bytes:
        remaining = num_bytes - len(buf)
        chunk = sock.recv(min(4096, remaining))
        if not chunk:
            raise ConnectionClosedError(f"Connection closed after {len(buf)}/{num_bytes} bytes")
        buf.extend(chunk)
    return bytes(buf)


# --- Encoders ---


def encode_hi(name: str, tcp_port: int, purpose: str) -> bytes:
    """Encode a HI|<name>|<tcp-port>|<purpose>| frame"""
    valid_name = validate_name(name)
    if isinstance(tcp_port, bool) or not isinstance(tcp_port, int):
        raise ProtocolError(f"TCP port must be an integer, got {type(tcp_port).__name__}")
    if not (MIN_PORT <= tcp_port <= MAX_PORT):
        raise ProtocolError(f"Invalid TCP port: {tcp_port}")
    if purpose not in VALID_PURPOSES:
        raise ProtocolError(f"Invalid HI purpose: {purpose!r}")
    return f"HI|{valid_name}|{tcp_port}|{purpose}|".encode()


def encode_msg(text: str) -> bytes:
    """Encode a MSG|<length>|<utf-8 text> frame"""
    body = text.encode("utf-8")
    if not (MIN_MSG_BYTES <= len(body) <= MAX_MSG_BYTES):
        raise ProtocolError(f"Message byte length must be in [{MIN_MSG_BYTES}, {MAX_MSG_BYTES}]")
    header = f"MSG|{len(body)}|".encode()
    return header + body


def encode_bye() -> bytes:
    """Encode a BYE| frame"""
    return b"BYE|"


def encode_file_header(filepath_or_name: str, size: int) -> bytes:
    """Encode a FILE|<filename>|<size>| header"""
    filename = sanitize_filename(filepath_or_name)
    if isinstance(size, bool) or not isinstance(size, int):
        raise ProtocolError(f"File size must be an integer, got {type(size).__name__}")
    if not (MIN_FILE_BYTES <= size <= MAX_FILE_BYTES):
        raise ProtocolError(f"File size must be in [{MIN_FILE_BYTES}, {MAX_FILE_BYTES}]")
    return f"FILE|{filename}|{size}|".encode()


# --- Decoders ---


def recv_hi(sock: socket.socket) -> HiFrame:
    """Read and validate the initial HI frame on a new TCP connection"""
    packet_type = recv_field(sock)
    if packet_type != "HI":
        raise ProtocolError(f"Expected HI as first packet, got {packet_type!r}")

    name = validate_name(recv_field(sock))
    port = _parse_uint(
        recv_field(sock),
        min_val=MIN_PORT,
        max_val=MAX_PORT,
        field_name="tcp_port",
    )
    purpose = recv_field(sock)
    if purpose not in VALID_PURPOSES:
        raise ProtocolError(f"Invalid HI purpose: {purpose!r}")
    return HiFrame(name=name, tcp_port=port, purpose=purpose)


def recv_chat_frame(sock: socket.socket) -> MsgFrame | ByeFrame:
    """Read the next MSG or BYE frame on an established chat connection"""
    packet_type = recv_field(sock)
    if packet_type == "MSG":
        length = _parse_uint(
            recv_field(sock),
            min_val=MIN_MSG_BYTES,
            max_val=MAX_MSG_BYTES,
            field_name="length",
        )
        raw_body = recv_exact(sock, length)
        try:
            text = raw_body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("Message payload is not valid UTF-8") from exc
        return MsgFrame(text=text)
    if packet_type == "BYE":
        return ByeFrame()
    raise ProtocolError(f"Unexpected packet type on chat connection: {packet_type!r}")


def recv_file_header(sock: socket.socket) -> FileHeaderFrame:
    """Read a FILE header on a file connection; caller streams raw bytes next"""
    packet_type = recv_field(sock)
    if packet_type != "FILE":
        raise ProtocolError(f"Expected FILE packet, got {packet_type!r}")
    filename = sanitize_filename(recv_field(sock))
    size = _parse_uint(
        recv_field(sock),
        min_val=MIN_FILE_BYTES,
        max_val=MAX_FILE_BYTES,
        field_name="size",
    )
    return FileHeaderFrame(filename=filename, size=size)
