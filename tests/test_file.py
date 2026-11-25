"""Unit tests for TCP file transfer"""

import socket
import threading
from pathlib import Path

import pytest

from file import recv_file, resolve_unique_path, send_file
from framing import ConnectionClosedError, HiFrame, encode_file_header, recv_hi


def test_resolve_unique_path_adds_suffix(tmp_path: Path) -> None:

    part_dir = tmp_path / "part_only"
    part_dir.mkdir()
    (part_dir / "slides.pdf.part").touch()
    assert resolve_unique_path(part_dir, "slides.pdf") == part_dir / "slides (1).pdf"

    assert resolve_unique_path(tmp_path, "slides.pdf") == tmp_path / "slides.pdf"

    (tmp_path / "slides.pdf").write_bytes(b"first")
    assert resolve_unique_path(tmp_path, "slides.pdf") == tmp_path / "slides (1).pdf"

    (tmp_path / "slides (1).pdf").write_bytes(b"second")
    assert resolve_unique_path(tmp_path, "slides.pdf") == tmp_path / "slides (2).pdf"


def test_send_and_recv_file_roundtrip(tmp_path: Path) -> None:
    src_file = tmp_path / "report.bin"
    payload = bytes(range(256)) * 1000  # 256KiB
    src_file.write_bytes(payload)

    download_dir = tmp_path / "downloads"
    received_hi: list[HiFrame] = []
    saved_paths: list[Path] = []
    recv_progress: list[tuple[int, int]] = []
    send_progress: list[tuple[int, int]] = []

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def _accept_and_receive() -> None:
            conn, _ = server.accept()
            with conn:
                received_hi.append(recv_hi(conn))
                path = recv_file(
                    conn,
                    download_dir=download_dir,
                    on_progress=lambda done, total: recv_progress.append((done, total)),
                )
                saved_paths.append(path)

        server.settimeout(5.0)
        worker = threading.Thread(target=_accept_and_receive)
        worker.start()

        send_file(
            sender_name="Khang",
            sender_tcp_port=5000,
            peer_host="127.0.0.1",
            peer_port=port,
            filepath=src_file,
            on_progress=lambda done, total: send_progress.append((done, total)),
        )
        worker.join(timeout=5)
        assert not worker.is_alive()

    assert received_hi == [HiFrame(name="Khang", tcp_port=5000, purpose="file")]
    assert len(saved_paths) == 1
    assert saved_paths[0] == download_dir / "report.bin"
    assert saved_paths[0].read_bytes() == payload
    assert not (download_dir / "report.bin.part").exists()

    assert send_progress[0] == (0, len(payload))
    assert send_progress[-1] == (len(payload), len(payload))
    assert len(send_progress) > 2

    assert recv_progress[0] == (0, len(payload))
    assert recv_progress[-1] == (len(payload), len(payload))
    assert len(recv_progress) > 2


def test_recv_duplicate_files_with_str_download_dir(tmp_path: Path) -> None:
    download_dir_str = str(tmp_path / "str_dir")

    s1_a, s2_a = socket.socketpair()
    with s1_a, s2_a:
        s1_a.sendall(encode_file_header("dup.txt", 5) + b"first")
        s1_a.shutdown(socket.SHUT_WR)
        first_path = recv_file(s2_a, download_dir=download_dir_str)

    s1_b, s2_b = socket.socketpair()
    with s1_b, s2_b:
        s1_b.sendall(encode_file_header("dup.txt", 6) + b"second")
        s1_b.shutdown(socket.SHUT_WR)
        second_path = recv_file(s2_b, download_dir=download_dir_str)

    expected_dir = tmp_path / "str_dir"

    assert first_path == expected_dir / "dup.txt"
    assert second_path == expected_dir / "dup (1).txt"
    assert first_path.read_bytes() == b"first"
    assert second_path.read_bytes() == b"second"


def test_recv_concurrent_duplicate_filenames(tmp_path: Path) -> None:
    payload_a = b"A" * 4096
    payload_b = b"B" * 4096

    s1_a, s2_a = socket.socketpair()
    s1_b, s2_b = socket.socketpair()
    barrier = threading.Barrier(2)
    saved_path: list[Path] = []
    errors: list[BaseException] = []
    results_lock = threading.Lock()

    def _receive(sock: socket.socket) -> None:
        try:
            barrier.wait(timeout=5)
            path = recv_file(sock, download_dir=tmp_path)
            with results_lock:
                saved_path.append(path)
        except BaseException as exc:
            with results_lock:
                errors.append(exc)

    with s1_a, s2_a, s1_b, s2_b:
        s1_a.sendall(encode_file_header("same.bin", len(payload_a)) + payload_a)
        s1_a.shutdown(socket.SHUT_WR)
        s1_b.sendall(encode_file_header("same.bin", len(payload_b)) + payload_b)
        s1_b.shutdown(socket.SHUT_WR)

        t1 = threading.Thread(target=_receive, args=(s2_a,))
        t2 = threading.Thread(target=_receive, args=(s2_b,))

        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

    assert not errors
    assert {p.name for p in saved_path} == {"same.bin", "same (1).bin"}
    assert {p.read_bytes() for p in saved_path} == {payload_a, payload_b}


def test_recv_sanitizes_malicious_filename(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"

    s1_a, s2_a = socket.socketpair()
    with s1_a, s2_a:
        s1_a.sendall(encode_file_header("../../evil|name.txt", 4) + b"safe")
        s1_a.shutdown(socket.SHUT_WR)
        saved_a = recv_file(s2_a, download_dir=download_dir)

    assert saved_a == download_dir / "evil_name.txt"
    assert saved_a.read_bytes() == b"safe"

    s1_b, s2_b = socket.socketpair()
    with s1_b, s2_b:
        s1_b.sendall(b"FILE|../../rogue.txt|5|hello")
        s1_b.shutdown(socket.SHUT_WR)
        saved_b = recv_file(s2_b, download_dir=download_dir)

    assert saved_b == download_dir / "rogue.txt"
    assert saved_b.read_bytes() == b"hello"
    assert not (tmp_path / "rogue.txt").exists()


def test_recv_empty_file(tmp_path: Path) -> None:
    s1, s2 = socket.socketpair()
    with s1, s2:
        s1.sendall(encode_file_header("empty.txt", 0))
        s1.shutdown(socket.SHUT_WR)
        saved = recv_file(s2, download_dir=tmp_path)

    assert saved == tmp_path / "empty.txt"
    assert saved.read_bytes() == b""


def test_recv_truncated_file_deletes_part(tmp_path: Path) -> None:
    s1, s2 = socket.socketpair()
    with s1, s2:
        s1.sendall(encode_file_header("broken.pdf", 100) + b"only-10-by")
        s1.close()
        with pytest.raises(ConnectionClosedError):
            recv_file(s2, download_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_send_missing_file_raises_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        send_file(
            sender_name="Khang",
            sender_tcp_port=5000,
            peer_host="127.0.0.1",
            peer_port=5000,
            filepath=tmp_path / "NotExist.pdf",
        )


def test_send_directory_raises_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        send_file(
            sender_name="Khang",
            sender_tcp_port=5000,
            peer_host="127.0.0.1",
            peer_port=5000,
            filepath=tmp_path,
        )
