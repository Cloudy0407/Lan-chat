"""Unit tests for TCP ChatManager, connection handling, and file dispatch."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from chat import ChatManager
from framing import FramingError, encode_file_header, encode_hi, encode_msg
from state import AppState


class LogCapture:
    """Thread-safe log collector with condition-based waits (avoids time.sleep)."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._lines: list[str] = []

    def __call__(self, message: str) -> None:
        with self._cond:
            self._lines.append(message)
            self._cond.notify_all()

    @property
    def lines(self) -> list[str]:
        with self._cond:
            return list(self._lines)

    def wait_for(self, predicate: Callable[[list[str]], bool], timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._cond:
            while not predicate(self._lines):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=remaining)
            return True


def _wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def _conn_snapshot(
    mgr: ChatManager,
    peer_name: str,
) -> tuple[bool, tuple[str, int], tuple[str, int]] | None:
    """Thread-safe snapshot of (initiated_by_me, local_addr, remote_addr)."""
    with mgr._conn_lock:
        conn = mgr._conns.get(peer_name)
        if conn is None:
            return None
        try:
            return (
                conn.initiated_by_me,
                conn.sock.getsockname(),
                conn.sock.getpeername(),
            )
        except OSError:
            return None


def test_invalid_self_name_raises_framing_error(tmp_path: Path) -> None:
    with pytest.raises(FramingError):
        ChatManager(AppState("Bad|Name", 0), download_dir=tmp_path)


