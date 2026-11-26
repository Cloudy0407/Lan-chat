import argparse
import os
import socket
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

from src import state
from src.error_handler import (
    FileTransferInterruptedError,
    PeerDisconnectedError,
    PeerOfflineError,
    handle_error,
)
from src.ui_console import ConsoleUI


def recv_exact(sock: socket.socket, num_bytes: int) -> bytes:
    """Đọc chính xác num_bytes từ TCP stream."""
    chunks = bytearray()
    while len(chunks) < num_bytes:
        chunk = sock.recv(num_bytes - len(chunks))
        if not chunk:
            raise ConnectionResetError("Peer closed connection unexpectedly.")
        chunks.extend(chunk)
    return bytes(chunks)


def recv_line(sock: socket.socket) -> str:
    """Đọc từng byte cho tới khi gặp ký tự xuống dòng '\\n'."""
    line = bytearray()
    while True:
        ch = sock.recv(1)
        if not ch:
            raise ConnectionResetError("Peer closed connection unexpectedly.")
        if ch == b"\n":
            break
        line.extend(ch)
    return line.decode("utf-8", errors="replace")


class LanChatApp:
    def __init__(self, username: str, tcp_port: int, udp_port: int, ui: ConsoleUI):
        self.username = username
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.ui = ui
        self.status = "online"
        self.running = False

        # Lock bảo vệ active_sockets và unread_messages (danh sách peer đã có lock bên src/state.py)
        self._lock = threading.Lock()
        self.active_sockets: Dict[str, socket.socket] = {}
        self.unread_messages: Dict[str, List[str]] = {}

        self.udp_sock: Optional[socket.socket] = None
        self.tcp_server_sock: Optional[socket.socket] = None

    def start(self) -> None:
        self.running = True
        os.makedirs("downloads", exist_ok=True)

        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.udp_sock.bind(("", self.udp_port))
        self.udp_sock.settimeout(1.0)

        self.tcp_server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp_server_sock.bind(("", self.tcp_port))
        self.tcp_server_sock.listen(10)
        self.tcp_server_sock.settimeout(1.0)

        threading.Thread(target=self._udp_broadcaster_loop, daemon=True).start()
        threading.Thread(target=self._udp_listener_loop, daemon=True).start()
        threading.Thread(target=self._peer_timeout_loop, daemon=True).start()
        threading.Thread(target=self._tcp_acceptor_loop, daemon=True).start()

        self.ui.display_message(f"[*] Listening on TCP port {self.tcp_port}")
        self.ui.display_message(f"[*] Broadcasting presence on UDP port {self.udp_port}")

    def stop(self) -> None:
        self.running = False
        with self._lock:
            for sock in list(self.active_sockets.values()):
                try:
                    sock.sendall(b"BYE|\n")
                    sock.close()
                except OSError:
                    pass
            self.active_sockets.clear()

        for s in (self.udp_sock, self.tcp_server_sock):
            if s:
                try:
                    s.close()
                except OSError:
                    pass
        self.ui.display_message("[*] Disconnected and exited.")

    # --------------------------------------------------------------------------
    # UDP DISCOVERY (Dùng trực tiếp các hàm trong src/state.py của Vai A)
    # --------------------------------------------------------------------------
    def _udp_broadcaster_loop(self) -> None:
        while self.running:
            try:
                payload = f"HELLO|{self.username}|{self.tcp_port}|{self.status}".encode("utf-8")
                for target_ip in ("255.255.255.255", "127.255.255.255", "127.0.0.1"):
                    try:
                        if self.udp_sock:
                            self.udp_sock.sendto(payload, (target_ip, self.udp_port))
                    except OSError:
                        pass
            except Exception as err:
                handle_error(err, context="UDP_BROADCASTER")
            time.sleep(3.0)

    def _udp_listener_loop(self) -> None:
        while self.running:
            try:
                if not self.udp_sock:
                    break
                data, addr = self.udp_sock.recvfrom(1024)
                msg = data.decode("utf-8", errors="replace").strip()
                parts = msg.split("|")
                if len(parts) == 4 and parts[0] == "HELLO":
                    _, peer_name, port_str, peer_status = parts
                    peer_port = int(port_str)

                    if peer_name == self.username and peer_port == self.tcp_port:
                        continue

                    # Gọi update_peer từ src/state.py
                    res = state.update_peer(peer_name, addr[0], peer_port, peer_status)
                    if res == "new":
                        self.ui.display_message(f"[*] {peer_name} ({addr[0]}) is now online.")
                    elif res == "conflict":
                        self.ui.display_message(
                            f"[!] Warning: Name conflict for '{peer_name}' from {addr[0]}."
                        )
            except socket.timeout:
                continue
            except Exception as err:
                if self.running:
                    handle_error(err, context="UDP_LISTENER")

    def _peer_timeout_loop(self) -> None:
        while self.running:
            time.sleep(2.0)
            # Gọi remove_stale_peers từ src/state.py
            offline_peers = state.remove_stale_peers(10.0)
            for name in offline_peers:
                self.ui.display_message(f"[*] {name} went offline (timeout).")

    # --------------------------------------------------------------------------
    # COMMAND HANDLERS
    # --------------------------------------------------------------------------
    def handle_list(self) -> None:
        # Gọi list_peers từ src/state.py
        peers = state.list_peers()
        if not peers:
            self.ui.display_message("[*] No other users online right now.")
            return
        lines = [
            f"{idx}. {p['name']} ({p['ip']}) {p['status']}"
            for idx, p in enumerate(peers, start=1)
        ]
        self.ui.display_message("\n".join(lines))

    def handle_status(self, new_status: str) -> None:
        self.status = new_status
        self.ui.display_message(f"[*] Status changed to '{self.status}'.")

    def _resolve_peer(self, target: str) -> Tuple[str, dict]:
        if target.isdigit():
            peers = state.list_peers()
            idx = int(target) - 1
            if 0 <= idx < len(peers):
                info = peers[idx]
                return info["name"], info
        else:
            info = state.get_peer(target)
            if info is not None:
                return target, info
        raise PeerOfflineError(target)

    def handle_chat(self, target: str) -> None:
        peer_name, info = self._resolve_peer(target)

        with self._lock:
            sock = self.active_sockets.get(peer_name)

        if sock is None:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                # Sử dụng đúng key 'tcp_port' từ src/state.py
                sock.connect((info["ip"], info["tcp_port"]))
                sock.settimeout(None)
                intro = f"INTRO|{self.username}\n".encode("utf-8")
                sock.sendall(intro)

                with self._lock:
                    self.active_sockets[peer_name] = sock

                threading.Thread(
                    target=self._tcp_receiver_loop,
                    args=(sock, peer_name),
                    daemon=True,
                ).start()
            except Exception:
                raise PeerOfflineError(peer_name)

        self.ui.set_partner(peer_name)

        with self._lock:
            unread = self.unread_messages.pop(peer_name, [])

        if unread:
            self.ui.display_message(
                f"[*] Switched to {peer_name} — {len(unread)} unread:"
            )
            for msg in unread:
                self.ui.display_message(f"[{peer_name}] {msg}")
        else:
            self.ui.display_message(
                f"[*] Connected to {peer_name} ({info['ip']}:{info['tcp_port']})"
            )

    def handle_leave(self) -> None:
        partner = self.ui.get_partner()
        if not partner:
            return
        with self._lock:
            sock = self.active_sockets.pop(partner, None)
        if sock:
            try:
                sock.sendall(b"BYE|\n")
                sock.close()
            except OSError:
                pass
        self.ui.display_message(f"[*] Left chat with {partner}.")

    def handle_send_message(self, text: str) -> None:
        partner = self.ui.get_partner()
        if not partner:
            return
        with self._lock:
            sock = self.active_sockets.get(partner)
        if not sock:
            self.ui.set_partner(None)
            raise PeerDisconnectedError(partner)

        try:
            data = text.encode("utf-8")
            header = f"MSG|{len(data)}\n".encode("utf-8")
            sock.sendall(header + data)
            self.ui.display_message(f"[{self.username}] {text}")
        except Exception:
            with self._lock:
                self.active_sockets.pop(partner, None)
            self.ui.set_partner(None)
            handle_error(PeerDisconnectedError(partner), printer=self.ui.display_message)

    def handle_send_file(self, file_path: str) -> None:
        partner = self.ui.get_partner()
        if not partner:
            return
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"File '{file_path}' does not exist.")

        with self._lock:
            sock = self.active_sockets.get(partner)
        if not sock:
            self.ui.set_partner(None)
            raise PeerDisconnectedError(partner)

        threading.Thread(
            target=self._file_sender_worker,
            args=(sock, partner, file_path),
            daemon=True,
        ).start()

    def _file_sender_worker(
        self, sock: socket.socket, partner: str, file_path: str
    ) -> None:
        filename = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        size_mb = file_size / (1024 * 1024)
        sent_bytes = 0
        last_percent = -1

        try:
            header = f"FILE|{filename}|{file_size}\n".encode("utf-8")
            sock.sendall(header)

            with open(file_path, "rb") as f:
                while sent_bytes < file_size:
                    chunk = f.read(8192)
                    if not chunk:
                        break
                    sock.sendall(chunk)
                    sent_bytes += len(chunk)

                    percent = int((sent_bytes * 100) / file_size) if file_size > 0 else 100
                    if percent >= last_percent + 25 or percent == 100:
                        last_percent = percent
                        if percent < 100:
                            self.ui.display_message(
                                f"[*] Sending {filename} ({size_mb:.2f} MB) ... {percent}%"
                            )
                        else:
                            self.ui.display_message(
                                f"[*] Sending {filename} ({size_mb:.2f} MB) ... 100% Done."
                            )
        except Exception as err:
            handle_error(
                FileTransferInterruptedError(str(err)),
                printer=self.ui.display_message,
                context="FILE_SENDER",
            )

    # --------------------------------------------------------------------------
    # TCP ACCEPTOR & RECEIVER
    # --------------------------------------------------------------------------
    def _tcp_acceptor_loop(self) -> None:
        while self.running:
            try:
                if not self.tcp_server_sock:
                    break
                conn, _ = self.tcp_server_sock.accept()
                threading.Thread(
                    target=self._handle_incoming_connection,
                    args=(conn,),
                    daemon=True,
                ).start()
            except socket.timeout:
                continue
            except Exception as err:
                if self.running:
                    handle_error(err, context="TCP_ACCEPTOR")

    def _handle_incoming_connection(self, conn: socket.socket) -> None:
        try:
            first_line = recv_line(conn)
            if first_line.startswith("INTRO|"):
                peer_name = first_line.split("|", 1)[1].strip()
                with self._lock:
                    self.active_sockets[peer_name] = conn
                self._tcp_receiver_loop(conn, peer_name)
            else:
                conn.close()
        except Exception:
            try:
                conn.close()
            except OSError:
                pass

    def _tcp_receiver_loop(self, sock: socket.socket, peer_name: str) -> None:
        current_partial_file: Optional[str] = None
        try:
            while self.running:
                header = recv_line(sock)
                if not header:
                    break

                parts = header.split("|")
                msg_type = parts[0]

                if msg_type == "MSG" and len(parts) == 2:
                    length = int(parts[1])
                    raw_msg = recv_exact(sock, length).decode("utf-8", errors="replace")

                    if self.ui.get_partner() == peer_name:
                        self.ui.display_message(f"[{peer_name}] {raw_msg}")
                    else:
                        with self._lock:
                            self.unread_messages.setdefault(peer_name, []).append(raw_msg)
                            count = len(self.unread_messages[peer_name])
                        self.ui.display_message(
                            f"[*] New message from {peer_name} ({count} unread)"
                        )

                elif msg_type == "FILE" and len(parts) == 3:
                    filename = os.path.basename(parts[1])
                    file_size = int(parts[2])
                    save_path = os.path.join("downloads", filename)
                    current_partial_file = save_path

                    received = 0
                    with open(save_path, "wb") as f:
                        while received < file_size:
                            to_read = min(8192, file_size - received)
                            chunk = recv_exact(sock, to_read)
                            f.write(chunk)
                            received += len(chunk)

                    current_partial_file = None
                    self.ui.display_message(
                        f"[*] Received file '{filename}' from {peer_name} -> saved to downloads/{filename}"
                    )

                elif msg_type == "BYE":
                    break

        except Exception as err:
            if self.running:
                handle_error(
                    err,
                    printer=self.ui.display_message,
                    partial_file_path=current_partial_file,
                    context=f"TCP_RECV_{peer_name}",
                )
        finally:
            with self._lock:
                self.active_sockets.pop(peer_name, None)
            try:
                sock.close()
            except OSError:
                pass
            if self.ui.get_partner() == peer_name:
                self.ui.set_partner(None)
                self.ui.display_message(
                    f"[*] {peer_name} disconnected. Returned to main prompt."
                )


def parse_args():
    parser = argparse.ArgumentParser(description="LAN Chat & File Sharing CLI")
    parser.add_argument("--name", type=str, required=True, help="Your display name")
    parser.add_argument("--port", type=int, default=5000, help="TCP listening port")
    parser.add_argument("--udp-port", type=int, default=9999, help="UDP discovery port")
    return parser.parse_args()


def main():
    args = parse_args()
    ui = ConsoleUI(username=args.name)
    app = LanChatApp(
        username=args.name,
        tcp_port=args.port,
        udp_port=args.udp_port,
        ui=ui,
    )

    ui.register_handler("list", app.handle_list)
    ui.register_handler("status", app.handle_status)
    ui.register_handler("chat", app.handle_chat)
    ui.register_handler("leave", app.handle_leave)
    ui.register_handler("message", app.handle_send_message)
    ui.register_handler("send", app.handle_send_file)
    ui.register_handler("quit", app.stop)

    try:
        app.start()
        ui.run()
    except Exception as err:
        handle_error(err, printer=ui.display_message, context="MAIN")
        app.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()