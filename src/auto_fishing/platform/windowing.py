from __future__ import annotations

import ctypes
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import dxcam
from ctypes import wintypes

from auto_fishing.model import Rect


MONITOR_DEFAULTTONEAREST = 2
PROCESS_PER_MONITOR_DPI_AWARE = 2
E_ACCESSDENIED = 0x80070005
GA_ROOT = 2
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
    device_index: int
    output_index: int


@dataclass(frozen=True)
class DxcamOutput:
    device_index: int
    output_index: int
    devicename: str
    resolution: tuple[int, int]


class DxcamOutputCatalog:
    def __init__(self, factory: Callable[[], Any] | None = None) -> None:
        self._factory = (factory or dxcam.DXFactory)()

    def list_outputs(self) -> list[DxcamOutput]:
        return [
            DxcamOutput(
                device_index=device_index,
                output_index=output_index,
                devicename=str(output.devicename),
                resolution=tuple(output.resolution),
            )
            for device_index, device_outputs in enumerate(self._factory.outputs)
            for output_index, output in enumerate(device_outputs)
        ]


class _WinRect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _MonitorInfoEx(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _WinRect),
        ("rcWork", _WinRect),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


def _handle_value(handle: Any) -> int:
    if isinstance(handle, int):
        return handle
    return int(ctypes.cast(handle, ctypes.c_void_p).value or 0)


