from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
from random import uniform as real_uniform
from time import sleep as real_sleep
from typing import Any, Callable, Protocol

from auto_fishing.model import Direction, Rect


class InputFailure(RuntimeError):
    """Raised when Windows does not accept a requested input action."""


class InputTargetUnavailable(InputFailure):
    """Raised before new input when the game cannot safely receive it."""


ULONG_PTR = wintypes.WPARAM


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("ki", KEYBDINPUT),
        ("mi", MOUSEINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", INPUT_UNION),
    ]


class InputBackend(Protocol):
    def key_down(self, key: str) -> None: ...

    def key_up(self, key: str) -> None: ...

    def click(self, x: int, y: int) -> None: ...

    def mouse_up(self) -> None: ...


class InputRecorder(Protocol):
    def event(self, name: str, **fields: Any) -> None: ...


def _record_event(
    recorder: InputRecorder | None,
    name: str,
    **fields: Any,
) -> None:
    if recorder is not None:
        recorder.event(name, **fields)


def _mouse_input(flags: int) -> INPUT:
    mouse = MOUSEINPUT(
        dx=0,
        dy=0,
        mouseData=0,
        dwFlags=flags,
        time=0,
        dwExtraInfo=0,
    )
    return INPUT(
        type=0,
        union=INPUT_UNION(mi=mouse),
    )


def _send_inputs(
    user32: object,
    recorder: InputRecorder | None,
    *inputs: INPUT,
) -> None:
    input_array = (INPUT * len(inputs))(*inputs)
    requested = len(input_array)
    sent = user32.SendInput(
        requested,
        input_array,
        ctypes.sizeof(INPUT),
    )
    fields: dict[str, int] = {"requested": requested, "sent": int(sent)}
    if sent != requested:
        fields["windows_error"] = ctypes.get_last_error()
    _record_event(recorder, "sendinput.result", **fields)
    if sent != requested:
        raise InputFailure(
            f"SendInput sent {sent} of {requested}; "
            f"Windows error {fields['windows_error']}"
        )


class Win32MouseDriver:
    _MOUSEEVENTF_LEFTDOWN = 0x0002
    _MOUSEEVENTF_LEFTUP = 0x0004

    def __init__(
        self,
        user32: object | None = None,
        recorder: InputRecorder | None = None,
    ) -> None:
        if user32 is None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.SendInput.argtypes = (
                wintypes.UINT,
                ctypes.POINTER(INPUT),
                ctypes.c_int,
            )
            user32.SendInput.restype = wintypes.UINT
            user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
            user32.SetCursorPos.restype = wintypes.BOOL
        self._user32 = user32
        self.recorder = recorder

    def move(self, x: int, y: int) -> None:
        cursor_set = self._user32.SetCursorPos(x, y)
        fields: dict[str, Any] = {"x": x, "y": y, "success": bool(cursor_set)}
        if not cursor_set:
            fields["windows_error"] = ctypes.get_last_error()
        _record_event(self.recorder, "cursor.result", **fields)
        if not cursor_set:
            raise InputFailure(
                f"SetCursorPos failed for ({x}, {y}); "
                f"Windows error {fields['windows_error']}"
            )

    def down(self) -> None:
        _send_inputs(
            self._user32,
            self.recorder,
            _mouse_input(self._MOUSEEVENTF_LEFTDOWN),
        )

    def up(self) -> None:
        _send_inputs(
            self._user32,
            self.recorder,
            _mouse_input(self._MOUSEEVENTF_LEFTUP),
        )

    def click(self, x: int, y: int) -> None:
        self.move(x, y)
        _send_inputs(
            self._user32,
            self.recorder,
            _mouse_input(self._MOUSEEVENTF_LEFTDOWN),
            _mouse_input(self._MOUSEEVENTF_LEFTUP),
        )