def test_start_port_in_use_closes_socket_and_raises(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        blocker.bind(("", 0))
        blocker.listen(1)
        occupied_port = blocker.getsockname()[1]

        mgr = ChatManager(AppState("Duy", occupied_port), download_dir=tmp_path)
        with pytest.raises(OSError):
            mgr.start()
        assert mgr._server_sock is None


def test_bidirectional_utf8_chat_auto_select_and_bye(tmp_path: Path) -> None:
    duy_state = AppState("Duy", 0)
    khang_state = AppState("Khang", 0)
    duy_logs = LogCapture()
    khang_logs = LogCapture()

    duy = ChatManager(duy_state, print_safe=duy_logs, download_dir=tmp_path / "d")
    khang = ChatManager(khang_state, print_safe=khang_logs, download_dir=tmp_path / "k")

    try:
        duy.start()
        khang_port = khang.start()
        duy_state.upsert_peer("Khang", "127.0.0.1", khang_port, "online")

        assert duy.open_chat("Khang") is True
        assert duy.is_connected("Khang")
        assert duy_state.get_current_partner() == "Khang"

        # Decision 6: idle Khang auto-selects Duy as current partner.
        assert khang_logs.wait_for(lambda ls: "[*] Duy connected. Now chatting with Duy." in ls)
        assert khang_state.get_current_partner() == "Duy"

        assert duy.send_message("Chào Khang, sẵn sàng demo chưa?") is True
        assert "[Duy] Chào Khang, sẵn sàng demo chưa?" in duy_logs.lines
        assert khang_logs.wait_for(lambda ls: "[Duy] Chào Khang, sẵn sàng demo chưa?" in ls)

        assert khang.send_message("Sẵn sàng rồi!") is True
        assert duy_logs.wait_for(lambda ls: "[Khang] Sẵn sàng rồi!" in ls)

        # Decision 3: /leave sends BYE| and closes the socket.
        assert duy.leave_chat() is True
        assert not duy.is_connected("Khang")
        assert duy_state.get_current_partner() is None
        assert khang_logs.wait_for(lambda ls: "[*] Duy left the chat." in ls)
        assert _wait_until(lambda: not khang.is_connected("Duy"))
        assert khang_state.get_current_partner() is None
    finally:
        duy.stop()
        khang.stop()


def test_unread_messages_and_switching_partner(tmp_path: Path) -> None:
    duy_state = AppState("Duy", 0)
    khang_state = AppState("Khang", 0)
    charlie_state = AppState("Charlie", 0)
    khang_logs = LogCapture()

    duy = ChatManager(duy_state, print_safe=LogCapture(), download_dir=tmp_path / "d")
    khang = ChatManager(khang_state, print_safe=khang_logs, download_dir=tmp_path / "k")
    charlie = ChatManager(charlie_state, print_safe=LogCapture(), download_dir=tmp_path / "c")

    try:
        duy.start()
        khang_port = khang.start()
        charlie.start()

        duy_state.upsert_peer("Khang", "127.0.0.1", khang_port, "online")
        charlie_state.upsert_peer("Khang", "127.0.0.1", khang_port, "online")

        assert duy.open_chat("Khang") is True
        assert khang_logs.wait_for(lambda ls: any("Now chatting with Duy" in line for line in ls))

        # Khang is busy with Duy; Charlie connects and sends 2 messages.
        assert charlie.open_chat("Khang") is True
        assert charlie.send_message("First unread") is True
        assert charlie.send_message("Second unread") is True

        assert khang_logs.wait_for(lambda ls: "[*] New message from Charlie (1 unread)" in ls)
        assert khang_logs.wait_for(lambda ls: "[*] New message from Charlie (2 unread)" in ls)
        assert khang_state.get_current_partner() == "Duy"

        # Decision 7: switching to Charlie prints only the 2 unread messages.
        assert khang.open_chat("Charlie") is True
        assert khang_state.get_current_partner() == "Charlie"
        lines = khang_logs.lines
        switch_idx = lines.index("[*] Switched to Charlie — 2 unread:")
        assert lines[switch_idx + 1] == "[Charlie] First unread"
        assert lines[switch_idx + 2] == "[Charlie] Second unread"

        # Switching again with no new unread prints plain switch notice.
        assert khang.open_chat("Charlie") is True
        assert khang_logs.lines[-1] == "[*] Switched to Charlie"
    finally:
        duy.stop()
        charlie.stop()
        khang.stop()


def test_open_chat_reconnecting_displays_existing_unread(tmp_path: Path) -> None:
    duy_state = AppState("Duy", 0)
    khang_state = AppState("Khang", 0)
    charlie_state = AppState("Charlie", 0)
    khang_logs = LogCapture()

    duy = ChatManager(duy_state, print_safe=LogCapture(), download_dir=tmp_path / "d")
    khang = ChatManager(khang_state, print_safe=khang_logs, download_dir=tmp_path / "k")
    charlie = ChatManager(charlie_state, print_safe=LogCapture(), download_dir=tmp_path / "c")

    try:
        duy.start()
        khang_port = khang.start()
        charlie_port = charlie.start()

        duy_state.upsert_peer("Khang", "127.0.0.1", khang_port, "online")
        charlie_state.upsert_peer("Khang", "127.0.0.1", khang_port, "online")
        khang_state.upsert_peer("Charlie", "127.0.0.1", charlie_port, "online")

        assert duy.open_chat("Khang") is True
        assert khang_logs.wait_for(lambda ls: any("Now chatting with Duy" in line for line in ls))

        # Charlie sends 1 unread message to busy Khang, then disconnects.
        assert charlie.open_chat("Khang") is True
        assert charlie.send_message("Missed before disconnect") is True
        assert khang_logs.wait_for(lambda ls: "[*] New message from Charlie (1 unread)" in ls)

        assert charlie.leave_chat() is True
        assert khang_logs.wait_for(lambda ls: "[*] Charlie left the chat." in ls)
        assert _wait_until(lambda: not khang.is_connected("Charlie"))

        # Reconnecting via open_chat() must display the preserved unread message.
        assert khang.open_chat("Charlie") is True
        lines = khang_logs.lines
        idx = lines.index("[*] 1 unread from Charlie:")
        assert lines[idx + 1] == "[Charlie] Missed before disconnect"
    finally:
        duy.stop()
        charlie.stop()
        khang.stop()


def test_open_chat_keeps_live_connection_when_peer_missing_from_discovery(
    tmp_path: Path,
) -> None:
    duy_state = AppState("Duy", 0)
    khang_state = AppState("Khang", 0)
    charlie_state = AppState("Charlie", 0)
    duy_logs = LogCapture()
    khang_logs = LogCapture()

    duy = ChatManager(duy_state, print_safe=duy_logs, download_dir=tmp_path / "d")
    khang = ChatManager(khang_state, print_safe=khang_logs, download_dir=tmp_path / "k")
    charlie = ChatManager(charlie_state, print_safe=LogCapture(), download_dir=tmp_path / "c")

    try:
        duy.start()
        khang_port = khang.start()
        charlie.start()

        # Khang has NO UDP discovery entry for Duy or Charlie.
        duy_state.upsert_peer("Khang", "127.0.0.1", khang_port, "online")
        charlie_state.upsert_peer("Khang", "127.0.0.1", khang_port, "online")

        assert duy.open_chat("Khang") is True
        assert khang_logs.wait_for(lambda ls: any("Now chatting with Duy" in line for line in ls))
        assert charlie.open_chat("Khang") is True
        assert _wait_until(lambda: khang.is_connected("Charlie"))

        assert khang_state.get_peer("Duy") is None
        assert khang_state.get_peer("Charlie") is None

        # Switching between connected peers must keep the live sockets intact.
        assert khang.open_chat("Charlie") is True
        assert khang.open_chat("Duy") is True
        assert khang.send_message("Still connected without HELLO") is True
        assert duy_logs.wait_for(lambda ls: "[Khang] Still connected without HELLO" in ls)
    finally:
        duy.stop()
        charlie.stop()
        khang.stop()


def test_open_chat_reconnects_when_peer_changes_port(tmp_path: Path) -> None:
    duy_state = AppState("Duy", 0)
    khang2_state = AppState("Khang", 0)
    duy_logs = LogCapture()
    khang2_logs = LogCapture()

    duy = ChatManager(duy_state, print_safe=duy_logs, download_dir=tmp_path / "d")
    khang2 = ChatManager(khang2_state, print_safe=khang2_logs, download_dir=tmp_path / "k2")
    accepted_old: list[socket.socket] = []

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old_server:
        old_server.bind(("127.0.0.1", 0))
        old_server.listen(1)
        old_server.settimeout(3.0)
        old_port = old_server.getsockname()[1]

        def _hold_silent_socket() -> None:
            conn, _ = old_server.accept()
            accepted_old.append(conn)

        holder = threading.Thread(target=_hold_silent_socket, daemon=True)
        holder.start()

        try:
            duy.start()
            khang2_port = khang2.start()

            duy_state.upsert_peer("Khang", "127.0.0.1", old_port, "online")
            assert duy.open_chat("Khang") is True
            holder.join(timeout=3.0)

            # Simulate Khang expiring and rejoining on a new TCP port.
            duy_state.expire_stale_peers(timeout_seconds=-1.0)
            duy_state.upsert_peer("Khang", "127.0.0.1", khang2_port, "online")

            assert duy.open_chat("Khang") is True
            assert duy.send_message("Hello new Khang") is True
            assert khang2_logs.wait_for(lambda ls: "[Duy] Hello new Khang" in ls)
        finally:
            for sock in accepted_old:
                sock.close()
            duy.stop()
            khang2.stop()


def test_open_chat_connect_and_handshake_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duy_state = AppState("Duy", 0)
    duy_logs = LogCapture()
    duy = ChatManager(duy_state, print_safe=duy_logs, download_dir=tmp_path / "d")
    duy_state.upsert_peer("Khang", "127.0.0.1", 59_999, "online")

    def _refuse(*_args: object, **_kwargs: object) -> socket.socket:
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(socket, "create_connection", _refuse)
    assert duy.open_chat("Khang") is False
    assert "[!] Could not connect to Khang: refused" in duy_logs.lines
    monkeypatch.undo()

    # Handshake failure: duy.start() was not called so self_tcp_port == 0 is invalid for HI.
    khang_state = AppState("Khang", 0)
    khang = ChatManager(khang_state, print_safe=LogCapture(), download_dir=tmp_path / "k")
    try:
        khang_port = khang.start()
        duy_state.expire_stale_peers(timeout_seconds=-1.0)
        duy_state.upsert_peer("Khang", "127.0.0.1", khang_port, "online")

        assert duy.open_chat("Khang") is False
        assert any("[!] Handshake with Khang failed:" in line for line in duy_logs.lines)
        assert not duy.is_connected("Khang")
    finally:
        duy.stop()
        khang.stop()


def test_decision_5_tie_break_deterministic(tmp_path: Path) -> None:
    # "Duy" < "Khang" -> Duy's outgoing connection always wins in all 4 arrival orders.
    duy_1 = ChatManager(AppState("Duy", 5001), download_dir=tmp_path / "d1")
    duy_2 = ChatManager(AppState("Duy", 5002), download_dir=tmp_path / "d2")
    khang_1 = ChatManager(AppState("Khang", 5003), download_dir=tmp_path / "k1")
    khang_2 = ChatManager(AppState("Khang", 5004), download_dir=tmp_path / "k2")

    pairs = [socket.socketpair() for _ in range(8)]
    try:
        d1_out, d1_in = pairs[0][0], pairs[1][0]
        d2_in, d2_out = pairs[2][0], pairs[3][0]
        k1_out, k1_in = pairs[4][0], pairs[5][0]
        k2_in, k2_out = pairs[6][0], pairs[7][0]

        # 1. Duy: outgoing first, incoming second -> incoming loses.
        assert duy_1._register_chat_socket("Khang", d1_out, initiated_by_me=True) is True
        assert duy_1._register_chat_socket("Khang", d1_in, initiated_by_me=False) is False
        assert duy_1._conns["Khang"].sock is d1_out

        # 2. Duy: incoming first, outgoing second -> outgoing wins and closes old incoming.
        assert duy_2._register_chat_socket("Khang", d2_in, initiated_by_me=False) is True
        assert duy_2._register_chat_socket("Khang", d2_out, initiated_by_me=True) is True
        assert duy_2._conns["Khang"].sock is d2_out
        assert d2_in.fileno() == -1

        # 3. Khang: outgoing first, incoming second -> incoming wins and closes old outgoing.
        assert khang_1._register_chat_socket("Duy", k1_out, initiated_by_me=True) is True
        assert khang_1._register_chat_socket("Duy", k1_in, initiated_by_me=False) is True
        assert khang_1._conns["Duy"].sock is k1_in
        assert k1_out.fileno() == -1

        # 4. Khang: incoming first, outgoing second -> outgoing loses.
        assert khang_2._register_chat_socket("Duy", k2_in, initiated_by_me=False) is True
        assert khang_2._register_chat_socket("Duy", k2_out, initiated_by_me=True) is False
        assert khang_2._conns["Duy"].sock is k2_in

        # After stop(), registering any new socket must be rejected.
        duy_1.stop()
        assert duy_1._register_chat_socket("Khang", d1_in, initiated_by_me=True) is False
        assert not duy_1.is_connected("Khang")
    finally:
        for mgr in (duy_1, duy_2, khang_1, khang_2):
            mgr.stop()
        for sock_a, sock_b in pairs:
            sock_a.close()
            sock_b.close()


def test_simultaneous_open_chat_converges_to_working_channel(tmp_path: Path) -> None:
    duy_state = AppState("Duy", 0)
    khang_state = AppState("Khang", 0)
    duy_logs = LogCapture()
    khang_logs = LogCapture()

    duy = ChatManager(duy_state, print_safe=duy_logs, download_dir=tmp_path / "d")
    khang = ChatManager(khang_state, print_safe=khang_logs, download_dir=tmp_path / "k")

    try:
        duy_port = duy.start()
        khang_port = khang.start()
        duy_state.upsert_peer("Khang", "127.0.0.1", khang_port, "online")
        khang_state.upsert_peer("Duy", "127.0.0.1", duy_port, "online")

        barrier = threading.Barrier(2)

        def _connect(mgr: ChatManager, target: str) -> None:
            barrier.wait(timeout=3.0)
            mgr.open_chat(target)

        t1 = threading.Thread(target=_connect, args=(duy, "Khang"))
        t2 = threading.Thread(target=_connect, args=(khang, "Duy"))
        t1.start()
        t2.start()
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)

        def _converged() -> bool:
            duy_snap = _conn_snapshot(duy, "Khang")
            khang_snap = _conn_snapshot(khang, "Duy")
            if duy_snap is None or khang_snap is None:
                return False
            duy_init, duy_local, duy_remote = duy_snap
            khang_init, khang_local, khang_remote = khang_snap
            return (
                duy_init != khang_init and duy_local == khang_remote and duy_remote == khang_local
            )

        assert _wait_until(_converged)

        assert duy.send_message("Ping from Duy", peer_name="Khang") is True
        assert khang_logs.wait_for(lambda ls: any("Ping from Duy" in line for line in ls))
        assert khang.send_message("Pong from Khang", peer_name="Duy") is True
        assert duy_logs.wait_for(lambda ls: any("Pong from Khang" in line for line in ls))
    finally:
        duy.stop()
        khang.stop()


