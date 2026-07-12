import ctypes

import pytest

from auto_fishing.model import Direction, Rect
from auto_fishing.platform.input import (
    INPUT,
    KEYBDINPUT,
    MOUSEINPUT,
    InputFailure,
    SafeInput,
    Win32InputBackend,
    Win32MouseDriver,
)


class FakeBackend:
    def __init__(self, *, fail_up: dict[str, int] | None = None) -> None:
        self.events: list[tuple[object, ...]] = []
        self.fail_up = dict(fail_up or {})

    def key_down(self, key: str) -> None:
        self.events.append(("down", key))

    def key_up(self, key: str) -> None:
        self.events.append(("up", key))
        if self.fail_up.get(key, 0) > 0:
            self.fail_up[key] -= 1
            raise InputFailure(f"failed to release {key}")

    def click(self, x: int, y: int) -> None:
        self.events.append(("click", x, y))

    def mouse_up(self) -> None:
        self.events.append(("mouse_up",))


class RecordingLog:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def event(self, name: str, **fields: object) -> None:
        self.events.append({"event": name, **fields})


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


def test_release_all_retries_failed_key_after_releasing_others() -> None:
    backend = FakeBackend(fail_up={"F": 2})
    safe = SafeInput(backend, sleep=lambda _: None)

    with pytest.raises(InputFailure, match="failed to release F"):
        safe.tap_f()
    safe.set_direction(Direction.RIGHT)

    with pytest.raises(InputFailure, match="failed to release F"):
        safe.release_all()

    assert backend.events.count(("up", "D")) == 1
    assert backend.events.count(("up", "F")) == 2

    safe.release_all()
    events_after_retry = list(backend.events)
    safe.release_all()

    assert backend.events.count(("up", "D")) == 1
    assert backend.events.count(("up", "F")) == 3
    assert backend.events == events_after_retry


def test_tap_f_and_click_are_balanced() -> None:
    backend = FakeBackend()
    safe = SafeInput(backend, sleep=lambda _: None)

    safe.tap_f()
    safe.click(200, 300)

    assert backend.events == [("down", "F"), ("up", "F"), ("click", 200, 300)]


def test_tap_f_waits_for_bounded_pre_press_delay() -> None:
    backend = FakeBackend()
    waits: list[float] = []
    safe = SafeInput(
        backend,
        sleep=waits.append,
        random_uniform=lambda _lower, upper: upper,
    )

    safe.tap_f()

    assert waits == [0.18, 0.05]
    assert backend.events == [("down", "F"), ("up", "F")]


def test_direction_changes_do_not_wait_for_f_jitter() -> None:
    backend = FakeBackend()
    waits: list[float] = []
    safe = SafeInput(backend, sleep=waits.append)

    safe.set_direction(Direction.LEFT)
    safe.set_direction(Direction.RIGHT)

    assert waits == []
    assert backend.events == [("down", "A"), ("up", "A"), ("down", "D")]


def test_release_all_cancels_f_before_its_delayed_press() -> None:
    backend = FakeBackend()
    waits: list[float] = []
    safe: SafeInput

    def interrupting_sleep(seconds: float) -> None:
        waits.append(seconds)
        safe.release_all()

    safe = SafeInput(
        backend,
        sleep=interrupting_sleep,
        random_uniform=lambda _lower, _upper: 0.10,
    )
    safe.tap_f()

    assert waits == [0.10]
    assert backend.events == []


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
        self.send_results: list[int] = []
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
        if self.send_results:
            return self.send_results.pop(0)
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


