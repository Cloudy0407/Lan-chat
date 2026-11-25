"""TCP file transfer for LAN chat P2P protocol"""

import socket
import threading
from collections.abc import Callable
from pathlib import Path

from framing import (
    FILE_CHUNK_SIZE,
    ConnectionClosedError,
    encode_file_header,
    encode_hi,
    recv_file_header,
)

CONNECT_TIMEOUT = 5.0
DEFAULT_DOWNLOAD_DIR = Path("downloads")

_PATH_LOCK = threading.Lock()

ProgressCallback = Callable[[int, int], None]


def resolve_unique_path(download_dir: Path, filename: str) -> Path:
    """Return a non-conflicting path in download_dir using '(1)', '(2)' suffixes"""
    base_path = Path(filename)
    stem = base_path.stem
    suffix = base_path.suffix

    candidate = download_dir / filename
    counter = 1
    while candidate.exists() or candidate.with_name(f"{candidate.name}.part").exists():
        candidate = download_dir / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


def _reserve_download_paths(download_dir: Path, filename: str) -> tuple[Path, Path]:
    """Atomically reserve a final path and its temporary .part path"""
    download_dir.mkdir(parents=True, exist_ok=True)
    with _PATH_LOCK:
        final_path = resolve_unique_path(download_dir, filename)
        part_path = final_path.with_name(f"{final_path.name}.part")
        part_path.touch(exist_ok=False)
    return final_path, part_path


def send_file(
    *,
    sender_name: str,
    sender_tcp_port: int,
    peer_host: str,
    peer_port: int,
    filepath: str | Path,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """Send a local file to a peer over a dedicated TCP connection"""
    src_path = Path(filepath)
    if not src_path.is_file():
        raise FileNotFoundError(f"file not found: {filepath}")

    total_size = src_path.stat().st_size
    hi_frame = encode_hi(sender_name, sender_tcp_port, "file")
    file_header = encode_file_header(src_path.name, total_size)

    with socket.create_connection((peer_host, peer_port), timeout=CONNECT_TIMEOUT) as sock:
        sock.settimeout(None)
        sock.sendall(hi_frame)
        sock.sendall(file_header)

        sent_bytes = 0
        if on_progress is not None:
            on_progress(0, total_size)

        with src_path.open("rb") as file_obj:
            while chunk := file_obj.read(FILE_CHUNK_SIZE):
                sock.sendall(chunk)
                sent_bytes += len(chunk)
                if on_progress is not None:
                    on_progress(sent_bytes, total_size)
    return src_path


def recv_file(
    sock: socket.socket,
    *,
    download_dir: str | Path = DEFAULT_DOWNLOAD_DIR,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """Receive a FILE frame and its raw bytes into download_dir (after HI is read)"""
    header = recv_file_header(sock)
    target_dir = Path(download_dir)
    final_path, part_path = _reserve_download_paths(target_dir, header.filename)

    received = 0
    if on_progress is not None:
        on_progress(0, header.size)

    try:
        with part_path.open("wb") as file_obj:
            while received < header.size:
                remaining = header.size - received
                chunk = sock.recv(min(FILE_CHUNK_SIZE, remaining))
                if not chunk:
                    raise ConnectionClosedError(
                        f"Connection closed after {received}/{header.size} bytes"
                    )
                file_obj.write(chunk)
                received += len(chunk)
                if on_progress is not None:
                    on_progress(received, header.size)
        part_path.rename(final_path)
    except BaseException:
        part_path.unlink(missing_ok=True)
        raise

    return final_path
