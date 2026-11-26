import socket
import threading
import time

from error_handler import format_error
from state import AppState
from ui_console import ConsoleUI

PORT = 9999
BROADCAST_ADDR = "255.255.255.255"
HELLO_INTERVAL = 3
PEER_TIMEOUT = 10
NAME_CHECK_WAIT = HELLO_INTERVAL + 1
VALID_STATUSES = ("online", "away")

_app: AppState | None = None
_ui: ConsoleUI | None = None
_sock: socket.socket | None = None
_warned: set[tuple[str, str, int]] = set()


def start(app: AppState, ui: ConsoleUI) -> bool:
    """Start discovery. Return False if the name is already taken."""
    global _app, _ui, _sock
    _app = app
    _ui = ui
    _sock = _make_socket()
    _ui.print_safe("Đang kiểm tra tên...")
    if _name_is_taken(NAME_CHECK_WAIT):
        _sock.close()
        _sock = None
        return False
    threading.Thread(target=_broadcaster, daemon=True).start()
    threading.Thread(target=_listener, daemon=True).start()
    threading.Thread(target=_cleaner, daemon=True).start()
    return True


def stop() -> None:
    """Release the UDP socket. Call after app.request_stop()."""
    if _sock is not None:
        _sock.close()


def _make_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", PORT))
    return sock


def _broadcaster() -> None:
    """Send HELLO every HELLO_INTERVAL seconds until the app stops."""
    while not _app.is_stopped():
        send_hello_now()
        _app.stop_event.wait(HELLO_INTERVAL)


def make_hello(name: str, tcp_port: int, status: str) -> bytes:
    """Build a HELLO packet."""
    text = f"HELLO|{name}|{tcp_port}|{status}"
    return text.encode("utf-8")


def parse_hello(data: bytes) -> tuple[str, int, str] | None:
    """Return (name, tcp_port, status), or None if the packet is invalid."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None

    elements = text.split("|")
    if len(elements) != 4 or elements[0] != "HELLO":
        return None

    _, name, port_text, status = elements

    if len(name) <= 0 or len(name.encode("utf-8")) > 32 or name.strip() != name or "  " in name:
        return None

    try:
        tcp_port = int(port_text)
    except ValueError:
        return None

    if tcp_port <= 0 or tcp_port > 65535:
        return None

    if status not in VALID_STATUSES:
        return None

    return name, tcp_port, status


def _warn_once(name: str, ip: str, tcp_port: int, message: str) -> None:
    """Print a warning only the first time a given source causes it."""
    key = (name, ip, tcp_port)
    if key in _warned:
        return
    _warned.add(key)
    _ui.print_safe(message)


def _name_is_taken(wait: float) -> bool:
    """Listen for `wait` seconds and return True if another peer already uses our name."""
    deadline = time.monotonic() + wait
    _sock.settimeout(0.5)
    try:
        while time.monotonic() < deadline:
            try:
                data, addr = _sock.recvfrom(1024)
            except TimeoutError:
                continue
            hello = parse_hello(data)
            if hello is None:
                continue
            name, tcp_port, status = hello
            if name == _app.self_name:
                return True
        return False
    finally:
        _sock.settimeout(None)


def _listener() -> None:
    """Receive HELLO packets and update the peer table."""
    while not _app.is_stopped():
        try:
            data, addr = _sock.recvfrom(1024)
        except OSError:
            if _app.is_stopped():
                break  # socket bị stop() đóng: thoát bình thường
            continue  # lỗi mạng tạm thời: nghe tiếp

        hello = parse_hello(data)
        if hello is None:
            continue

        name, tcp_port, status = hello
        ip = addr[0]

        if name == _app.self_name:
            if tcp_port != _app.self_tcp_port:
                _warn_once(
                    name,
                    ip,
                    tcp_port,
                    f"[!] Có người khác đang dùng tên '{name}' ({ip}:{tcp_port})",
                )
            continue

        result, _ = _app.upsert_peer(name, ip, tcp_port, status)
        if result == "added":
            _ui.print_safe(f"{name} is online")
        elif result == "conflict":
            _warn_once(
                name,
                ip,
                tcp_port,
                f"[!] Tên '{name}' đã có người dùng, bỏ qua gói từ {ip}:{tcp_port}",
            )


def _cleaner() -> None:
    """Every second, remove peers that timed out and announce them offline."""
    while not _app.is_stopped():
        for peer in _app.expire_stale_peers(PEER_TIMEOUT):
            _ui.print_safe(f"{peer.name} is offline")
        _app.stop_event.wait(1)


def send_hello_now() -> None:
    """Send one HELLO immediately (used when the status changes)."""
    packet = make_hello(_app.self_name, _app.self_tcp_port, _app.get_status())
    try:
        _sock.sendto(packet, (BROADCAST_ADDR, PORT))
    except OSError:
        if not _app.is_stopped():
            _ui.print_safe(format_error("Không gửi được HELLO, kiểm tra kết nối mạng"))


def cmd_status(arg: str) -> None:
    """Handle /status online|away."""
    new_status = arg.strip()
    if new_status not in VALID_STATUSES:
        _ui.print_safe(format_error("Cách dùng: /status online|away"))
        return
    if new_status == _app.get_status():
        _ui.print_safe(f"Bạn đang ở trạng thái {new_status} rồi")
        return
    _app.set_status(new_status)
    send_hello_now()
    _ui.print_safe(f"Đã đổi trạng thái thành {new_status}")


def cmd_list(arg: str) -> None:
    """Handle /list."""
    peers = _app.list_peers()
    if not peers:
        _ui.print_safe("Chưa có ai online")
        return
    for peer in peers:
        _ui.print_safe(f"{peer.name:<16} {peer.status:<7} {peer.ip}:{peer.tcp_port}")


if __name__ == "__main__":
    import sys

    app = AppState(sys.argv[1], int(sys.argv[2]))
    ui = ConsoleUI(app)
    if not start(app, ui):
        print(format_error(f"Tên '{app.self_name}' đã có người dùng. Hãy chọn tên khác."))
        sys.exit(1)

    COMMANDS = {"/list": cmd_list, "/status": cmd_status}
    while True:
        line = ui.read_line()
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        if line == "/quit":
            break
        command, _, arg = line.partition(" ")
        handler = COMMANDS.get(command)
        if handler is None:
            ui.print_safe(format_error("Lệnh: /list, /status online|away, /quit"))
            continue
        handler(arg)

    app.request_stop()
    stop()
