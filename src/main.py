"""Entry point and CLI command dispatcher for LAN Chat & File Sharing (P2P)."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path

import discovery
from chat import ChatManager
from error_handler import HELP_TEXT, format_error
from framing import MAX_PORT, MIN_PORT, FramingError, validate_name
from state import AppState
from ui_console import ConsoleUI


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="P2P LAN Chat & File Sharing")
    parser.add_argument(
        "--name",
        required=True,
        help="Display name (1-32 UTF-8 bytes, no '|' or double spaces)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="TCP listening port (1-65535, or 0 for OS-assigned port)",
    )
    parser.add_argument(
        "--udp-port",
        type=int,
        default=discovery.PORT,
        help="UDP discovery port (default: 9999)",
    )
    parser.add_argument(
        "--bcast",
        default=discovery.BROADCAST_ADDR,
        help="UDP broadcast address (use 127.255.255.255 for offline single-machine demo)",
    )
    parser.add_argument(
        "--downloads",
        default="downloads",
        help="Directory for received files (default: downloads)",
    )
    return parser.parse_args(argv)


def validate_cli_args(args: argparse.Namespace) -> tuple[str, int, int, str, Path]:
    """Validate CLI options against protocol rules and return normalized values."""
    # Decision 13: trim leading/trailing whitespace from username on startup.
    name = validate_name(args.name.strip())

    if not (0 <= args.port <= MAX_PORT):
        raise ValueError(f"TCP port must be in [0, {MAX_PORT}], got {args.port}")
    if not (MIN_PORT <= args.udp_port <= MAX_PORT):
        raise ValueError(f"UDP port must be in [{MIN_PORT}, {MAX_PORT}], got {args.udp_port}")

    bcast = args.bcast.strip()
    if not bcast:
        raise ValueError("Broadcast address cannot be empty")

    download_dir = Path(args.downloads)
    return name, args.port, args.udp_port, bcast, download_dir


def _unquote_filepath(raw_path: str) -> str:
    """Strip matching outer quotes commonly added by Windows 'Copy as path'."""
    cleaned = raw_path.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] == '"':
        return cleaned[1:-1].strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] == "'" and not Path(cleaned).is_file():
        return cleaned[1:-1].strip()
    return cleaned


class CommandDispatcher:
    """Parse and execute CLI commands and chat messages against AppState and ChatManager."""

    def __init__(
        self,
        state: AppState,
        chat: ChatManager,
        ui: ConsoleUI,
        *,
        send_hello: Callable[[], None] = discovery.send_hello_now,
    ) -> None:
        self.state = state
        self.chat = chat
        self.ui = ui
        self._send_hello = send_hello
        self._last_listed_names: list[str] = []

    def handle_line(self, raw_line: str) -> bool:
        """Process one input line. Return False when the user requests /quit."""
        line = raw_line.strip()
        if not line:
            return True

        if not line.startswith("/"):
            self.chat.send_message(line)
            return True

        # Decision 13: preserve spaces in arguments (peer names and file paths).
        cmd_part, _, arg_part = line.partition(" ")
        cmd = cmd_part.lower()
        arg = arg_part.strip()

        if cmd == "/quit":
            if arg:
                self.ui.print_safe(format_error("Usage: /quit"))
                return True
            return False

        if cmd == "/help":
            if arg:
                self.ui.print_safe(format_error("Usage: /help"))
            else:
                self.ui.print_safe(HELP_TEXT)
            return True

        if cmd == "/list":
            if arg:
                self.ui.print_safe(format_error("Usage: /list"))
            else:
                self._handle_list()
            return True

        if cmd == "/chat":
            if not arg:
                self.ui.print_safe(format_error("Usage: /chat <name|number>"))
            else:
                self._handle_chat(arg)
            return True

        if cmd == "/send":
            filepath = _unquote_filepath(arg)
            if not filepath:
                self.ui.print_safe(format_error("Usage: /send <file_path>"))
            else:
                self.chat.send_file_to_partner(filepath)
            return True

        if cmd == "/leave":
            if arg:
                self.ui.print_safe(format_error("Usage: /leave"))
            else:
                self.chat.leave_chat()
            return True

        if cmd == "/status":
            self._handle_status(arg)
            return True

        self.ui.print_safe(format_error(f"Unknown command: {cmd}", with_help=True))
        return True

    def _handle_list(self) -> None:
        peers = self.state.list_peers()
        self._last_listed_names = [peer.name for peer in peers]
        if not peers:
            self.ui.print_safe("[*] No other users online.")
            return

        current = self.state.get_current_partner()
        lines: list[str] = []
        for idx, peer in enumerate(peers, start=1):
            active_tag = " *" if peer.name == current else ""
            lines.append(
                f"{idx}. {peer.name} ({peer.ip}:{peer.tcp_port}) {peer.status}{active_tag}"
            )
        self.ui.print_safe("\n".join(lines))

    def _handle_chat(self, target: str) -> None:
        peer_name = self._resolve_peer_name(target)
        if peer_name is None:
            return
        self.chat.open_chat(peer_name)

    def _resolve_peer_name(self, target: str) -> str | None:
        # Exact username takes precedence (supports numeric usernames if any).
        if self.state.get_peer(target) is not None or self.chat.is_connected(target):
            return target

        if target.isascii() and target.isdigit():
            idx = int(target)
            candidates = self._last_listed_names or [p.name for p in self.state.list_peers()]
            if not candidates:
                self.ui.print_safe(format_error("No other users online. Use /list to check."))
                return None
            if 1 <= idx <= len(candidates):
                return candidates[idx - 1]
            self.ui.print_safe(
                format_error(f"Invalid peer number: {target}. Use /list to see online peers.")
            )
            return None

        return target

    def _handle_status(self, arg: str) -> None:
        new_status = arg.lower()
        if new_status not in discovery.VALID_STATUSES:
            self.ui.print_safe(format_error("Usage: /status <online|away>"))
            return

        if new_status == self.state.get_status():
            self.ui.print_safe(f"[*] Status is already {new_status}.")
            return

        self.state.set_status(new_status)
        self._send_hello()
        self.ui.print_safe(f"[*] Status changed to {new_status}.")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the LAN Chat CLI application and return a process exit code."""
    args = parse_args(argv)
    try:
        name, tcp_port, udp_port, bcast, download_dir = validate_cli_args(args)
    except (FramingError, ValueError) as exc:
        print(format_error(str(exc)))
        return 1

    discovery.PORT = udp_port
    discovery.BROADCAST_ADDR = bcast

    state = AppState(self_name=name, self_tcp_port=tcp_port)
    ui = ConsoleUI(state)
    chat = ChatManager(state, print_safe=ui.print_safe, download_dir=download_dir)

    try:
        bound_port = chat.start()
    except OSError as exc:
        ui.print_safe(format_error(f"Could not listen on TCP port {tcp_port}: {exc}"))
        return 1

    try:
        try:
            name_ok = discovery.start(state, ui)
        except OSError as exc:
            ui.print_safe(format_error(f"Could not start UDP discovery on port {udp_port}: {exc}"))
            return 1
        except KeyboardInterrupt:
            return 0

        if not name_ok:
            ui.print_safe(
                format_error(f"Name '{name}' is already in use on the LAN. Choose another name.")
            )
            return 1

        ui.print_safe(f"[*] Listening on TCP port {bound_port}")
        ui.print_safe(f"[*] Broadcasting presence on UDP port {udp_port} ({bcast})")
        ui.print_safe("[*] Type /help for available commands.")

        dispatcher = CommandDispatcher(state, chat, ui)
        while not state.is_stopped():
            raw_line = ui.read_line()
            if raw_line is None:
                break
            if not dispatcher.handle_line(raw_line):
                break
        return 0
    finally:
        state.request_stop()
        discovery.stop()
        chat.stop()
        ui.print_safe("[*] Bye!")


if __name__ == "__main__":
    raise SystemExit(main())