def test_concurrent_incoming_connections_auto_select_only_one(tmp_path: Path) -> None:
    khang_state = AppState("Khang", 0)
    khang_logs = LogCapture()
    khang = ChatManager(khang_state, print_safe=khang_logs, download_dir=tmp_path)

    sockets: list[socket.socket] = []
    sock_lock = threading.Lock()
    try:
        khang_port = khang.start()
        barrier = threading.Barrier(2)

        def _client(name: str, msg: str) -> None:
            sock = socket.create_connection(("127.0.0.1", khang_port), timeout=3.0)
            with sock_lock:
                sockets.append(sock)
            barrier.wait(timeout=3.0)
            sock.sendall(encode_hi(name, 5500, "chat") + encode_msg(msg))

        t1 = threading.Thread(target=_client, args=("PeerA", "hello-A"))
        t2 = threading.Thread(target=_client, args=("PeerB", "hello-B"))
        t1.start()
        t2.start()
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)

        # Wait until both peers have delivered their message (past auto-select block).
        assert khang_logs.wait_for(
            lambda ls: (
                any("hello-A" in line or "New message from PeerA" in line for line in ls)
                and any("hello-B" in line or "New message from PeerB" in line for line in ls)
            )
        )

        auto_lines = [line for line in khang_logs.lines if "Now chatting with" in line]
        assert len(auto_lines) == 1
        partner = khang_state.get_current_partner()
        assert partner in {"PeerA", "PeerB"}
        assert f"Now chatting with {partner}." in auto_lines[0]
    finally:
        with sock_lock:
            for sock in sockets:
                sock.close()
        khang.stop()


