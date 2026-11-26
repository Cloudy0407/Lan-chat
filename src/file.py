"""TCP file transfer for LAN chat P2P protocol"""

import socket
import threading
from collections.abc import Callable
from pathlib import Path

from framing import (
    FILE_CHUNK_SIZE,
    ConnectionClosedError,
    FramingError,
    encode_file_header,
    encode_hi,
    recv_file_header,
)

CONNECT_TIMEOUT = 5.0
TRANSFER_TIMEOUT = 30.0
DEFAULT_DOWNLOAD_DIR = Path("downloads")

_PATH_LOCK = threading.Lock()

StartCallback = Callable[[str, int], None]
ProgressCallback = Callable[[int, int], None]


def progress_percent(done: int, total: int) -> int:
    """Return transfer completion percentage int [0,100] handling 0-byte files"""
    if total <= 0:
        return 100
    return min(100, (done * 100) // total)


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
        while True:
            final_path = resolve_unique_path(download_dir, filename)
            part_path = final_path.with_name(f"{final_path.name}.part")
            try:
                part_path.touch(exist_ok=False)
            except FileExistsError:
                continue
            return final_path, part_path


def send_file(
    *,
    sender_name: str,
    sender_tcp_port: int,
    peer_host: str,
    peer_port: int,
    filepath: str | Path,
    timeout: float = TRANSFER_TIMEOUT,
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
        sock.settimeout(timeout)
        sock.sendall(hi_frame)
        sock.sendall(file_header)

        sent_bytes = 0
        last_percent = progress_percent(0, total_size)
        if on_progress is not None:
            on_progress(0, total_size)

        with src_path.open("rb") as file_obj:
            while chunk := file_obj.read(FILE_CHUNK_SIZE):
                sock.sendall(chunk)
                sent_bytes += len(chunk)
                percent = progress_percent(sent_bytes, total_size)
                if on_progress is not None and (
                    percent != last_percent or sent_bytes == total_size
                ):
                    on_progress(sent_bytes, total_size)
                    last_percent = percent
    return src_path


def send_file_async(
    *,
    sender_name: str,
    sender_tcp_port: int,
    peer_host: str,
    peer_port: int,
    filepath: str | Path,
    timeout: float = TRANSFER_TIMEOUT,
    on_progress: ProgressCallback | None = None,
    on_done: Callable[[Path], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> threading.Thread:
    """Run send_file in a background daemon thread so chat stays responsive"""

    def _worker() -> None:
        try:
            send_path = send_file(
                sender_name=sender_name,
                sender_tcp_port=sender_tcp_port,
                peer_host=peer_host,
                peer_port=peer_port,
                filepath=filepath,
                timeout=timeout,
                on_progress=on_progress,
            )
        except (OSError, FramingError) as exc:
            if on_error is not None:
                on_error(exc)
            return
        if on_done is not None:
            on_done(send_path)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread


def recv_file(
    sock: socket.socket,
    *,
    download_dir: str | Path = DEFAULT_DOWNLOAD_DIR,
    timeout: float = TRANSFER_TIMEOUT,
    on_start: StartCallback | None = None,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """Receive a FILE frame and its raw bytes into download_dir (after HI is read)"""
    sock.settimeout(timeout)
    header = recv_file_header(sock)
    target_dir = Path(download_dir)
    final_path, part_path = _reserve_download_paths(target_dir, header.filename)

    if on_start is not None:
        on_start(final_path.name, header.size)

    received = 0
    last_percent = progress_percent(0, header.size)
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
                percent = progress_percent(received, header.size)
                if on_progress is not None and (percent != last_percent or received == header.size):
                    on_progress(received, header.size)
                    last_percent = percent
        part_path.rename(final_path)
    except BaseException:
        part_path.unlink(missing_ok=True)
        raise

    return final_path
