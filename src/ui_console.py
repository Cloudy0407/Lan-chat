"""Thread-safe prompt_toolkit console UI with live transfer progress toolbar."""

from __future__ import annotations

import threading

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.patch_stdout import patch_stdout

from error_handler import HELP_TEXT
from state import AppState


class ConsoleUI:
    """Console interface backed by AppState and prompt_toolkit."""

    def __init__(self, state: AppState) -> None:
        self.state = state
        self._io_lock = threading.Lock()
        self._session: PromptSession[str] | None = None

    def _prompt_text(self) -> str:
        partner = self.state.get_current_partner()
        return f"[{partner}] > " if partner else "> "

    def _bottom_toolbar(self) -> FormattedText:
        partner = self.state.get_current_partner()
        status = self.state.get_status()
        partner_label = partner if partner else "none"
        items: list[tuple[str, str]] = [
            ("class:toolbar", f" [{self.state.self_name} | {status} | chat: {partner_label}] ")
        ]
        for tr in self.state.get_active_transfers():
            arrow = "->" if tr.direction == "send" else "<-"
            items.append(
                (
                    "class:toolbar",
                    f"| {tr.filename} ({arrow} {tr.peer_name}): {tr.percent:5.1f}% ",
                )
            )
        return FormattedText(items)

    def ensure_session(self) -> PromptSession[str]:
        with self._io_lock:
            if self._session is None:
                self._session = PromptSession(
                    bottom_toolbar=self._bottom_toolbar,
                    refresh_interval=0.25,
                )
            return self._session

    def invalidate(self) -> None:
        """Request a prompt/toolbar redraw if a session is active."""
        with self._io_lock:
            session = self._session
        if session is not None:
            try:
                session.app.invalidate()
            except Exception:
                pass

    def print_safe(self, message: str) -> None:
        """Print a message without breaking the active input line."""
        with self._io_lock:
            print(message, flush=True)
            session = self._session
        if session is not None:
            try:
                session.app.invalidate()
            except Exception:
                pass

    def display_message(self, text: str) -> None:
        """Alias for print_safe()."""
        self.print_safe(text)

    def print_chat_message(self, sender: str, text: str) -> None:
        self.print_safe(f"[{sender}] {text}")

    def show_help(self) -> None:
        self.print_safe(HELP_TEXT)

    def get_partner(self) -> str | None:
        return self.state.get_current_partner()

    def set_partner(self, partner_name: str | None) -> None:
        self.state.set_current_partner(partner_name)
        self.invalidate()

    def read_line(self) -> str | None:
        """Read one input line; return None on Ctrl+C or EOF."""
        session = self.ensure_session()
        try:
            with patch_stdout(raw=True):
                return session.prompt(self._prompt_text)
        except (KeyboardInterrupt, EOFError):
            return None