def test_unexpected_disconnect_protocol_error_and_rogue_handshake(tmp_path: Path) -> None:
    khang_state = AppState("Khang", 0)
    khang_logs = LogCapture()
    khang = ChatManager(khang_state, print_safe=khang_logs, download_dir=tmp_path)

    try:
        khang_port = khang.start()

        # 1. Invalid handshake and self-handshake must be ignored without crashing server.
        with socket.create_connection(("127.0.0.1", khang_port), timeout=3.0) as rogue:
            rogue.sendall(b"GARBAGE|bad|")
        with socket.create_connection(("127.0.0.1", khang_port), timeout=3.0) as self_sock:
            self_sock.sendall(encode_hi("Khang", 5000, "chat"))

        # 2. Peer closes socket without sending BYE| -> Lost connection.
        with socket.create_connection(("127.0.0.1", khang_port), timeout=3.0) as ghost:
            ghost.sendall(encode_hi("Ghost", 5555, "chat"))
        assert khang_logs.wait_for(lambda ls: "[!] Lost connection to Ghost" in ls)
        assert _wait_until(lambda: not khang.is_connected("Ghost"))

        # 3. Peer sends malformed frame on chat connection -> Protocol error.
        with socket.create_connection(("127.0.0.1", khang_port), timeout=3.0) as bad:
            bad.sendall(encode_hi("BadPeer", 5556, "chat") + b"UNKNOWN|")
            assert khang_logs.wait_for(
                lambda ls: any("[!] Protocol error from BadPeer:" in line for line in ls)
            )
        assert _wait_until(lambda: not khang.is_connected("BadPeer"))
    finally:
        khang.stop()


