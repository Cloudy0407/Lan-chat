import socket
import threading
import time

import state

PORT = 9999
BROADCAST_ADDR = "255.255.255.255"
HELLO_INTERVAL = 3
PEER_TIMEOUT = 10
_sock: socket.socket | None = None
_name = ""
_tcp_port = 0
_status = "online"
_warned: set[tuple[str, str, int]] = set()


def start(name: str, tcp_port: int) -> None:
    global _name, _sock, _tcp_port
    _name = name
    _tcp_port = tcp_port
    _sock = _make_socket()
    threading.Thread(target=_broadcaster, daemon=True).start()
    threading.Thread(target=_listener, daemon=True).start()
    threading.Thread(target=_cleaner, daemon=True).start()


def _make_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", PORT))
    return sock


def _broadcaster() -> None:
    """Send HELLO every HELLO_INTERVAL seconds."""
    while True:
        packet = make_hello(_name, _tcp_port, _status)
        _sock.sendto(packet, (BROADCAST_ADDR, PORT))
        time.sleep(HELLO_INTERVAL)


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

    if status not in ("online", "away"):
        return None

    return name, tcp_port, status


def _warn_once(name: str, ip: str, tcp_port: int, message: str) -> None:
    """Print a warning only the first time a given source causes it."""
    key = (name, ip, tcp_port)
    if key in _warned:
        return
    _warned.add(key)
    print(message)


def _listener() -> None:
    """Receive HELLO packets and update the peer table."""
    while True:
        data, addr = _sock.recvfrom(1024)
        hello = parse_hello(data)

        if hello is None:
            continue

        name, tcp_port, status = hello
        ip = addr[0]

        if name == _name:
            if tcp_port != _tcp_port:
                _warn_once(
                    name,
                    ip,
                    tcp_port,
                    f"[!] Có người khác đang dùng tên '{name}' ({ip}:{tcp_port})",
                )
            continue

        result = state.update_peer(name, ip, tcp_port, status)
        if result == "new":
            print(f"{name} is online")
        elif result == "conflict":
            _warn_once(
                name,
                ip,
                tcp_port,
                f"[!] Tên '{name}' đã có người dùng, bỏ qua gói từ {ip}:{tcp_port}",
            )


def _cleaner() -> None:
    """Every second, remove peers that timed out and announce them offline."""
    while True:
        remove_list = state.remove_stale_peers(PEER_TIMEOUT)
        for name in remove_list:
            print(f"{name} is offline")
        time.sleep(1)


def send_hello_now() -> None:
    """Send one HELLO immediately (used when the status changes)."""
    packet = make_hello(_name, _tcp_port, _status)
    _sock.sendto(packet, ("255.255.255.255", PORT))


def cmd_status(arg: str) -> None:
    """Handle /status online|away."""
    global _status
    new_status = arg.strip()
    if new_status not in ("online", "away"):
        print("Cách dùng: /status online|away")
        return
    if new_status == _status:
        print(f"Bạn đang ở trạng thái {_status} rồi")
        return
    _status = new_status
    send_hello_now()
    print(f"Đã đổi trạng thái thành {_status}")


def cmd_list(arg: str) -> None:
    """Handle /list."""
    peers = state.list_peers()
    if not peers:
        print("Chưa có ai online")
        return
    for peer in peers:
        print(f"{peer['name']:<16} {peer['status']:<7} {peer['ip']}:{peer['tcp_port']}")


if __name__ == "__main__":
    import sys

    COMMANDS = {
        "/list": cmd_list,
        "/status": cmd_status,
    }

    start(sys.argv[1], int(sys.argv[2]))
    try:
        while True:
            line = input("> ").strip()
            if not line:
                continue
            if line == "/quit":
                break
            command, _, arg = line.partition(" ")
            handler = COMMANDS.get(command)
            if handler is None:
                print("Lệnh: /list, /status online|away, /quit")
                continue
            handler(arg)
    except KeyboardInterrupt:
        print()
