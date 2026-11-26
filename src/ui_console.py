
from __future__ import annotations

import threading

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted import FormattedText
from prompt_toolkit.patch_stdout import patch_stdout

from state import AppState


class ConsoleUI:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self._io_lock = threading.Lock()
        self._session: PromptSession[str] | None = None

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
                ("class:toolbar", f"| {tr.filename} ({arrow} {tr.peer_name}): {tr.percent:5.1f}% ")
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

    def print_safe(self, message: str) -> None:
        with self._io_lock:
            if self._session is not None:
                with patch_stdout(raw=True):
                    print(message, flush=True)
                    try:
                        self._session.app.invalidate()
                    except Exception:
                        pass
            else:
                print(message, flush=True)

    def print_chat_message(self, sender: str, text: str) -> None:
        self.print_safe(f"[{sender}] {text}")

    def read_line(self) -> str | None:
        session = self.ensure_session()
        partner = self.state.get_current_partner()
        prompt_str = f"[{partner}] > " if partner else "> "
        try:
            with patch_stdout(raw=True):
                return session.prompt(prompt_str)
        except (KeyboardInterrupt, EOFError):
            return None