def test_drop_peer_closes_without_bye(tmp_path: Path) -> None:
    duy_state = AppState("Duy", 0)
    khang_state = AppState("Khang", 0)
    khang_logs = LogCapture()

    duy = ChatManager(duy_state, print_safe=LogCapture(), download_dir=tmp_path / "d")
    khang = ChatManager(khang_state, print_safe=khang_logs, download_dir=tmp_path / "k")

    try:
        duy.start()
        khang_port = khang.start()
        duy_state.upsert_peer("Khang", "127.0.0.1", khang_port, "online")

        assert duy.open_chat("Khang") is True
        assert _wait_until(lambda: khang.is_connected("Duy"))

        assert duy.drop_peer("Nobody") is False
        assert duy.drop_peer("Khang") is True
        assert not duy.is_connected("Khang")
        assert khang_logs.wait_for(lambda ls: "[!] Lost connection to Duy" in ls)
    finally:
        duy.stop()
        khang.stop()


def test_send_message_framing_and_socket_errors(tmp_path: Path) -> None:
    duy_state = AppState("Duy", 5001)
    duy_logs = LogCapture()
    duy = ChatManager(duy_state, print_safe=duy_logs, download_dir=tmp_path)

    s1, s2 = socket.socketpair()
    try:
        assert duy._register_chat_socket("Khang", s1, initiated_by_me=True) is True
        duy_state.set_current_partner("Khang")

        # Empty message and payload > 65,536 bytes trigger FramingError without dropping conn.
        assert duy.send_message("") is False
        assert duy.send_message("a" * 65_537) is False
        framing_errors = [
            line for line in duy_logs.lines if line.startswith("[!] Cannot send message:")
        ]
        assert len(framing_errors) == 2
        assert duy.is_connected("Khang")

        # Closing underlying socket forces OSError on sendall() -> unregister & report loss.
        s1.close()
        assert duy.send_message("after socket close") is False
        assert "[!] Lost connection to Khang" in duy_logs.lines
        assert not duy.is_connected("Khang")
        assert duy_state.get_current_partner() is None
    finally:
        duy.stop()
        s1.close()
        s2.close()


