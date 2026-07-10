from __future__ import annotations

import ctypes
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import dxcam
from ctypes import wintypes

from auto_fishing.model import Rect


MONITOR_DEFAULTTONEAREST = 2
SW_RESTORE = 9
WDA_EXCLUDEFROMCAPTURE = 0x11
MIN_CLIENT_WIDTH = 960
MIN_CLIENT_HEIGHT = 540


class WindowBindingError(RuntimeError):
    """Raised when a safe, capturable game window cannot be identified."""


@dataclass(frozen=True)
class BoundWindow:
    hwnd: int
    title: str
    client_rect: Rect
    monitor_rect: Rect
    output_index: int


class _WinRect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def _default_output_resolutions() -> list[tuple[int, int]]:
    resolutions: list[tuple[int, int]] = []
    for width, height in re.findall(r"Res:\((\d+),\s*(\d+)\)", dxcam.output_info()):
        resolutions.append((int(width), int(height)))
    return resolutions


def _handle_value(handle: Any) -> int:
    if isinstance(handle, int):
        return handle
    return int(ctypes.cast(handle, ctypes.c_void_p).value or 0)


class WindowService:
    def __init__(
        self,
        user32: Any | None = None,
        own_hwnd: int | None = None,
        output_resolutions: Callable[[], list[tuple[int, int]]] | None = None,
    ) -> None:
        self.user32 = user32 or ctypes.windll.user32
        self.own_hwnd = own_hwnd
        self.output_resolutions = output_resolutions or _default_output_resolutions

    def enable_dpi_awareness(self) -> None:
        try:
            enabled = bool(
                self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            )
        except (AttributeError, OSError):
            enabled = False
        if not enabled:
            self.user32.SetProcessDPIAware()

    def bind_foreground(self) -> BoundWindow:
        hwnd = int(self.user32.GetForegroundWindow() or 0)
        if not hwnd:
            raise WindowBindingError("没有可绑定的前台窗口")
        if self.own_hwnd is not None and hwnd == self.own_hwnd:
            raise WindowBindingError("不能绑定控制窗口")
        if self.user32.IsIconic(hwnd):
            raise WindowBindingError("窗口已最小化")

        title = self._window_title(hwnd)
        if not title.strip():
            raise WindowBindingError("窗口标题为空")
        client_rect, monitor_rect, output_index = self._window_geometry(hwnd)
        return BoundWindow(hwnd, title, client_rect, monitor_rect, output_index)

    def refresh(self, bound: BoundWindow) -> BoundWindow:
        if not self.user32.IsWindow(bound.hwnd):
            raise WindowBindingError("窗口已失效")
        if self.user32.IsIconic(bound.hwnd):
            raise WindowBindingError("窗口已最小化")
        client_rect, monitor_rect, output_index = self._window_geometry(bound.hwnd)
        return BoundWindow(
            bound.hwnd,
            bound.title,
            client_rect,
            monitor_rect,
            output_index,
        )

    def activate(self, bound: BoundWindow) -> bool:
        self.user32.ShowWindow(bound.hwnd, SW_RESTORE)
        self.user32.SetForegroundWindow(bound.hwnd)
        return self.is_foreground(bound)

    def is_foreground(self, bound: BoundWindow) -> bool:
        return int(self.user32.GetForegroundWindow() or 0) == bound.hwnd

    def exclude_from_capture(self, hwnd: int) -> bool:
        return bool(
            self.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        )

    def _window_title(self, hwnd: int) -> str:
        length = int(self.user32.GetWindowTextLengthW(hwnd))
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        self.user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def _window_geometry(self, hwnd: int) -> tuple[Rect, Rect, int]:
        native_rect = _WinRect()
        if not self.user32.GetClientRect(hwnd, ctypes.byref(native_rect)):
            raise WindowBindingError("无法读取窗口客户区")

        top_left = _Point(native_rect.left, native_rect.top)
        bottom_right = _Point(native_rect.right, native_rect.bottom)
        if not self.user32.ClientToScreen(hwnd, ctypes.byref(top_left)):
            raise WindowBindingError("无法换算窗口客户区坐标")
        if not self.user32.ClientToScreen(hwnd, ctypes.byref(bottom_right)):
            raise WindowBindingError("无法换算窗口客户区坐标")
        client_rect = Rect(
            int(top_left.x),
            int(top_left.y),
            int(bottom_right.x),
            int(bottom_right.y),
        )
        if (
            client_rect.width < MIN_CLIENT_WIDTH
            or client_rect.height < MIN_CLIENT_HEIGHT
        ):
            raise WindowBindingError("客户区尺寸过小")

        monitor_handle = _handle_value(
            self.user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        )
        if not monitor_handle:
            raise WindowBindingError("无法映射游戏所在显示器")
        monitors = self._enum_monitors()
        matches = [
            (index, rect)
            for index, (handle, rect) in enumerate(monitors)
            if handle == monitor_handle
        ]
        if len(matches) != 1:
            raise WindowBindingError("无法映射游戏所在显示器")
        output_index, monitor_rect = matches[0]
        self._validate_output(output_index, monitor_rect)
        return client_rect, monitor_rect, output_index

    def _enum_monitors(self) -> list[tuple[int, Rect]]:
        monitors: list[tuple[int, Rect]] = []

        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HMONITOR,
            wintypes.HDC,
            ctypes.POINTER(_WinRect),
            wintypes.LPARAM,
        )

        def collect(handle, _hdc, rect_pointer, _data) -> bool:
            native_rect = rect_pointer.contents
            monitors.append(
                (
                    _handle_value(handle),
                    Rect(
                        int(native_rect.left),
                        int(native_rect.top),
                        int(native_rect.right),
                        int(native_rect.bottom),
                    ),
                )
            )
            return True

        callback = callback_type(collect)
        callback._rect_type = _WinRect
        if not self.user32.EnumDisplayMonitors(None, None, callback, 0):
            raise WindowBindingError("无法枚举显示器")
        return monitors

    def _validate_output(self, output_index: int, monitor_rect: Rect) -> None:
        resolutions = self.output_resolutions()
        if output_index >= len(resolutions):
            raise WindowBindingError("无法映射游戏所在显示器")
        if tuple(resolutions[output_index]) != (
            monitor_rect.width,
            monitor_rect.height,
        ):
            raise WindowBindingError("无法映射游戏所在显示器")
