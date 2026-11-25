
from __future__ import annotations

HELP_TEXT = """Commands:
  /list                  Show online peers
  /chat <name|number>    Open or switch chat with a peer
  <any text>             Send chat message to current partner
  /send <file_path>      Send file to current partner
  /leave                 Leave current chat (send BYE|)
  /status <online|away>  Change presence status and broadcast immediately
  /help                  Show this help message
  /quit                  Close connections and exit"""


def format_error(message: str, *, with_help: bool = False) -> str:
    if with_help:
        return f"[!] {message}\n{HELP_TEXT}"
    return f"[!] {message}"
