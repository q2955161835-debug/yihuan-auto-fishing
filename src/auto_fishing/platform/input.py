from __future__ import annotations

import ctypes
from ctypes import wintypes
from time import sleep as real_sleep
from typing import Callable, Protocol

from auto_fishing.model import Direction


class InputFailure(RuntimeError):
    """Raised when Windows does not accept a requested input action."""


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


class SafeInput:
    def __init__(
        self,
        backend: InputBackend,
        sleep: Callable[[float], None] = real_sleep,
    ) -> None:
        self.backend = backend
        self.sleep = sleep
        self.held: set[str] = set()

    def _down(self, key: str) -> None:
        if key not in self.held:
            self.backend.key_down(key)
            self.held.add(key)

    def _up(self, key: str) -> None:
        if key in self.held:
            try:
                self.backend.key_up(key)
            finally:
                self.held.discard(key)

    def tap_f(self) -> None:
        self._down("F")
        try:
            self.sleep(0.05)
        finally:
            self._up("F")

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
        self.backend.click(x, y)

    def release_all(self) -> None:
        for key in tuple(self.held):
            try:
                self.backend.key_up(key)
            finally:
                self.held.discard(key)


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

    def __init__(self, user32: object | None = None) -> None:
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

    def key_down(self, key: str) -> None:
        self._send_key(key, key_up=False)

    def key_up(self, key: str) -> None:
        self._send_key(key, key_up=True)

    def click(self, x: int, y: int) -> None:
        if not self._user32.SetCursorPos(x, y):
            raise InputFailure(
                f"SetCursorPos failed for ({x}, {y}); "
                f"Windows error {ctypes.get_last_error()}"
            )
        self._send_inputs(
            self._mouse_input(self._MOUSEEVENTF_LEFTDOWN),
            self._mouse_input(self._MOUSEEVENTF_LEFTUP),
        )

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

    def _mouse_input(self, flags: int) -> INPUT:
        mouse = MOUSEINPUT(
            dx=0,
            dy=0,
            mouseData=0,
            dwFlags=flags,
            time=0,
            dwExtraInfo=0,
        )
        return INPUT(
            type=self._INPUT_MOUSE,
            union=INPUT_UNION(mi=mouse),
        )

    def _send_inputs(self, *inputs: INPUT) -> None:
        input_array = (INPUT * len(inputs))(*inputs)
        requested = len(input_array)
        sent = self._user32.SendInput(
            requested,
            input_array,
            ctypes.sizeof(INPUT),
        )
        if sent != requested:
            raise InputFailure(
                f"SendInput sent {sent} of {requested}; "
                f"Windows error {ctypes.get_last_error()}"
            )