def test_win32_records_sendinput_success_without_stale_error_code() -> None:
    recorder = RecordingLog()
    backend = Win32InputBackend(user32=FakeUser32(), recorder=recorder)

    backend.key_down("F")

    assert recorder.events == [
        {"event": "sendinput.result", "requested": 1, "sent": 1}
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


def test_safe_click_releases_mouse_after_partial_win32_send() -> None:
    user32 = FakeUser32()
    user32.send_results = [1, 1]
    safe = SafeInput(Win32InputBackend(user32=user32), sleep=lambda _: None)

    with pytest.raises(InputFailure, match="SendInput sent 1 of 2"):
        safe.click(200, 300)

    assert user32.events[-1] == (
        "send",
        [("mouse", 0x0004)],
        ctypes.sizeof(INPUT),
    )
    events_after_cleanup = list(user32.events)
    safe.release_all()
    assert user32.events == events_after_cleanup


def test_safe_click_retries_mouse_release_after_cleanup_fails() -> None:
    user32 = FakeUser32()
    user32.send_results = [1, 0, 1]
    safe = SafeInput(Win32InputBackend(user32=user32), sleep=lambda _: None)

    with pytest.raises(InputFailure, match="mouse release failed"):
        safe.click(200, 300)

    safe.release_all()
    events_after_retry = list(user32.events)
    safe.release_all()

    mouse_sends = [event for event in user32.events if event[0] == "send"]
    assert [event[1] for event in mouse_sends] == [
        [("mouse", 0x0002), ("mouse", 0x0004)],
        [("mouse", 0x0004)],
        [("mouse", 0x0004)],
    ]
    assert user32.events == events_after_retry


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


def test_win32_records_partial_send_with_windows_error(monkeypatch) -> None:
    recorder = RecordingLog()
    user32 = FakeUser32()
    user32.send_result = 0
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)
    backend = Win32InputBackend(user32=user32, recorder=recorder)

    with pytest.raises(InputFailure, match="SendInput sent 0 of 1"):
        backend.key_down("F")

    assert recorder.events == [
        {
            "event": "sendinput.result",
            "requested": 1,
            "sent": 0,
            "windows_error": 5,
        }
    ]


def test_win32_mouse_driver_moves_and_holds_then_releases_left_button() -> None:
    user32 = FakeUser32()
    mouse = Win32MouseDriver(user32=user32)

    mouse.move(200, 300)
    mouse.down()
    mouse.up()

    assert user32.events == [
        ("cursor", 200, 300),
        ("send", [("mouse", 0x0002)], ctypes.sizeof(INPUT)),
        ("send", [("mouse", 0x0004)], ctypes.sizeof(INPUT)),
    ]


def test_win32_mouse_driver_reports_cursor_failure_before_mouse_down() -> None:
    user32 = FakeUser32()
    user32.cursor_result = 0
    mouse = Win32MouseDriver(user32=user32)

    with pytest.raises(InputFailure, match="SetCursorPos failed"):
        mouse.move(200, 300)

    assert user32.events == [("cursor", 200, 300)]


def test_win32_records_cursor_result_for_click() -> None:
    recorder = RecordingLog()
    backend = Win32InputBackend(user32=FakeUser32(), recorder=recorder)

    backend.click(200, 300)

    assert recorder.events[0] == {
        "event": "cursor.result",
        "x": 200,
        "y": 300,
        "success": True,
    }


def test_safe_input_records_tap_and_direction_requests() -> None:
    recorder = RecordingLog()
    safe = SafeInput(FakeBackend(), sleep=lambda _: None, recorder=recorder)

    safe.tap_f()
    safe.set_direction(Direction.RIGHT)
    safe.release_all()

    assert recorder.events == [
        {"event": "input.request", "action": "tap", "key": "F"},
        {"event": "input.request", "action": "key_down", "key": "F"},
        {"event": "input.request", "action": "key_up", "key": "F"},
        {"event": "input.request", "action": "key_down", "key": "D"},
        {"event": "input.request", "action": "key_up", "key": "D"},
    ]


def test_safe_input_records_mouse_click_and_cleanup_request() -> None:
    class FailingClickBackend(FakeBackend):
        def click(self, x: int, y: int) -> None:
            super().click(x, y)
            raise InputFailure("click failed")

    recorder = RecordingLog()
    safe = SafeInput(FailingClickBackend(), sleep=lambda _: None, recorder=recorder)

    with pytest.raises(InputFailure, match="click failed"):
        safe.click(200, 300)

    assert recorder.events == [
        {"event": "input.request", "action": "click", "x": 200, "y": 300},
        {"event": "input.request", "action": "mouse_up"},
    ]


def test_safe_input_delegates_keyboard_lifecycle_and_occlusion() -> None:
    class LifecycleBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.prepared: list[tuple[Rect, Rect]] = []
            self.closed = 0

        def prepare(self, monitor_rect: Rect, client_rect: Rect) -> None:
            self.prepared.append((monitor_rect, client_rect))

        def occlusion_rect(self) -> Rect:
            return Rect(0, 600, 900, 1080)

        def close(self) -> None:
            self.closed += 1

    backend = LifecycleBackend()
    safe = SafeInput(backend)
    monitor = Rect(0, 0, 1920, 1080)
    client = Rect(0, 0, 1920, 1080)

    safe.prepare(monitor, client)
    occlusion = safe.occlusion_rect()
    safe.close()

    assert backend.prepared == [(monitor, client)]
    assert occlusion == Rect(0, 600, 900, 1080)
    assert backend.closed == 1


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