def test_open_chat_and_send_message_validation_errors(tmp_path: Path) -> None:
    state = AppState("Duy", 0)
    logs = LogCapture()
    mgr = ChatManager(state, print_safe=logs, download_dir=tmp_path)

    assert mgr.open_chat("Duy") is False
    assert "[!] Cannot chat with yourself." in logs.lines

    assert mgr.open_chat("OfflinePeer") is False
    assert "[!] OfflinePeer is not online" in logs.lines

    assert mgr.send_message("hello") is False
    assert "[!] No active chat partner. Use /chat <name|number> first." in logs.lines

    assert mgr.send_message("hello", peer_name="Ghost") is False
    assert "[!] Not connected to Ghost. Use /chat Ghost first." in logs.lines

    assert mgr.leave_chat() is False
    assert "[!] Not currently in a chat." in logs.lines

    assert mgr.send_file_to_partner(tmp_path / "missing.txt") is None
    assert "[!] Use /chat <name|number> before sending a file." in logs.lines

    state.set_current_partner("Khang")
    assert mgr.send_file_to_partner(tmp_path / "missing.txt") is None
    assert any("file not found:" in line for line in logs.lines)


def test_send_and_recv_file_updates_transfer_state(tmp_path: Path) -> None:
    src_file = tmp_path / "slides.pdf"
    payload = bytes(range(256)) * 512  # 128 KiB
    src_file.write_bytes(payload)

    duy_state = AppState("Duy", 0)
    khang_state = AppState("Khang", 0)
    duy_logs = LogCapture()
    khang_logs = LogCapture()
    duy_updates = 0
    khang_updates = 0

    def _inc_duy() -> None:
        nonlocal duy_updates
        duy_updates += 1

    def _inc_khang() -> None:
        nonlocal khang_updates
        khang_updates += 1

    duy = ChatManager(
        duy_state,
        print_safe=duy_logs,
        download_dir=tmp_path / "d_dl",
        on_transfer_update=_inc_duy,
    )
    khang = ChatManager(
        khang_state,
        print_safe=khang_logs,
        download_dir=tmp_path / "k_dl",
        on_transfer_update=_inc_khang,
    )

    try:
        duy.start()
        khang_port = khang.start()
        duy_state.upsert_peer("Khang", "127.0.0.1", khang_port, "online")
        assert duy.open_chat("Khang") is True

        thread = duy.send_file_to_partner(src_file)
        assert thread is not None
        thread.join(timeout=5.0)
        assert not thread.is_alive()

        assert duy_logs.wait_for(lambda ls: "[*] Sent slides.pdf to Khang. Done." in ls)
        assert khang_logs.wait_for(
            lambda ls: any("[*] Received slides.pdf from Duy:" in line for line in ls)
        )

        saved = tmp_path / "k_dl" / "slides.pdf"
        assert saved.read_bytes() == payload
        assert duy_updates >= 2
        assert khang_updates >= 2
        assert duy_state.get_active_transfers() == []
        assert khang_state.get_active_transfers() == []
    finally:
        duy.stop()
        khang.stop()


