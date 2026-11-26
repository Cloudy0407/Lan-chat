import logging
import os
import traceback
from typing import Callable, Optional

logging.basicConfig(
    filename="lan_chat_error.log",
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


class LanChatError(Exception):
    """Lớp lỗi cơ sở của ứng dụng LAN Chat."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


class PeerOfflineError(LanChatError):
    """Mục 5.3: Kết nối tới người dùng đã offline hoặc không tồn tại."""

    def __init__(self, target: str):
        super().__init__(f"[!] Error: User '{target}' is offline or not found.")


class PeerDisconnectedError(LanChatError):
    """Mục 5.3: Đối phương đóng kết nối hoặc crash giữa chừng."""

    def __init__(self, peer_name: str):
        super().__init__(f"[*] {peer_name} disconnected. Returned to main prompt.")


class FileTransferInterruptedError(LanChatError):
    """Mục 5.3: Lỗi file không tồn tại hoặc quá trình truyền bị ngắt."""

    def __init__(self, reason: str, partial_path: Optional[str] = None):
        cleanup_partial_file(partial_path)
        super().__init__(f"[!] File transfer error: {reason}")


class InvalidCommandError(LanChatError):
    """Mục 5.3: Nhập sai lệnh hoặc thiếu tham số."""

    def __init__(self, short_help: str):
        super().__init__(f"[!] Invalid command. Usage: {short_help}")


def cleanup_partial_file(file_path: Optional[str]) -> bool:
    """
    Xóa tệp tin đang nhận dở nếu quá trình truyền bị gián đoạn (Mục 5.3).
    Trả về True nếu đã xóa thành công.
    """
    if not file_path:
        return False
    try:
        if os.path.exists(file_path) and os.path.isfile(file_path):
            os.remove(file_path)
            return True
    except OSError as e:
        logging.error(f"Failed to remove partial file '{file_path}': {e}")
    return False


def handle_error(
    err: Exception,
    printer: Optional[Callable[[str], None]] = None,
    partial_file_path: Optional[str] = None,
    context: str = "",
) -> str:
    """
    Hàm xử lý lỗi tập trung:
    - Dọn dẹp partial file nếu có.
    - Ghi log chi tiết ra file lan_chat_error.log.
    - Trả về chuỗi thông báo chuẩn format [*] / [!] của đề bài và in ra UI.
    """
    if partial_file_path:
        cleanup_partial_file(partial_file_path)

    logging.error(f"[{context}] {type(err).__name__}: {err}\n{traceback.format_exc()}")

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