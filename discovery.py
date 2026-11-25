import socket
import threading
import time

PORT = 9999
_sock: socket.socket | None = None
_name = ""
_tcp_port = 0
_status = "online"


def start(name: str, tcp_port: int) -> None:
    global _name, _sock, _tcp_port
    _name = name
    _tcp_port = tcp_port
    _sock = _make_socket()
    threading.Thread(target=_broadcaster, daemon=True).start()
    threading.Thread(target=_listener, daemon=True).start()


def _make_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", PORT))
    return sock


def _broadcaster() -> None:
    packet = make_hello(_name, _tcp_port, _status)
    while True:
        _sock.sendto(packet, ("255.255.255.255", PORT))
        time.sleep(3)


def make_hello(name: str, tcp_port: int, status: str) -> bytes:
    """Build a HELLO packet"""
    text = f"HELLO|{name}|{tcp_port}|{status}"
    return text.encode("utf-8")


def _listener() -> None:
    """Receive HELLO packets and print them"""
    while True:
        data, addr = _sock.recvfrom(1024)
        print(data, addr)


if __name__ == "__main__":
    import sys

    start(sys.argv[1], int(sys.argv[2]))
    try:
        input("Nhấn Enter để thoát\n")
    except KeyboardInterrupt:
        print()
