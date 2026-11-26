import threading
from typing import Callable, Dict, Optional
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from src.error_handler import InvalidCommandError, handle_error


class ConsoleUI:
    """
    Giao diện dòng lệnh (CLI) đáp ứng Mục 4.1, 4.2 và 4.3:
    - Sử dụng prompt_toolkit + patch_stdout để giữ nguyên text đang gõ khi có tin nhắn đến.
    - Bảo vệ luồng xuất màn hình bằng threading.Lock.
    - Điều phối đầy đủ 8 lệnh chuẩn của đề bài.
    """

    def __init__(self, username: str):
        self.username = username
        self._print_lock = threading.Lock()
        self._session = PromptSession()
        self._running = False
        self._current_partner: Optional[str] = None
        self._handlers: Dict[str, Callable] = {}

    def register_handler(self, command_name: str, func: Callable) -> None:
        """Liên kết hàm xử lý từ Vai A và Vai B vào giao diện."""
        self._handlers[command_name.lower()] = func

    def set_partner(self, partner_name: Optional[str]) -> None:
        """Cập nhật người đang chat hiện tại (gọi khi /chat thành công hoặc /leave)."""
        with self._print_lock:
            self._current_partner = partner_name

    def get_partner(self) -> Optional[str]:
        with self._print_lock:
            return self._current_partner

    def display_message(self, text: str) -> None:
        """
        Thread-safe printer:
        Khi patch_stdout() đang bật, lệnh print() bên trong lock này sẽ tự động:
        1. Xóa dòng prompt hiện tại.
        2. In tin nhắn mới lên trên.
        3. Vẽ lại '> ' cùng toàn bộ ký tự user đang gõ dở.
        """
        with self._print_lock:
            print(text, flush=True)

    def show_help(self) -> None:
        """Hiển thị danh sách lệnh chuẩn theo bảng Mục 4.1."""
        help_msg = (
            "Available commands:\n"
            "  /list                  - Show online users (number, name, IP, status)\n"
            "  /chat <name|number>    - Open a chat with the selected user\n"
            "  <any text>             - Send a message to the current chat partner\n"
            "  /send <file>           - Send a file to the current chat partner\n"
            "  /leave                 - Close the current chat\n"
            "  /status <online|away>  - Change your status (sent via UDP)\n"
            "  /help                  - Show all commands\n"
            "  /quit                  - Exit the program"
        )
        self.display_message(help_msg)

    def process_input(self, raw_line: str) -> None:
        """Phân tích lệnh bắt đầu bằng '/' hoặc gửi tin nhắn văn bản."""
        line = raw_line.strip()
        if not line:
            return

        if line.startswith("/"):
            parts = line.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""

            if cmd == "/help":
                self.show_help()

            elif cmd == "/list":
                self._invoke("list")

            elif cmd == "/chat":
                if not arg:
                    raise InvalidCommandError("/chat <name|number>")
                self._invoke("chat", arg)

            elif cmd == "/send":
                if not arg:
                    raise InvalidCommandError("/send <file>")
                if not self.get_partner():
                    raise InvalidCommandError("Select a partner first using /chat <name|number>")
                self._invoke("send", arg)

            elif cmd == "/leave":
                if not self.get_partner():
                    self.display_message("[*] You are not in any active chat.")
                else:
                    self._invoke("leave")
                    self.set_partner(None)

            elif cmd == "/status":
                if arg.lower() not in ("online", "away"):
                    raise InvalidCommandError("/status <online|away>")
                self._invoke("status", arg.lower())

            elif cmd == "/quit":
                self._running = False
                self._invoke("quit")

            else:
                raise InvalidCommandError("Unknown command. Type /help for all commands.")
        else:
            # Văn bản thông thường -> Gửi tin nhắn tới current chat partner
            if not self.get_partner():
                raise InvalidCommandError(
                    "No active chat. Use /list to see users and /chat <name|number> to start."
                )
            self._invoke("message", raw_line)

    def _invoke(self, action: str, *args) -> None:
        handler = self._handlers.get(action)
        if handler:
            handler(*args)
        else:
            self.display_message(f"[!] Handler for '{action}' is not connected.")

    def run(self) -> None:
        """Vòng lặp chính chạy trên Main Thread."""
        self._running = True
        with patch_stdout(raw=True):
            while self._running:
                try:
                    # Chuẩn '> ' theo Mục 4.2 & 4.3 của đề bài
                    user_input = self._session.prompt("> ")
                    self.process_input(user_input)
                except KeyboardInterrupt:
                    self.display_message("[*] Use /quit to exit properly.")
                except EOFError:
                    self._running = False
                    self._invoke("quit")
                except Exception as err:
                    handle_error(err, printer=self.display_message, context="CONSOLE_UI")