def test_send_file_offline_and_connect_failure_updates_transfer_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src_file = tmp_path / "note.txt"
    src_file.write_bytes(b"hello")

    duy_state = AppState("Duy", 5001)
    duy_logs = LogCapture()
    duy = ChatManager(duy_state, print_safe=duy_logs, download_dir=tmp_path / "d")

    # Partner set, file exists, but peer is neither in discovery state nor connected.
    duy_state.set_current_partner("Khang")
    assert duy.send_file_to_partner(src_file) is None
    assert "[!] Khang is not online" in duy_logs.lines

    # Peer is online in discovery, but TCP connection fails -> on_error marks transfer failed.
    duy_state.upsert_peer("Khang", "127.0.0.1", 59_999, "online")

    def _refuse(*_args: object, **_kwargs: object) -> socket.socket:
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(socket, "create_connection", _refuse)
    thread = duy.send_file_to_partner(src_file)
    assert thread is not None
    thread.join(timeout=3.0)
    assert not thread.is_alive()

    assert duy_logs.wait_for(
        lambda ls: any("[!] Failed to send note.txt to Khang:" in line for line in ls)
    )
    assert duy_state.get_active_transfers() == []
    assert len(duy_state._transfers) == 1
    transfer = next(iter(duy_state._transfers.values()))
    assert transfer.status == "failed"
    assert transfer.error is not None


