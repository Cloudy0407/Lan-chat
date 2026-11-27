"""Thread-safe shared state for LAN Chat P2P (Role C)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class PeerInfo:
    name: str
    ip: str
    tcp_port: int
    status: str = "online"
    last_seen: float = field(default_factory=time.monotonic)


@dataclass
class ChatMessage:
    sender: str
    text: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class TransferProgress:
    transfer_id: str
    direction: str  # "send" | "receive"
    peer_name: str
    filename: str
    transferred_bytes: int
    total_bytes: int
    status: str = "in_progress"
    error: str | None = None

    @property
    def percent(self) -> float:
        if self.total_bytes <= 0:
            return 100.0 if self.status == "completed" else 0.0
        return min(100.0, (self.transferred_bytes / self.total_bytes) * 100.0)


class AppState:
    def __init__(self, self_name: str, self_tcp_port: int, status: str = "online") -> None:
        self.self_name = self_name
        self.self_tcp_port = self_tcp_port
        self._lock = threading.Lock()
        self._status = status
        self._peers: dict[str, PeerInfo] = {}
        self._current_partner: str | None = None
        self._histories: dict[str, list[ChatMessage]] = {}
        self._unread: dict[str, int] = {}
        self._chat_sockets: dict[str, object] = {}
        self._transfers: dict[str, TransferProgress] = {}
        self._stop_event = threading.Event()

    @property
    def stop_event(self) -> threading.Event:
        return self._stop_event

    def request_stop(self) -> None:
        self._stop_event.set()

    def is_stopped(self) -> bool:
        return self._stop_event.is_set()

    def get_status(self) -> str:
        with self._lock:
            return self._status

    def set_status(self, status: str) -> None:
        with self._lock:
            self._status = status

    def get_current_partner(self) -> str | None:
        with self._lock:
            return self._current_partner

    def set_current_partner(self, partner: str | None) -> list[ChatMessage]:
        with self._lock:
            self._current_partner = partner
            if partner is not None:
                self._unread[partner] = 0
                return list(self._histories.get(partner, []))
            return []

    def upsert_peer(
        self, name: str, ip: str, tcp_port: int, status: str
    ) -> tuple[str, PeerInfo | None]:
        now = time.monotonic()
        with self._lock:
            existing = self._peers.get(name)
            if existing is None:
                peer = PeerInfo(name=name, ip=ip, tcp_port=tcp_port, status=status, last_seen=now)
                self._peers[name] = peer
                self._histories.setdefault(name, [])
                self._unread.setdefault(name, 0)
                return ("added", peer)
            if existing.ip != ip or existing.tcp_port != tcp_port:
                return ("conflict", existing)
            existing.status = status
            existing.last_seen = now
            return ("updated", existing)

    def get_peer(self, name: str) -> PeerInfo | None:
        with self._lock:
            p = self._peers.get(name)
            if p is None:
                return None
            return PeerInfo(
                name=p.name, ip=p.ip, tcp_port=p.tcp_port, status=p.status, last_seen=p.last_seen
            )

    def list_peers(self) -> list[PeerInfo]:
        with self._lock:
            return [
                PeerInfo(
                    name=p.name,
                    ip=p.ip,
                    tcp_port=p.tcp_port,
                    status=p.status,
                    last_seen=p.last_seen,
                )
                for p in self._peers.values()
            ]

    def expire_stale_peers(self, timeout_seconds: float = 10.0) -> list[PeerInfo]:
        now = time.monotonic()
        expired: list[PeerInfo] = []
        with self._lock:
            stale = [
                name for name, p in self._peers.items() if (now - p.last_seen) > timeout_seconds
            ]
            for name in stale:
                expired.append(self._peers.pop(name))
                if self._current_partner == name:
                    self._current_partner = None
        return expired

    def record_message(self, peer_name: str, sender: str, text: str) -> tuple[bool, int]:
        with self._lock:
            self._histories.setdefault(peer_name, []).append(ChatMessage(sender=sender, text=text))
            if self._current_partner == peer_name:
                self._unread[peer_name] = 0
                return (True, 0)
            if sender != self.self_name:
                self._unread[peer_name] = self._unread.get(peer_name, 0) + 1
            return (False, self._unread.get(peer_name, 0))

    def register_chat_socket(self, peer_name: str, sock: object) -> None:
        with self._lock:
            self._chat_sockets[peer_name] = sock

    def get_chat_socket(self, peer_name: str) -> object | None:
        with self._lock:
            return self._chat_sockets.get(peer_name)

    def remove_chat_socket(self, peer_name: str) -> object | None:
        with self._lock:
            sock = self._chat_sockets.pop(peer_name, None)
            if self._current_partner == peer_name:
                self._current_partner = None
            return sock

    def all_chat_sockets(self) -> list[tuple[str, object]]:
        with self._lock:
            return list(self._chat_sockets.items())

    def update_transfer(self, progress: TransferProgress) -> None:
        with self._lock:
            self._transfers[progress.transfer_id] = TransferProgress(
                transfer_id=progress.transfer_id,
                direction=progress.direction,
                peer_name=progress.peer_name,
                filename=progress.filename,
                transferred_bytes=progress.transferred_bytes,
                total_bytes=progress.total_bytes,
                status=progress.status,
                error=progress.error,
            )

    def remove_transfer(self, transfer_id: str) -> None:
        with self._lock:
            self._transfers.pop(transfer_id, None)

    def get_active_transfers(self) -> list[TransferProgress]:
        with self._lock:
            return [t for t in self._transfers.values() if t.status == "in_progress"]