class WindowService:
    def __init__(
        self,
        user32: Any | None = None,
        shcore: Any | None = None,
        own_hwnd: int | None = None,
        output_catalog: DxcamOutputCatalog | None = None,
    ) -> None:
        native_user32 = user32 is None
        if user32 is None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetForegroundWindow.argtypes = ()
            user32.GetForegroundWindow.restype = wintypes.HWND
            user32.SetProcessDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
            user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
            user32.SetProcessDPIAware.argtypes = ()
            user32.SetProcessDPIAware.restype = wintypes.BOOL
            user32.MonitorFromRect.argtypes = (
                ctypes.POINTER(_WinRect),
                wintypes.DWORD,
            )
            user32.MonitorFromRect.restype = wintypes.HMONITOR
        self.user32 = user32
        if shcore is None and native_user32:
            try:
                shcore = ctypes.WinDLL("shcore", use_last_error=True)
                shcore.SetProcessDpiAwareness.argtypes = (ctypes.c_int,)
                shcore.SetProcessDpiAwareness.restype = ctypes.c_long
            except (AttributeError, OSError):
                shcore = None
        self.shcore = shcore
        self._native_user32 = native_user32
        self.dpi_awareness = "unknown"
        self.own_hwnd = own_hwnd
        self.output_catalog = output_catalog or DxcamOutputCatalog()

    def enable_dpi_awareness(self) -> str:
        try:
            enabled = bool(
                self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            )
        except (AttributeError, OSError):
            enabled = False
        if enabled:
            self.dpi_awareness = "per_monitor_v2"
            return self.dpi_awareness
        if self._native_user32 and ctypes.get_last_error() == 5:
            self.dpi_awareness = "manifest_or_existing"
            return self.dpi_awareness

        if self.shcore is not None:
            try:
                result = int(
                    self.shcore.SetProcessDpiAwareness(
                        PROCESS_PER_MONITOR_DPI_AWARE
                    )
                )
            except (AttributeError, OSError):
                result = 1
            if result == 0:
                self.dpi_awareness = "per_monitor"
                return self.dpi_awareness
            if result & 0xFFFFFFFF == E_ACCESSDENIED:
                self.dpi_awareness = "manifest_or_existing"
                return self.dpi_awareness

        try:
            legacy_enabled = bool(self.user32.SetProcessDPIAware())
        except (AttributeError, OSError):
            legacy_enabled = False
        self.dpi_awareness = (
            "system" if legacy_enabled else "manifest_or_unknown"
        )
        return self.dpi_awareness

    def clamp_window_position(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> tuple[int, int]:
        native = _WinRect(x, y, x + width, y + height)
        monitor_handle = _handle_value(
            self.user32.MonitorFromRect(
                ctypes.byref(native),
                MONITOR_DEFAULTTONEAREST,
            )
        )
        if not monitor_handle:
            return x, y
        info = _MonitorInfoEx()
        info.cbSize = ctypes.sizeof(info)
        if not self.user32.GetMonitorInfoW(monitor_handle, ctypes.byref(info)):
            return x, y
        work = Rect(
            int(info.rcWork.left),
            int(info.rcWork.top),
            int(info.rcWork.right),
            int(info.rcWork.bottom),
        )
        return (
            min(max(x, work.left), max(work.left, work.right - width)),
            min(max(y, work.top), max(work.top, work.bottom - height)),
        )

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
        client_rect, monitor_rect, device_index, output_index = (
            self._window_geometry(hwnd)
        )
        return BoundWindow(
            hwnd,
            title,
            client_rect,
            monitor_rect,
            device_index,
            output_index,
        )

    def resolve_top_level(self, hwnd: int) -> int:
        """Return the root top-level HWND that owns a Tk or child HWND."""
        root_hwnd = int(self.user32.GetAncestor(hwnd, GA_ROOT) or 0)
        if not root_hwnd:
            raise WindowBindingError("无法解析控制窗口顶层句柄")
        return root_hwnd

    def refresh(self, bound: BoundWindow) -> BoundWindow:
        if not self.user32.IsWindow(bound.hwnd):
            raise WindowBindingError("窗口已失效")
        if self.user32.IsIconic(bound.hwnd):
            raise WindowBindingError("窗口已最小化")
        client_rect, monitor_rect, device_index, output_index = (
            self._window_geometry(bound.hwnd)
        )
        return BoundWindow(
            bound.hwnd,
            bound.title,
            client_rect,
            monitor_rect,
            device_index,
            output_index,
        )

    def activate(self, bound: BoundWindow) -> bool:
        self.user32.ShowWindow(bound.hwnd, SW_RESTORE)
        self.user32.SetForegroundWindow(bound.hwnd)
        return self.is_foreground(bound)

    def is_foreground(self, bound: BoundWindow) -> bool:
        return int(self.user32.GetForegroundWindow() or 0) == bound.hwnd

    def is_control_foreground(self) -> bool:
        foreground = int(self.user32.GetForegroundWindow() or 0)
        return self.own_hwnd is not None and foreground == self.own_hwnd

    def exclude_from_capture(self, hwnd: int) -> bool:
        return bool(
            self.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        )

    def _window_title(self, hwnd: int) -> str:
        length = int(self.user32.GetWindowTextLengthW(hwnd))
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        self.user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def _window_geometry(self, hwnd: int) -> tuple[Rect, Rect, int, int]:
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
        monitor_rect, device_name = self._monitor_info(monitor_handle)
        if not self._contains(monitor_rect, client_rect):
            raise WindowBindingError("客户区跨越显示器边界")

        outputs = [
            output
            for output in self.output_catalog.list_outputs()
            if output.devicename == device_name
        ]
        if len(outputs) != 1:
            raise WindowBindingError("无法映射游戏所在显示器")
        output = outputs[0]
        if output.resolution != (monitor_rect.width, monitor_rect.height):
            raise WindowBindingError("无法映射游戏所在显示器")
        return client_rect, monitor_rect, output.device_index, output.output_index

    def _monitor_info(self, monitor_handle: int) -> tuple[Rect, str]:
        info = _MonitorInfoEx()
        info.cbSize = ctypes.sizeof(info)
        if not self.user32.GetMonitorInfoW(monitor_handle, ctypes.byref(info)):
            raise WindowBindingError("无法读取显示器信息")
        monitor_rect = Rect(
            int(info.rcMonitor.left),
            int(info.rcMonitor.top),
            int(info.rcMonitor.right),
            int(info.rcMonitor.bottom),
        )
        return monitor_rect, str(info.szDevice)

    @staticmethod
    def _contains(outer: Rect, inner: Rect) -> bool:
        return (
            outer.left <= inner.left
            and outer.top <= inner.top
            and inner.right <= outer.right
            and inner.bottom <= outer.bottom
        )