def test_send_file_fallback_uses_conn_peer_addr_and_recv_errors(tmp_path: Path) -> None:
    src_file = tmp_path / "fallback.txt"
    src_file.write_bytes(b"hello via HI port")

    duy_state = AppState("Duy", 0)
    khang_state = AppState("Khang", 0)
    duy_logs = LogCapture()
    khang_logs = LogCapture()

    duy = ChatManager(duy_state, print_safe=duy_logs, download_dir=tmp_path / "d_dl")
    khang = ChatManager(khang_state, print_safe=khang_logs, download_dir=tmp_path / "k_dl")

    try:
        duy.start()
        khang_port = khang.start()
        duy_state.upsert_peer("Khang", "127.0.0.1", khang_port, "online")

        # Khang has NO discovery record for Duy, only the incoming HI endpoint.
        assert duy.open_chat("Khang") is True
        assert khang_logs.wait_for(lambda ls: any("Now chatting with Duy" in line for line in ls))
        assert khang_state.get_peer("Duy") is None

        thread = khang.send_file_to_partner(src_file)
        assert thread is not None
        thread.join(timeout=5.0)
        assert duy_logs.wait_for(
            lambda ls: any("[*] Received fallback.txt from Khang:" in line for line in ls)
        )
        assert (tmp_path / "d_dl" / "fallback.txt").read_bytes() == b"hello via HI port"

        before_ids = set(khang_state._transfers)
        assert len(before_ids) == 1

        # 1. Malformed FILE header before on_start must not add any receive transfer record.
        with socket.create_connection(("127.0.0.1", khang_port), timeout=3.0) as bad_hdr:
            bad_hdr.sendall(encode_hi("Duy", 5001, "file") + b"BADHEADER|")
        assert khang_logs.wait_for(
            lambda ls: any("[!] Failed to receive file from Duy:" in line for line in ls)
        )
        assert set(khang_state._transfers) == before_ids
        assert not [t for t in khang_state._transfers.values() if t.direction == "receive"]

        # 2. Truncated incoming file deletes .part and marks receive transfer failed.
        with socket.create_connection(("127.0.0.1", khang_port), timeout=3.0) as broken:
            broken.sendall(
                encode_hi("Duy", 5001, "file")
                + encode_file_header("broken.bin", 100)
                + b"only-a-few"
            )
        assert khang_logs.wait_for(
            lambda ls: any("[!] Failed to receive broken.bin from Duy:" in line for line in ls)
        )
        assert khang_state.get_active_transfers() == []
        recv_transfers = [t for t in khang_state._transfers.values() if t.direction == "receive"]
        assert len(recv_transfers) == 1
        assert recv_transfers[0].status == "failed"
        assert list((tmp_path / "k_dl").iterdir()) == []
    finally:
        duy.stop()
        khang.stop()


def test_stop_sends_bye_to_all_peers_and_clears_unread(tmp_path: Path) -> None:
    duy_state = AppState("Duy", 0)
    khang_state = AppState("Khang", 0)
    charlie_state = AppState("Charlie", 0)
    duy_logs = LogCapture()
    khang_logs = LogCapture()
    charlie_logs = LogCapture()

    duy = ChatManager(duy_state, print_safe=duy_logs, download_dir=tmp_path / "d")
    khang = ChatManager(khang_state, print_safe=khang_logs, download_dir=tmp_path / "k")
    charlie = ChatManager(charlie_state, print_safe=charlie_logs, download_dir=tmp_path / "c")

    try:
        duy.start()
        khang_port = khang.start()
        charlie.start()

        duy_state.upsert_peer("Khang", "127.0.0.1", khang_port, "online")
        charlie_state.upsert_peer("Khang", "127.0.0.1", khang_port, "online")

        assert duy.open_chat("Khang") is True
        assert khang_logs.wait_for(lambda ls: any("Now chatting with Duy" in line for line in ls))
        assert charlie.open_chat("Khang") is True
        assert charlie.send_message("unread before quit") is True
        assert khang_logs.wait_for(lambda ls: "[*] New message from Charlie (1 unread)" in ls)

        khang.stop()

        assert khang._unread_counts == {}
        assert khang_state.is_stopped()
        assert duy_logs.wait_for(lambda ls: "[*] Khang left the chat." in ls)
        assert charlie_logs.wait_for(lambda ls: "[*] Khang left the chat." in ls)
    finally:
        duy.stop()
        charlie.stop()
        khang.stop()
