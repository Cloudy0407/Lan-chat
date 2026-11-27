"""Unit and integration tests for CLI parsing, CommandDispatcher, and main()."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import discovery
import main as main_module
from error_handler import HELP_TEXT, format_error
from framing import FramingError
from main import CommandDispatcher, _unquote_filepath, parse_args, validate_cli_args
from state import AppState


@pytest.fixture(autouse=True)
def _restore_discovery_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent main() mutations of discovery globals from leaking across tests."""
    monkeypatch.setattr(discovery, "PORT", discovery.PORT)
    monkeypatch.setattr(discovery, "BROADCAST_ADDR", discovery.BROADCAST_ADDR)


class _FakeUI:
    """In-memory stand-in for ConsoleUI (avoids prompt_toolkit terminal I/O)."""

    def __init__(self, state: AppState, inputs: list[str | None] | None = None) -> None:
        self.state = state
        self.messages: list[str] = []
        self._inputs: Iterator[str | None] = iter(inputs or [])
        self.read_calls = 0

    def print_safe(self, message: str) -> None:
        self.messages.append(message)

    def read_line(self) -> str | None:
        self.read_calls += 1
        return next(self._inputs, None)


class _FakeChat:
    """Minimal ChatManager spy for testing CommandDispatcher and main()."""

    def __init__(
        self,
        state: AppState,
        *,
        print_safe: object = None,
        download_dir: str | Path = "downloads",
        bound_port: int = 5001,
        start_error: Exception | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.state = state
        self.print_safe = print_safe
        self.download_dir = Path(download_dir)
        self._bound_port = bound_port
        self._start_error = start_error
        self._events = events if events is not None else []
        self.connected_peers: set[str] = set()
        self.opened: list[str] = []
        self.sent_messages: list[str] = []
        self.sent_files: list[str] = []
        self.leave_count = 0
        self.started = False
        self.stopped = False

    def start(self) -> int:
        self._events.append("chat.start")
        if self._start_error is not None:
            raise self._start_error
        self.started = True
        self.state.self_tcp_port = self._bound_port
        return self._bound_port

    def stop(self) -> None:
        self._events.append("chat.stop")
        self.stopped = True
        self.state.request_stop()

    def is_connected(self, peer_name: str) -> bool:
        return peer_name in self.connected_peers

    def open_chat(self, peer_name: str) -> bool:
        self.opened.append(peer_name)
        return True

    def send_message(self, text: str) -> bool:
        self.sent_messages.append(text)
        return True

    def leave_chat(self) -> bool:
        self.leave_count += 1
        return True

    def send_file_to_partner(self, filepath: str | Path) -> None:
        self.sent_files.append(str(filepath))


# --- Argument parsing & validation ---


def test_parse_args_defaults_and_custom_values() -> None:
    default_args = parse_args(["--name", "Duy"])
    assert default_args.name == "Duy"
    assert default_args.port == 5000
    assert default_args.udp_port == discovery.PORT
    assert default_args.bcast == discovery.BROADCAST_ADDR
    assert default_args.downloads == "downloads"

    custom_args = parse_args(
        [
            "--name",
            "Duy Khang",
            "--port",
            "5005",
            "--udp-port",
            "10000",
            "--bcast",
            "127.255.255.255",
            "--downloads",
            "custom_dl",
        ]
    )
    assert custom_args.name == "Duy Khang"
    assert custom_args.port == 5005
    assert custom_args.udp_port == 10000
    assert custom_args.bcast == "127.255.255.255"
    assert custom_args.downloads == "custom_dl"

    with pytest.raises(SystemExit) as exc_info:
        parse_args([])
    assert exc_info.value.code == 2


def test_validate_cli_args_valid_and_boundary_values() -> None:
    args_low = parse_args(
        [
            "--name",
            "  Duy Khang  ",
            "--port",
            "0",
            "--udp-port",
            "1",
            "--bcast",
            "  127.255.255.255  ",
            "--downloads",
            "my_files",
        ]
    )
    name, tcp_port, udp_port, bcast, download_dir = validate_cli_args(args_low)
    assert name == "Duy Khang"
    assert tcp_port == 0
    assert udp_port == 1
    assert bcast == "127.255.255.255"
    assert download_dir == Path("my_files")

    args_high = parse_args(["--name", "Khang", "--port", "65535", "--udp-port", "65535"])
    assert validate_cli_args(args_high)[:3] == ("Khang", 65535, 65535)


@pytest.mark.parametrize(
    ("argv", "expected_exc"),
    [
        (["--name", ""], FramingError),
        (["--name", "   "], FramingError),
        (["--name", "Duy|Khang"], FramingError),
        (["--name", "Duy  Khang"], FramingError),
        (["--name", "Duy\nKhang"], FramingError),
        (["--name", "a" * 33], FramingError),
        (["--name", "Duy", "--port", "-1"], ValueError),
        (["--name", "Duy", "--port", "65536"], ValueError),
        (["--name", "Duy", "--udp-port", "0"], ValueError),
        (["--name", "Duy", "--udp-port", "-5"], ValueError),
        (["--name", "Duy", "--udp-port", "65536"], ValueError),
        (["--name", "Duy", "--bcast", "   "], ValueError),
    ],
)
def test_validate_cli_args_invalid_inputs(
    argv: list[str],
    expected_exc: type[Exception],
) -> None:
    args = parse_args(argv)
    with pytest.raises(expected_exc):
        validate_cli_args(args)


def test_unquote_filepath_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _unquote_filepath('  "D:\\My Folder\\bao cao.pdf"  ') == "D:\\My Folder\\bao cao.pdf"
    assert _unquote_filepath('  "  spaced.txt  "  ') == "spaced.txt"
    assert _unquote_filepath('""') == ""
    assert _unquote_filepath('"   "') == ""
    assert _unquote_filepath("'nonexistent/path with spaces.txt'") == (
        "nonexistent/path with spaces.txt"
    )
    assert _unquote_filepath("\"mismatched'") == "\"mismatched'"
    assert _unquote_filepath('"') == '"'
    assert _unquote_filepath("  folder/my file.txt  ") == "folder/my file.txt"

    # Preserve literal single quotes when such a file actually exists on disk.
    monkeypatch.chdir(tmp_path)
    literal = tmp_path / "'literal.txt'"
    literal.write_text("ok", encoding="utf-8")
    assert _unquote_filepath("'literal.txt'") == "'literal.txt'"


# --- CommandDispatcher ---


def test_dispatcher_blank_and_chat_messages() -> None:
    state = AppState("Duy", 5001)
    ui = _FakeUI(state)
    chat = _FakeChat(state)
    dispatcher = CommandDispatcher(state, chat, ui, send_hello=lambda: None)

    assert dispatcher.handle_line("") is True
    assert dispatcher.handle_line("   \t  ") is True
    assert chat.sent_messages == []
    assert ui.messages == []

    assert dispatcher.handle_line("  Xin chào Khang!  ") is True
    assert chat.sent_messages == ["Xin chào Khang!"]


def test_dispatcher_quit_help_leave_and_unknown_commands() -> None:
    state = AppState("Duy", 5001)
    ui = _FakeUI(state)
    chat = _FakeChat(state)
    dispatcher = CommandDispatcher(state, chat, ui, send_hello=lambda: None)

    assert dispatcher.handle_line("/quit extra") is True
    assert ui.messages[-1] == format_error("Usage: /quit")
    assert dispatcher.handle_line("/QUIT") is False

    assert dispatcher.handle_line("/help extra") is True
    assert ui.messages[-1] == format_error("Usage: /help")
    assert dispatcher.handle_line("/HELP") is True
    assert ui.messages[-1] == HELP_TEXT

    assert dispatcher.handle_line("/leave extra") is True
    assert ui.messages[-1] == format_error("Usage: /leave")
    assert chat.leave_count == 0
    assert dispatcher.handle_line("/Leave") is True
    assert chat.leave_count == 1

    assert dispatcher.handle_line("/unknown arg") is True
    assert ui.messages[-1] == format_error("Unknown command: /unknown", with_help=True)


def test_dispatcher_list_empty_populated_active_and_extra_arg() -> None:
    state = AppState("Duy", 5001)
    ui = _FakeUI(state)
    chat = _FakeChat(state)
    dispatcher = CommandDispatcher(state, chat, ui, send_hello=lambda: None)

    assert dispatcher.handle_line("/list extra") is True
    assert ui.messages[-1] == format_error("Usage: /list")

    assert dispatcher.handle_line("/list") is True
    assert ui.messages[-1] == "[*] No other users online."

    state.upsert_peer("Khang", "192.168.1.10", 5002, "online")
    state.upsert_peer("Minh", "192.168.1.11", 5003, "away")
    state.set_current_partner("Minh")

    assert dispatcher.handle_line("/LIST") is True
    assert ui.messages[-1] == (
        "1. Khang (192.168.1.10:5002) online\n2. Minh (192.168.1.11:5003) away *"
    )


def test_dispatcher_chat_resolution_paths() -> None:
    state = AppState("Duy", 5001)
    ui = _FakeUI(state)
    chat = _FakeChat(state)
    dispatcher = CommandDispatcher(state, chat, ui, send_hello=lambda: None)

    # Missing argument.
    assert dispatcher.handle_line("/chat") is True
    assert ui.messages[-1] == format_error("Usage: /chat <name|number>")

    # Numeric index when no peers are online.
    assert dispatcher.handle_line("/chat 1") is True
    assert ui.messages[-1] == format_error("No other users online. Use /list to check.")
    assert chat.opened == []

    # Fallback to live peer list when /list has not been called yet.
    state.upsert_peer("Khang", "127.0.0.1", 5002, "online")
    state.upsert_peer("Minh", "127.0.0.1", 5003, "online")
    assert dispatcher.handle_line("/chat 2") is True
    assert chat.opened[-1] == "Minh"

    # Out-of-range indices (0 and too large).
    assert dispatcher.handle_line("/chat 0") is True
    assert ui.messages[-1] == format_error("Invalid peer number: 0. Use /list to see online peers.")
    assert dispatcher.handle_line("/chat 99") is True
    assert ui.messages[-1] == format_error(
        "Invalid peer number: 99. Use /list to see online peers."
    )

    # Snapshot from /list remains stable even if peer table changes afterwards.
    assert dispatcher.handle_line("/list") is True
    state.expire_stale_peers(timeout_seconds=-1.0)
    state.upsert_peer("NewPeer", "127.0.0.1", 5004, "online")
    assert dispatcher.handle_line("/chat 2") is True
    assert chat.opened[-1] == "Minh"

    # Exact numeric username takes precedence over index (via discovery or live socket).
    state.upsert_peer("1", "127.0.0.1", 5005, "online")
    assert dispatcher.handle_line("/chat 1") is True
    assert chat.opened[-1] == "1"

    chat.connected_peers.add("2")
    assert dispatcher.handle_line("/chat 2") is True
    assert chat.opened[-1] == "2"

    # Spaces in username, unknown names, and non-ASCII digits are forwarded directly.
    assert dispatcher.handle_line("/chat Duy Khang") is True
    assert chat.opened[-1] == "Duy Khang"

    assert dispatcher.handle_line("/chat ²") is True
    assert chat.opened[-1] == "²"

    assert dispatcher.handle_line("/chat Ghost") is True
    assert chat.opened[-1] == "Ghost"


def test_dispatcher_send_file_validation_and_unquoting() -> None:
    state = AppState("Duy", 5001)
    ui = _FakeUI(state)
    chat = _FakeChat(state)
    dispatcher = CommandDispatcher(state, chat, ui, send_hello=lambda: None)

    for bad_cmd in ("/send", "/send   ", '/send ""', '/send "   "'):
        assert dispatcher.handle_line(bad_cmd) is True
        assert ui.messages[-1] == format_error("Usage: /send <file_path>")
    assert chat.sent_files == []

    assert dispatcher.handle_line('/SEND "D:\\My Folder\\bao cao.pdf"') is True
    assert dispatcher.handle_line("/send docs/my slides.pdf") is True
    assert chat.sent_files == ["D:\\My Folder\\bao cao.pdf", "docs/my slides.pdf"]


def test_dispatcher_status_transitions_and_validation() -> None:
    state = AppState("Duy", 5001)
    ui = _FakeUI(state)
    chat = _FakeChat(state)
    hello_calls = 0

    def _send_hello() -> None:
        nonlocal hello_calls
        hello_calls += 1

    dispatcher = CommandDispatcher(state, chat, ui, send_hello=_send_hello)

    assert dispatcher.handle_line("/status") is True
    assert ui.messages[-1] == format_error("Usage: /status <online|away>")
    assert dispatcher.handle_line("/status busy") is True
    assert ui.messages[-1] == format_error("Usage: /status <online|away>")
    assert hello_calls == 0

    # Same status does not rebroadcast.
    assert dispatcher.handle_line("/status online") is True
    assert ui.messages[-1] == "[*] Status is already online."
    assert hello_calls == 0

    # Changing status updates state and broadcasts immediately.
    assert dispatcher.handle_line("/STATUS AWAY") is True
    assert state.get_status() == "away"
    assert ui.messages[-1] == "[*] Status changed to away."
    assert hello_calls == 1

    assert dispatcher.handle_line("/status Online") is True
    assert state.get_status() == "online"
    assert ui.messages[-1] == "[*] Status changed to online."
    assert hello_calls == 2


# --- main() lifecycle & error handling ---


def test_main_invalid_cli_args_prints_error_and_returns_1(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orig_port = discovery.PORT
    orig_bcast = discovery.BROADCAST_ADDR
    called = False

    def _unexpected_chat(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("ChatManager should not be constructed on invalid CLI args")

    monkeypatch.setattr(main_module, "ChatManager", _unexpected_chat)

    code_bad_name = main_module.main(["--name", "Bad|Name", "--udp-port", "12345"])
    assert code_bad_name == 1
    assert "[!]" in capsys.readouterr().out

    code_bad_port = main_module.main(["--name", "Duy", "--port", "70000", "--bcast", "10.0.0.255"])
    assert code_bad_port == 1
    assert "[!] TCP port must be in [0, 65535]" in capsys.readouterr().out

    assert called is False
    assert discovery.PORT == orig_port
    assert discovery.BROADCAST_ADDR == orig_bcast


def test_main_chat_start_oserror_returns_1(monkeypatch: pytest.MonkeyPatch) -> None:
    created_uis: list[_FakeUI] = []
    discovery_called = False

    def _make_ui(state: AppState) -> _FakeUI:
        ui = _FakeUI(state)
        created_uis.append(ui)
        return ui

    def _make_chat(
        state: AppState,
        *,
        print_safe: object = None,
        download_dir: str | Path = "downloads",
    ) -> _FakeChat:
        return _FakeChat(
            state,
            print_safe=print_safe,
            download_dir=download_dir,
            start_error=OSError("address already in use"),
        )

    def _unexpected_discovery(*_args: object, **_kwargs: object) -> bool:
        nonlocal discovery_called
        discovery_called = True
        return True

    monkeypatch.setattr(main_module, "ConsoleUI", _make_ui)
    monkeypatch.setattr(main_module, "ChatManager", _make_chat)
    monkeypatch.setattr(discovery, "start", _unexpected_discovery)

    code = main_module.main(["--name", "Duy", "--port", "5001"])
    assert code == 1
    assert discovery_called is False
    assert created_uis[0].messages == [
        format_error("Could not listen on TCP port 5001: address already in use")
    ]


@pytest.mark.parametrize(
    ("disc_behavior", "expected_code", "expected_fragment"),
    [
        (OSError("udp bind failed"), 1, "[!] Could not start UDP discovery on port 19999:"),
        (KeyboardInterrupt(), 0, None),
        (False, 1, "[!] Name 'Duy' is already in use on the LAN."),
    ],
)
def test_main_discovery_failures_always_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    disc_behavior: object,
    expected_code: int,
    expected_fragment: str | None,
) -> None:
    events: list[str] = []
    created_uis: list[_FakeUI] = []
    created_chats: list[_FakeChat] = []

    def _make_ui(state: AppState) -> _FakeUI:
        ui = _FakeUI(state)
        created_uis.append(ui)
        return ui

    def _make_chat(
        state: AppState,
        *,
        print_safe: object = None,
        download_dir: str | Path = "downloads",
    ) -> _FakeChat:
        chat = _FakeChat(
            state,
            print_safe=print_safe,
            download_dir=download_dir,
            bound_port=5001,
            events=events,
        )
        created_chats.append(chat)
        return chat

    def _disc_start(_state: AppState, _ui: _FakeUI) -> bool:
        events.append("discovery.start")
        if isinstance(disc_behavior, BaseException):
            raise disc_behavior
        return bool(disc_behavior)

    def _disc_stop() -> None:
        events.append("discovery.stop")

    monkeypatch.setattr(main_module, "ConsoleUI", _make_ui)
    monkeypatch.setattr(main_module, "ChatManager", _make_chat)
    monkeypatch.setattr(discovery, "start", _disc_start)
    monkeypatch.setattr(discovery, "stop", _disc_stop)

    code = main_module.main(["--name", "Duy", "--port", "5001", "--udp-port", "19999"])
    assert code == expected_code
    assert events == ["chat.start", "discovery.start", "discovery.stop", "chat.stop"]
    assert created_chats[0].stopped is True
    assert created_uis[0].state.is_stopped() is True
    assert created_uis[0].messages[-1] == "[*] Bye!"
    if expected_fragment is not None:
        assert any(expected_fragment in msg for msg in created_uis[0].messages)


def test_main_normal_session_quit_eof_stop_flag_and_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def _run_session(inputs: list[str | None]) -> tuple[int, _FakeUI, _FakeChat, list[str]]:
        events: list[str] = []
        holder_ui: list[_FakeUI] = []
        holder_chat: list[_FakeChat] = []

        def _make_ui(state: AppState) -> _FakeUI:
            ui = _FakeUI(state, inputs)
            holder_ui.append(ui)
            return ui

        def _make_chat(
            state: AppState,
            *,
            print_safe: object = None,
            download_dir: str | Path = "downloads",
        ) -> _FakeChat:
            chat = _FakeChat(
                state,
                print_safe=print_safe,
                download_dir=download_dir,
                bound_port=5050,
                events=events,
            )
            holder_chat.append(chat)
            return chat

        monkeypatch.setattr(main_module, "ConsoleUI", _make_ui)
        monkeypatch.setattr(main_module, "ChatManager", _make_chat)
        monkeypatch.setattr(discovery, "start", lambda _s, _u: True)
        monkeypatch.setattr(discovery, "stop", lambda: events.append("discovery.stop"))

        code = main_module.main(
            [
                "--name",
                "  Duy  ",
                "--port",
                "0",
                "--udp-port",
                "19998",
                "--bcast",
                "127.255.255.255",
                "--downloads",
                str(tmp_path),
            ]
        )
        return code, holder_ui[0], holder_chat[0], events

    # 1. Normal /quit flow.
    code, ui, chat, events = _run_session(["/list", "/help", "/quit"])
    assert code == 0
    assert discovery.PORT == 19998
    assert discovery.BROADCAST_ADDR == "127.255.255.255"
    assert chat.download_dir == tmp_path
    assert "[*] Listening on TCP port 5050" in ui.messages
    assert "[*] Broadcasting presence on UDP port 19998 (127.255.255.255)" in ui.messages
    assert "[*] Type /help for available commands." in ui.messages
    assert HELP_TEXT in ui.messages
    assert ui.messages[-1] == "[*] Bye!"
    assert events == ["chat.start", "discovery.stop", "chat.stop"]

    # 2. EOF / Ctrl+C (read_line returns None).
    code_eof, ui_eof, chat_eof, events_eof = _run_session([None])
    assert code_eof == 0
    assert chat_eof.stopped is True
    assert ui_eof.messages[-1] == "[*] Bye!"
    assert events_eof == ["chat.start", "discovery.stop", "chat.stop"]

    # 3. External stop_event set before read_line -> loop exits without reading.
    events_stop: list[str] = []
    holder_ui: list[_FakeUI] = []

    def _make_ui_stop(state: AppState) -> _FakeUI:
        ui_inst = _FakeUI(state, ["/help"])
        holder_ui.append(ui_inst)
        return ui_inst

    def _disc_start_and_stop(state: AppState, _ui: _FakeUI) -> bool:
        state.request_stop()
        return True

    monkeypatch.setattr(main_module, "ConsoleUI", _make_ui_stop)
    monkeypatch.setattr(
        main_module,
        "ChatManager",
        lambda state, **kw: _FakeChat(state, events=events_stop, **kw),
    )
    monkeypatch.setattr(discovery, "start", _disc_start_and_stop)
    monkeypatch.setattr(discovery, "stop", lambda: events_stop.append("discovery.stop"))

    assert main_module.main(["--name", "Duy"]) == 0
    assert holder_ui[0].read_calls == 0
    assert events_stop == ["chat.start", "discovery.stop", "chat.stop"]

    # 4. Unexpected runtime error inside loop still runs finally cleanup.
    events_err: list[str] = []

    class _ExplodingUI(_FakeUI):
        def read_line(self) -> str | None:
            raise RuntimeError("boom")

    monkeypatch.setattr(main_module, "ConsoleUI", lambda state: _ExplodingUI(state))
    monkeypatch.setattr(
        main_module,
        "ChatManager",
        lambda state, **kw: _FakeChat(state, events=events_err, **kw),
    )
    monkeypatch.setattr(discovery, "start", lambda _s, _u: True)
    monkeypatch.setattr(discovery, "stop", lambda: events_err.append("discovery.stop"))

    with pytest.raises(RuntimeError, match="boom"):
        main_module.main(["--name", "Duy"])
    assert events_err == ["chat.start", "discovery.stop", "chat.stop"]


def test_main_integration_with_real_chat_manager(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    holder_ui: list[_FakeUI] = []
    disc_stopped = False

    def _make_ui(state: AppState) -> _FakeUI:
        ui = _FakeUI(state, ["/list", "/chat Khang", "/leave", "/quit"])
        holder_ui.append(ui)
        return ui

    def _disc_stop() -> None:
        nonlocal disc_stopped
        disc_stopped = True

    monkeypatch.setattr(main_module, "ConsoleUI", _make_ui)
    monkeypatch.setattr(discovery, "start", lambda _s, _u: True)
    monkeypatch.setattr(discovery, "stop", _disc_stop)

    code = main_module.main(
        [
            "--name",
            "Duy",
            "--port",
            "0",
            "--udp-port",
            "19997",
            "--bcast",
            "127.255.255.255",
            "--downloads",
            str(tmp_path),
        ]
    )

    assert code == 0
    assert disc_stopped is True
    ui = holder_ui[0]
    assert ui.state.is_stopped() is True
    assert ui.state.self_tcp_port > 0
    assert f"[*] Listening on TCP port {ui.state.self_tcp_port}" in ui.messages
    assert "[*] No other users online." in ui.messages
    assert format_error("Khang is not online") in ui.messages
    assert format_error("Not currently in a chat.") in ui.messages
    assert ui.messages[-1] == "[*] Bye!"
