from __future__ import annotations

import logging
import os
import traceback
from collections.abc import Callable

HELP_TEXT = """Commands:
  /list                  Show online peers
  /chat <name|number>    Open or switch chat with a peer
  <any text>             Send chat message to current partner
  /send <file_path>      Send file to current partner
  /leave                 Leave current chat (send BYE|)
  /status <online|away>  Change presence status and broadcast immediately
  /help                  Show this help message
  /quit                  Close connections and exit"""

_LOGGER_NAME = "lan_chat.errors"


def _get_error_logger() -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        handler = logging.FileHandler("lan_chat_error.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.ERROR)
        logger.propagate = False
    return logger


def format_error(message: str, *, with_help: bool = False) -> str:
    if with_help:
        return f"[!] {message}\n{HELP_TEXT}"
    return f"[!] {message}"


class LanChatError(Exception):
    """Lớp lỗi cơ sở của ứng dụng LAN Chat."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(self.message)


class PeerOfflineError(LanChatError):
    """Mục 5.3: Kết nối tới người dùng đã offline hoặc không tồn tại."""

    def __init__(self, target: str) -> None:
        super().__init__(f"[!] Error: User '{target}' is offline or not found.")


class PeerDisconnectedError(LanChatError):
    """Mục 5.3: Đối phương đóng kết nối hoặc crash giữa chừng."""

    def __init__(self, peer_name: str) -> None:
        super().__init__(f"[*] {peer_name} disconnected. Returned to main prompt.")


class FileTransferInterruptedError(LanChatError):
    """Mục 5.3: Lỗi file không tồn tại hoặc quá trình truyền bị ngắt."""

    def __init__(self, reason: str, partial_path: str | None = None) -> None:
        cleanup_partial_file(partial_path)
        super().__init__(f"[!] File transfer error: {reason}")


class InvalidCommandError(LanChatError):
    """Mục 5.3: Nhập sai lệnh hoặc thiếu tham số."""

    def __init__(self, short_help: str) -> None:
        super().__init__(f"[!] Invalid command. Usage: {short_help}")


def cleanup_partial_file(file_path: str | None) -> bool:
    """Xóa tệp tin đang nhận dở nếu quá trình truyền bị gián đoạn."""
    if not file_path:
        return False
    try:
        if os.path.exists(file_path) and os.path.isfile(file_path):
            os.remove(file_path)
            return True
    except OSError as exc:
        _get_error_logger().error("Failed to remove partial file '%s': %s", file_path, exc)
    return False


def handle_error(
    err: Exception,
    printer: Callable[[str], None] | None = None,
    partial_file_path: str | None = None,
    context: str = "",
) -> str:
    """Dọn partial file, ghi log lỗi, định dạng thông báo và in ra UI."""
    if partial_file_path:
        cleanup_partial_file(partial_file_path)

    _get_error_logger().error(
        "[%s] %s: %s\n%s",
        context,
        type(err).__name__,
        err,
        traceback.format_exc(),
    )

    if isinstance(err, LanChatError):
        msg = err.message
    elif isinstance(err, FileNotFoundError):
        msg = "[!] Error: File not found."
    elif isinstance(err, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
        msg = "[!] Connection lost: Peer closed connection or crashed."
    elif isinstance(err, TimeoutError):
        msg = "[!] Error: Connection timed out."
    else:
        msg = f"[!] Unexpected error: {err}"

    if printer:
        printer(msg)
    return msg