class SafeInput:
    def __init__(
        self,
        backend: InputBackend,
        sleep: Callable[[float], None] = real_sleep,
        recorder: InputRecorder | None = None,
        random_uniform: Callable[[float, float], float] = real_uniform,
    ) -> None:
        self.backend = backend
        self.sleep = sleep
        self.recorder = recorder
        self.random_uniform = random_uniform
        self.held: set[str] = set()
        self.mouse_held = False
        self._cancel_generation = 0
        self._target_guard: Callable[[], bool] | None = None

    def set_target_guard(self, guard: Callable[[], bool] | None) -> None:
        self._target_guard = guard

    def _ensure_target_available(self) -> None:
        guard = self._target_guard
        if guard is None:
            return
        try:
            available = bool(guard())
        except Exception as error:
            raise InputTargetUnavailable(
                f"无法确认游戏窗口前台状态: {error}"
            ) from error
        if not available:
            raise InputTargetUnavailable(
                "游戏窗口已失去前台，可能被 Windows 系统弹窗或其他窗口遮挡"
            )

    def _down(self, key: str) -> None:
        self._ensure_target_available()
        if key not in self.held:
            self._record("input.request", action="key_down", key=key)
            self.backend.key_down(key)
            self.held.add(key)

    def _up(self, key: str) -> None:
        if key in self.held:
            self._record("input.request", action="key_up", key=key)
            self.backend.key_up(key)
            self.held.remove(key)

    def tap_f(self) -> None:
        self._record("input.request", action="tap", key="F")
        generation = self._cancel_generation
        delay = self._f_pre_press_delay()
        self._record("input.delay", key="F", seconds=delay)
        self.sleep(delay)
        if generation != self._cancel_generation:
            self._record("input.cancelled", action="tap", key="F")
            return
        self._down("F")
        try:
            self.sleep(0.05)
        finally:
            self._up("F")

    def prepare(self, monitor_rect: Rect, client_rect: Rect) -> None:
        prepare = getattr(self.backend, "prepare", None)
        if prepare is not None:
            prepare(monitor_rect, client_rect)

    def occlusion_rect(self) -> Rect | None:
        read_occlusion = getattr(self.backend, "occlusion_rect", None)
        if read_occlusion is None:
            return None
        return read_occlusion()

    def close(self) -> None:
        close = getattr(self.backend, "close", None)
        if close is not None:
            close()

    def set_direction(self, direction: Direction) -> None:
        desired = {
            Direction.LEFT: "A",
            Direction.RIGHT: "D",
        }.get(direction)
        for key in ("A", "D"):
            if key != desired:
                self._up(key)
        if desired is not None:
            self._down(desired)

    def click(self, x: int, y: int) -> None:
        self._ensure_target_available()
        self._record("input.request", action="click", x=x, y=y)
        self.mouse_held = True
        try:
            self.backend.click(x, y)
        except Exception as click_error:
            try:
                self._record("input.request", action="mouse_up")
                self.backend.mouse_up()
            except Exception as release_error:
                raise InputFailure(
                    f"click failed: {click_error}; "
                    f"mouse release failed: {release_error}"
                ) from release_error
            else:
                self.mouse_held = False
            raise
        else:
            self.mouse_held = False

    def release_all(self) -> None:
        self._cancel_generation += 1
        failures: list[tuple[str, Exception]] = []
        for key in tuple(self.held):
            try:
                self._record("input.request", action="key_up", key=key)
                self.backend.key_up(key)
            except Exception as error:
                failures.append((key, error))
            else:
                self.held.discard(key)
        if self.mouse_held:
            try:
                self._record("input.request", action="mouse_up")
                self.backend.mouse_up()
            except Exception as error:
                failures.append(("mouse", error))
            else:
                self.mouse_held = False
        if failures:
            details = "; ".join(
                f"{key}: {error}" for key, error in failures
            )
            raise InputFailure(
                f"failed to release inputs: {details}"
            ) from failures[0][1]

    def _record(self, name: str, **fields: Any) -> None:
        if self.recorder is not None:
            self.recorder.event(name, **fields)

    def _f_pre_press_delay(self) -> float:
        delay = float(self.random_uniform(0.08, 0.18))
        if not math.isfinite(delay):
            raise ValueError("F 按键延迟必须为有限数")
        return min(0.18, max(0.08, delay))


class Win32InputBackend:
    _INPUT_MOUSE = 0
    _INPUT_KEYBOARD = 1
    _KEYEVENTF_KEYUP = 0x0002
    _KEYEVENTF_SCANCODE = 0x0008
    _MOUSEEVENTF_LEFTDOWN = 0x0002
    _MOUSEEVENTF_LEFTUP = 0x0004
    _SCAN_CODES = {
        "A": 0x1E,
        "F": 0x21,
        "D": 0x20,
    }

    def __init__(
        self,
        user32: object | None = None,
        recorder: InputRecorder | None = None,
    ) -> None:
        if user32 is None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.SendInput.argtypes = (
                wintypes.UINT,
                ctypes.POINTER(INPUT),
                ctypes.c_int,
            )
            user32.SendInput.restype = wintypes.UINT
            user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
            user32.SetCursorPos.restype = wintypes.BOOL
        self._user32 = user32
        self.recorder = recorder
        self._mouse = Win32MouseDriver(user32=user32, recorder=recorder)

    def key_down(self, key: str) -> None:
        self._send_key(key, key_up=False)

    def key_up(self, key: str) -> None:
        self._send_key(key, key_up=True)

    def click(self, x: int, y: int) -> None:
        self._mouse.click(x, y)

    def mouse_up(self) -> None:
        self._mouse.up()

    def _send_key(self, key: str, *, key_up: bool) -> None:
        normalized = key.upper()
        try:
            scan_code = self._SCAN_CODES[normalized]
        except KeyError as error:
            raise ValueError(f"unsupported key: {key!r}") from error
        flags = self._KEYEVENTF_SCANCODE
        if key_up:
            flags |= self._KEYEVENTF_KEYUP
        keyboard = KEYBDINPUT(
            wVk=0,
            wScan=scan_code,
            dwFlags=flags,
            time=0,
            dwExtraInfo=0,
        )
        self._send_inputs(
            INPUT(
                type=self._INPUT_KEYBOARD,
                union=INPUT_UNION(ki=keyboard),
            )
        )

    def _send_inputs(self, *inputs: INPUT) -> None:
        _send_inputs(self._user32, self.recorder, *inputs)

    def _record(self, name: str, **fields: Any) -> None:
        _record_event(self.recorder, name, **fields)
