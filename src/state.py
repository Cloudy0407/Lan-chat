import threading
import time

_lock = threading.Lock()
_peers = {}


def update_peer(name: str, ip: str, tcp_port: int, status: str) -> str:
    with _lock:
        if _peers.get(name) is None:
            _peers[name] = {
                "ip": ip,
                "tcp_port": tcp_port,
                "status": status,
                "last_seen": time.monotonic(),
            }
            return "new"
        user = _peers.get(name)
        if user["ip"] == ip and user["tcp_port"] == tcp_port:
            last_seen = time.monotonic()
            user["status"] = status
            user["last_seen"] = last_seen

            return "updated"
        return "conflict"


def get_peer(name: str) -> dict | None:
    with _lock:
        if _peers.get(name) is None:
            return None
        new_peer = _peers.get(name)
        return dict(new_peer)


def list_peers() -> list[dict]:

    with _lock:
        result = []
        for name, info in _peers.items():
            item = dict(info)
            item["name"] = name
            result.append(item)
    return result


def remove_stale_peers(timeout: float) -> list[str]:
    """Remove peers not heard from for more than `timeout` seconds. Return their names."""
    with _lock:
        item_remove = []
        now = time.monotonic()
        for key, val in _peers.items():
            if now - val["last_seen"] > timeout:
                item_remove.append(key)
        for name in item_remove:
            del _peers[name]
        return item_remove