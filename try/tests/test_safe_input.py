import ctypes

import pytest

from auto_fishing.model import Direction
from auto_fishing.platform.input import (
    INPUT,
    KEYBDINPUT,
    MOUSEINPUT,
    InputFailure,
    SafeInput,
    Win32InputBackend,
)


class FakeBackend:
    def __init__(self, *, fail_up: set[str] | None = None) -> None:
        self.events: list[tuple[object, ...]] = []
        self.fail_up = fail_up or set()

    def key_down(self, key: str) -> None:
        self.events.append(("down", key))

    def key_up(self, key: str) -> None:
        self.events.append(("up", key))
        if key in self.fail_up:
            raise RuntimeError(f"failed to release {key}")

    def click(self, x: int, y: int) -> None:
        self.events.append(("click", x, y))


def test_direction_switch_releases_old_key_first() -> None:
    backend = FakeBackend()
    safe = SafeInput(backend, sleep=lambda _: None)

    safe.set_direction(Direction.LEFT)
    safe.set_direction(Direction.RIGHT)

    assert backend.events == [("down", "A"), ("up", "A"), ("down", "D")]


def test_release_all_is_idempotent() -> None:
    backend = FakeBackend()
    safe = SafeInput(backend, sleep=lambda _: None)

    safe.set_direction(Direction.RIGHT)
    safe.release_all()
    safe.release_all()

    assert backend.events == [("down", "D"), ("up", "D")]


def test_release_all_forgets_key_even_when_release_fails() -> None:
    backend = FakeBackend(fail_up={"D"})
    safe = SafeInput(backend, sleep=lambda _: None)
    safe.set_direction(Direction.RIGHT)

    with pytest.raises(RuntimeError, match="failed to release D"):
        safe.release_all()
    safe.release_all()

    assert backend.events == [("down", "D"), ("up", "D")]


def test_tap_f_and_click_are_balanced() -> None:
    backend = FakeBackend()
    safe = SafeInput(backend, sleep=lambda _: None)

    safe.tap_f()
    safe.click(200, 300)

    assert backend.events == [("down", "F"), ("up", "F"), ("click", 200, 300)]


def test_tap_f_releases_key_when_sleep_fails() -> None:
    backend = FakeBackend()

    def fail_sleep(_: float) -> None:
        raise RuntimeError("sleep interrupted")

    safe = SafeInput(backend, sleep=fail_sleep)

    with pytest.raises(RuntimeError, match="sleep interrupted"):
        safe.tap_f()

    assert backend.events == [("down", "F"), ("up", "F")]


class FakeUser32:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []
        self.send_result: int | None = None
        self.cursor_result = 1

    def SendInput(self, count: int, inputs: object, size: int) -> int:
        captured = []
        for item in inputs:
            if item.type == 1:
                captured.append(
                    ("keyboard", item.union.ki.wScan, item.union.ki.dwFlags)
                )
            else:
                captured.append(("mouse", item.union.mi.dwFlags))
        self.events.append(("send", captured, size))
        return count if self.send_result is None else self.send_result

    def SetCursorPos(self, x: int, y: int) -> int:
        self.events.append(("cursor", x, y))
        return self.cursor_result


def test_win32_input_structures_match_native_pointer_width() -> None:
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(KEYBDINPUT) == 24
        assert ctypes.sizeof(MOUSEINPUT) == 32
        assert ctypes.sizeof(INPUT) == 40
    else:
        assert ctypes.sizeof(KEYBDINPUT) == 16
        assert ctypes.sizeof(MOUSEINPUT) == 24
        assert ctypes.sizeof(INPUT) == 28


def test_win32_keyboard_uses_scan_codes_and_balanced_flags() -> None:
    user32 = FakeUser32()
    backend = Win32InputBackend(user32=user32)

    backend.key_down("A")
    backend.key_up("A")

    assert user32.events == [
        ("send", [("keyboard", 0x1E, 0x0008)], ctypes.sizeof(INPUT)),
        ("send", [("keyboard", 0x1E, 0x000A)], ctypes.sizeof(INPUT)),
    ]


def test_win32_click_moves_cursor_then_sends_left_down_and_up() -> None:
    user32 = FakeUser32()
    backend = Win32InputBackend(user32=user32)

    backend.click(200, 300)

    assert user32.events == [
        ("cursor", 200, 300),
        (
            "send",
            [("mouse", 0x0002), ("mouse", 0x0004)],
            ctypes.sizeof(INPUT),
        ),
    ]


def test_win32_rejects_unknown_key_without_sending_input() -> None:
    user32 = FakeUser32()
    backend = Win32InputBackend(user32=user32)

    with pytest.raises(ValueError, match="unsupported key"):
        backend.key_down("X")

    assert user32.events == []


def test_win32_raises_when_set_cursor_pos_fails() -> None:
    user32 = FakeUser32()
    user32.cursor_result = 0
    backend = Win32InputBackend(user32=user32)

    with pytest.raises(InputFailure, match="SetCursorPos failed"):
        backend.click(200, 300)

    assert user32.events == [("cursor", 200, 300)]


@pytest.mark.parametrize(
    ("action", "requested"),
    [
        (lambda backend: backend.key_down("F"), 1),
        (lambda backend: backend.click(200, 300), 2),
    ],
)
def test_win32_checks_send_input_return_count(action, requested: int) -> None:
    user32 = FakeUser32()
    user32.send_result = requested - 1
    backend = Win32InputBackend(user32=user32)

    with pytest.raises(
        InputFailure,
        match=rf"SendInput sent {requested - 1} of {requested}",
    ):
        action(backend)
