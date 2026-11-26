"""TCP chat server, connection manager, and file transfer dispatcher."""

from __future__ import annotations

import socket
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from error_handler import format_error
from file import DEFAULT_DOWNLOAD_DIR, recv_file, send_file_async
from framing import (
    ByeFrame,
    ConnectionClosedError,
    FramingError,
    MsgFrame,
    encode_bye,
    encode_hi,
    encode_msg,
    recv_chat_frame,
    recv_hi,
    validate_name,
)
from state import AppState, ChatMessage, TransferProgress

CONNECT_TIMEOUT = 5.0
HANDSHAKE_TIMEOUT = 5.0
ACCEPT_POLL_TIMEOUT = 1.0

PrintFn = Callable[[str], None]
TransferUpdateFn = Callable[[], None]


@dataclass
class _PeerConn:
    """Active TCP chat socket with its origin flag, endpoint, and write lock."""

    sock: socket.socket
    initiated_by_me: bool
    peer_addr: tuple[str, int] | None = None
    send_lock: threading.Lock = field(default_factory=threading.Lock)


class ChatManager:
    """Coordinate TCP chat connections and dedicated TCP file transfers."""

    def __init__(
        self,
        state: AppState,
        *,
        print_safe: PrintFn = print,
        download_dir: str | Path = DEFAULT_DOWNLOAD_DIR,
        on_transfer_update: TransferUpdateFn | None = None,
    ) -> None:
        validate_name(state.self_name)
        self.state = state
        self.print_safe = print_safe
        self.download_dir = Path(download_dir)
        self._on_transfer_update = on_transfer_update

        self._conn_lock = threading.Lock()
        self._conns: dict[str, _PeerConn] = {}
        self._unread_counts: dict[str, int] = {}
        self._stop_event = threading.Event()
        self._server_sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None

    def start(self) -> int:
        """Bind the TCP server socket and launch the acceptor thread."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("", self.state.self_tcp_port))
            server.listen()
            server.settimeout(ACCEPT_POLL_TIMEOUT)
            bound_port = server.getsockname()[1]
        except BaseException:
            server.close()
            raise

        self.state.self_tcp_port = bound_port
        self._server_sock = server
        self._stop_event.clear()

        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        return bound_port

    def stop(self) -> None:
        """Stop accepting, send BYE| on all open chat sockets, and close everything."""
        self._stop_event.set()
        self.state.request_stop()

        if self._server_sock is not None:
            self._safe_close(self._server_sock)
            self._server_sock = None

        with self._conn_lock:
            active_items = list(self._conns.items())
            self._conns.clear()
            self._unread_counts.clear()
            for peer_name, _conn in active_items:
                self.state.remove_chat_socket(peer_name)

        for _peer_name, conn in active_items:
            self._send_bye_quietly(conn)
            self._safe_close(conn.sock)

        if self._accept_thread is not None and self._accept_thread.is_alive():
            self._accept_thread.join(timeout=2.0)

    def is_connected(self, peer_name: str) -> bool:
        """Return True if an active TCP chat connection exists for peer_name."""
        with self._conn_lock:
            return peer_name in self._conns

    def drop_peer(self, peer_name: str) -> bool:
        """Close and unregister a peer's chat socket without sending BYE|."""
        with self._conn_lock:
            conn = self._conns.pop(peer_name, None)
            if conn is not None:
                self.state.remove_chat_socket(peer_name)
        if conn is None:
            return False

        self._safe_close(conn.sock)
        return True

    def open_chat(self, peer_name: str) -> bool:
        """Open or reuse a TCP chat connection and switch current partner."""
        if peer_name == self.state.self_name:
            self.print_safe(format_error("Cannot chat with yourself."))
            return False

        peer = self.state.get_peer(peer_name)
        with self._conn_lock:
            existing = self._conns.get(peer_name)

        if (
            existing is not None
            and peer is not None
            and existing.peer_addr is not None
            and existing.peer_addr != (peer.ip, peer.tcp_port)
        ):
            self._unregister_chat_socket(peer_name, existing.sock)
            self._safe_close(existing.sock)
            existing = None

        already_connected = existing is not None
        if not already_connected:
            if peer is None:
                self.print_safe(format_error(f"{peer_name} is not online"))
                return False

            try:
                sock = socket.create_connection((peer.ip, peer.tcp_port), timeout=CONNECT_TIMEOUT)
            except OSError as exc:
                self.print_safe(format_error(f"Could not connect to {peer.name}: {exc}"))
                return False

            try:
                hi_bytes = encode_hi(self.state.self_name, self.state.self_tcp_port, "chat")
                sock.sendall(hi_bytes)
                sock.settimeout(None)
            except (FramingError, OSError) as exc:
                self._safe_close(sock)
                self.print_safe(format_error(f"Handshake with {peer.name} failed: {exc}"))
                return False

            kept = self._register_chat_socket(
                peer.name,
                sock,
                initiated_by_me=True,
                peer_addr=(peer.ip, peer.tcp_port),
            )
            if kept:
                threading.Thread(
                    target=self._chat_recv_loop,
                    args=(peer.name, sock),
                    daemon=True,
                ).start()
            else:
                self._safe_close(sock)
                if self._is_stopping():
                    return False

            self.print_safe(f"[*] Connected to {peer.name} ({peer.ip}:{peer.tcp_port})")

        unread_messages: list[ChatMessage] = []
        with self._conn_lock:
            unread = self._unread_counts.pop(peer_name, 0)
            history = self.state.set_current_partner(peer_name)
            if unread > 0:
                unread_messages = history[-unread:]

        if already_connected:
            if unread_messages:
                self.print_safe(f"[*] Switched to {peer_name} — {len(unread_messages)} unread:")
                for msg in unread_messages:
                    self.print_safe(f"[{msg.sender}] {msg.text}")
            else:
                self.print_safe(f"[*] Switched to {peer_name}")
        elif unread_messages:
            self.print_safe(f"[*] {len(unread_messages)} unread from {peer_name}:")
            for msg in unread_messages:
                self.print_safe(f"[{msg.sender}] {msg.text}")

        return True

    def send_message(self, text: str, peer_name: str | None = None) -> bool:
        """Send a UTF-8 chat message to the current (or specified) partner."""
        target = peer_name or self.state.get_current_partner()
        if target is None:
            self.print_safe(format_error("No active chat partner. Use /chat <name|number> first."))
            return False

        with self._conn_lock:
            conn = self._conns.get(target)
        if conn is None:
            self.print_safe(format_error(f"Not connected to {target}. Use /chat {target} first."))
            return False

        try:
            payload = encode_msg(text)
        except FramingError as exc:
            self.print_safe(format_error(f"Cannot send message: {exc}"))
            return False

        try:
            with conn.send_lock:
                conn.sock.sendall(payload)
        except OSError:
            if self._unregister_chat_socket(target, conn.sock):
                self.print_safe(format_error(f"Lost connection to {target}"))
            self._safe_close(conn.sock)
            return False

        self.state.record_message(target, self.state.self_name, text)
        self.print_safe(f"[{self.state.self_name}] {text}")
        return True

    def leave_chat(self, peer_name: str | None = None) -> bool:
        """Send BYE| and close the chat connection with the active partner."""
        target = peer_name or self.state.get_current_partner()
        if target is None:
            self.print_safe(format_error("Not currently in a chat."))
            return False

        with self._conn_lock:
            conn = self._conns.pop(target, None)
            self.state.remove_chat_socket(target)

        if conn is not None:
            self._send_bye_quietly(conn)
            self._safe_close(conn.sock)

        self.print_safe(f"[*] Left chat with {target}.")
        return True

    def send_file_to_partner(
        self,
        filepath: str | Path,
        peer_name: str | None = None,
    ) -> threading.Thread | None:
        """Start a background thread to send a file to the active partner."""
        target = peer_name or self.state.get_current_partner()
        if target is None:
            self.print_safe(format_error("Use /chat <name|number> before sending a file."))
            return None

        src_path = Path(filepath)
        if not src_path.is_file():
            self.print_safe(format_error(f"file not found: {filepath}"))
            return None

        try:
            total_size = src_path.stat().st_size
        except OSError as exc:
            self.print_safe(format_error(f"Cannot read {filepath}: {exc}"))
            return None

        peer = self.state.get_peer(target)
        if peer is not None:
            peer_label = peer.name
            peer_host, peer_port = peer.ip, peer.tcp_port
        else:
            with self._conn_lock:
                conn = self._conns.get(target)
            if conn is None or conn.peer_addr is None:
                self.print_safe(format_error(f"{target} is not online"))
                return None
            peer_label = target
            peer_host, peer_port = conn.peer_addr

        transfer_id = uuid.uuid4().hex[:8]
        done_bytes = 0

        def _update(
            done: int,
            total: int,
            *,
            status: str = "in_progress",
            error: str | None = None,
        ) -> None:
            nonlocal done_bytes
            done_bytes = done
            self.state.update_transfer(
                TransferProgress(
                    transfer_id=transfer_id,
                    direction="send",
                    peer_name=peer_label,
                    filename=src_path.name,
                    transferred_bytes=done,
                    total_bytes=total,
                    status=status,
                    error=error,
                )
            )
            self._notify_transfer_update()

        def _on_progress(done: int, total: int) -> None:
            _update(done, total)

        def _on_done(sent_path: Path) -> None:
            _update(total_size, total_size, status="completed")
            self.print_safe(f"[*] Sent {sent_path.name} to {peer_label}. Done.")

        def _on_error(exc: Exception) -> None:
            _update(done_bytes, total_size, status="failed", error=str(exc))
            self.print_safe(format_error(f"Failed to send {src_path.name} to {peer_label}: {exc}"))

        _update(0, total_size)
        return send_file_async(
            sender_name=self.state.self_name,
            sender_tcp_port=self.state.self_tcp_port,
            peer_host=peer_host,
            peer_port=peer_port,
            filepath=src_path,
            on_progress=_on_progress,
            on_done=_on_done,
            on_error=_on_error,
        )

    # --- Internal TCP server & receiver loops ---

    def _accept_loop(self) -> None:
        assert self._server_sock is not None
        while not self._is_stopping():
            try:
                conn, addr = self._server_sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break

            threading.Thread(
                target=self._handle_incoming_connection,
                args=(conn, addr[0]),
                daemon=True,
            ).start()

    def _handle_incoming_connection(self, sock: socket.socket, peer_ip: str) -> None:
        try:
            sock.settimeout(HANDSHAKE_TIMEOUT)
            hi = recv_hi(sock)
        except (FramingError, OSError):
            self._safe_close(sock)
            return

        if hi.name == self.state.self_name:
            self._safe_close(sock)
            return

        if hi.purpose == "file":
            self._handle_incoming_file(hi.name, sock)
            return

        sock.settimeout(None)
        kept = self._register_chat_socket(
            hi.name,
            sock,
            initiated_by_me=False,
            peer_addr=(peer_ip, hi.tcp_port),
        )
        if not kept:
            self._safe_close(sock)
            return

        # Decision 6: auto-select incoming peer as current partner when idle.
        auto_selected = False
        unread_messages: list[ChatMessage] = []
        with self._conn_lock:
            current = self._conns.get(hi.name)
            if (
                current is not None
                and current.sock is sock
                and self.state.get_current_partner() is None
            ):
                unread = self._unread_counts.pop(hi.name, 0)
                history = self.state.set_current_partner(hi.name)
                auto_selected = True
                if unread > 0:
                    unread_messages = history[-unread:]

        if auto_selected:
            self.print_safe(f"[*] {hi.name} connected. Now chatting with {hi.name}.")
            for msg in unread_messages:
                self.print_safe(f"[{msg.sender}] {msg.text}")

        self._chat_recv_loop(hi.name, sock)

    def _handle_incoming_file(self, sender_name: str, sock: socket.socket) -> None:
        transfer_id = uuid.uuid4().hex[:8]
        filename = "file"
        done_bytes = 0
        total_bytes = 0
        started = False

        def _update(
            done: int,
            total: int,
            *,
            status: str = "in_progress",
            error: str | None = None,
        ) -> None:
            nonlocal done_bytes, total_bytes
            done_bytes = done
            total_bytes = total
            self.state.update_transfer(
                TransferProgress(
                    transfer_id=transfer_id,
                    direction="receive",
                    peer_name=sender_name,
                    filename=filename,
                    transferred_bytes=done,
                    total_bytes=total,
                    status=status,
                    error=error,
                )
            )
            self._notify_transfer_update()

        def _on_start(incoming_name: str, size: int) -> None:
            nonlocal filename, started
            filename = incoming_name
            started = True
            _update(0, size)

        def _on_progress(done: int, total: int) -> None:
            _update(done, total)

        try:
            saved_path = recv_file(
                sock,
                download_dir=self.download_dir,
                on_start=_on_start,
                on_progress=_on_progress,
            )
        except (FramingError, OSError) as exc:
            if started:
                _update(done_bytes, total_bytes, status="failed", error=str(exc))
            self.print_safe(format_error(f"Failed to receive {filename} from {sender_name}: {exc}"))
        else:
            filename = saved_path.name
            final_size = saved_path.stat().st_size if saved_path.exists() else total_bytes
            _update(final_size, final_size, status="completed")
            self.print_safe(f"[*] Received {saved_path.name} from {sender_name}: {saved_path}")
        finally:
            self._safe_close(sock)

    def _chat_recv_loop(self, peer_name: str, sock: socket.socket) -> None:
        graceful_bye = False
        protocol_error: str | None = None
        try:
            while not self._is_stopping():
                frame = recv_chat_frame(sock)
                if isinstance(frame, MsgFrame):
                    with self._conn_lock:
                        is_current, unread = self.state.record_message(
                            peer_name, peer_name, frame.text
                        )
                        if is_current:
                            self._unread_counts.pop(peer_name, None)
                        else:
                            self._unread_counts[peer_name] = unread
                    if is_current:
                        self.print_safe(f"[{peer_name}] {frame.text}")
                    else:
                        self.print_safe(f"[*] New message from {peer_name} ({unread} unread)")
                elif isinstance(frame, ByeFrame):
                    graceful_bye = True
                    break
        except ConnectionClosedError:
            pass
        except FramingError as exc:
            protocol_error = str(exc)
        except OSError:
            pass
        finally:
            was_active = self._unregister_chat_socket(peer_name, sock)
            self._safe_close(sock)
            if was_active and not self._is_stopping():
                if graceful_bye:
                    self.print_safe(f"[*] {peer_name} left the chat.")
                elif protocol_error is not None:
                    self.print_safe(
                        format_error(f"Protocol error from {peer_name}: {protocol_error}")
                    )
                else:
                    self.print_safe(format_error(f"Lost connection to {peer_name}"))

    def _register_chat_socket(
        self,
        peer_name: str,
        sock: socket.socket,
        *,
        initiated_by_me: bool,
        peer_addr: tuple[str, int] | None = None,
    ) -> bool:
        """Register sock for peer_name, applying Decision 5 tie-breaking if needed."""
        replaced_sock: socket.socket | None = None
        with self._conn_lock:
            if self._is_stopping():
                return False

            existing = self._conns.get(peer_name)
            if existing is not None:
                if existing.initiated_by_me != initiated_by_me:
                    # Decision 5: keep the connection opened by the smaller name.
                    i_am_smaller = self.state.self_name < peer_name
                    new_wins = (initiated_by_me and i_am_smaller) or (
                        not initiated_by_me and not i_am_smaller
                    )
                    if not new_wins:
                        return False
                replaced_sock = existing.sock

            self._conns[peer_name] = _PeerConn(
                sock=sock,
                initiated_by_me=initiated_by_me,
                peer_addr=peer_addr,
            )
            self.state.register_chat_socket(peer_name, sock)

        if replaced_sock is not None:
            self._safe_close(replaced_sock)
        return True

    def _unregister_chat_socket(self, peer_name: str, sock: socket.socket) -> bool:
        with self._conn_lock:
            current = self._conns.get(peer_name)
            if current is not None and current.sock is sock:
                del self._conns[peer_name]
                self.state.remove_chat_socket(peer_name)
                return True
            return False

    def _notify_transfer_update(self) -> None:
        if self._on_transfer_update is not None:
            self._on_transfer_update()

    def _is_stopping(self) -> bool:
        return self._stop_event.is_set() or self.state.is_stopped()

    @staticmethod
    def _send_bye_quietly(conn: _PeerConn) -> None:
        try:
            with conn.send_lock:
                conn.sock.sendall(encode_bye())
        except OSError:
            pass

    @staticmethod
    def _safe_close(sock: socket.socket) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
