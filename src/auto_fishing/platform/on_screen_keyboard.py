from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from time import sleep as real_sleep
from typing import Any, Callable

from auto_fishing.model import Rect
from auto_fishing.vision.regions import READY_ROI, TOP_ROI


class OnScreenKeyboardError(RuntimeError):
    """Raised when the Windows on-screen keyboard cannot be used safely."""


class OnScreenKeyboardPositionDenied(OnScreenKeyboardError):
    """Raised when Windows protects the accessibility window from movement."""


class OnScreenKeyboardCloseDenied(OnScreenKeyboardError):
    """Raised when Windows protects the accessibility window from closing."""


@dataclass(frozen=True)
class KeyboardGeometry:
    hwnd: int
    window_rect: Rect
    client_rect: Rect
    key_points: dict[str, tuple[int, int]]


_KEY_CENTERS = {
    "A": (160 / 1350, 205 / 377),
    "D": (310 / 1350, 205 / 377),
    "F": (383 / 1350, 205 / 377),
}

_OSK_CLASS = "OSKMainClass"
_HWND_TOPMOST = -1
_SWP_SHOWWINDOW = 0x0040
_WM_CLOSE = 0x0010
_CANONICAL_OUTER_WIDTH = 1365
_CANONICAL_OUTER_HEIGHT = 415
_MIN_CLIENT_WIDTH = 900
_MIN_CLIENT_HEIGHT = 250
_MIN_CLIENT_ASPECT_RATIO = 3.2
_MAX_CLIENT_ASPECT_RATIO = 3.9


class _WinRect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class Win32KeyboardApi:
    def __init__(self, user32: Any | None = None) -> None:
        if user32 is None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
            user32.FindWindowW.restype = wintypes.HWND
            user32.IsWindow.argtypes = (wintypes.HWND,)
            user32.IsWindow.restype = wintypes.BOOL
            user32.IsWindowVisible.argtypes = (wintypes.HWND,)
            user32.IsWindowVisible.restype = wintypes.BOOL
            user32.IsIconic.argtypes = (wintypes.HWND,)
            user32.IsIconic.restype = wintypes.BOOL
            user32.GetClassNameW.argtypes = (
                wintypes.HWND,
                wintypes.LPWSTR,
                ctypes.c_int,
            )
            user32.GetClassNameW.restype = ctypes.c_int
            user32.GetWindowRect.argtypes = (
                wintypes.HWND,
                ctypes.POINTER(_WinRect),
            )
            user32.GetWindowRect.restype = wintypes.BOOL
            user32.GetClientRect.argtypes = (
                wintypes.HWND,
                ctypes.POINTER(_WinRect),
            )
            user32.GetClientRect.restype = wintypes.BOOL
            user32.ClientToScreen.argtypes = (
                wintypes.HWND,
                ctypes.POINTER(_Point),
            )
            user32.ClientToScreen.restype = wintypes.BOOL
            user32.SetWindowPos.argtypes = (
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            )
            user32.SetWindowPos.restype = wintypes.BOOL
            user32.PostMessageW.argtypes = (
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            user32.PostMessageW.restype = wintypes.BOOL
        self.user32 = user32

    def find_window(self) -> int:
        return int(self.user32.FindWindowW(_OSK_CLASS, None) or 0)

    def validate_window(self, hwnd: int) -> None:
        if not self.user32.IsWindow(hwnd):
            raise OnScreenKeyboardError("Windows 屏幕键盘窗口已失效")
        if not self.user32.IsWindowVisible(hwnd):
            raise OnScreenKeyboardError("Windows 屏幕键盘窗口不可见")
        if self.user32.IsIconic(hwnd):
            raise OnScreenKeyboardError("Windows 屏幕键盘窗口已最小化")
        class_name = ctypes.create_unicode_buffer(256)
        if not self.user32.GetClassNameW(hwnd, class_name, len(class_name)):
            raise OnScreenKeyboardError("无法读取 Windows 屏幕键盘窗口类别")
        if class_name.value != _OSK_CLASS:
            raise OnScreenKeyboardError("Windows 屏幕键盘窗口类别不匹配")

    def position_bottom_left(
        self,
        hwnd: int,
        monitor_rect: Rect,
        target_width: int,
    ) -> None:
        width = min(target_width, monitor_rect.width)
        height = round(width * _CANONICAL_OUTER_HEIGHT / _CANONICAL_OUTER_WIDTH)
        if height > monitor_rect.height:
            height = monitor_rect.height
            width = round(
                height * _CANONICAL_OUTER_WIDTH / _CANONICAL_OUTER_HEIGHT
            )
        positioned = self.user32.SetWindowPos(
            hwnd,
            _HWND_TOPMOST,
            monitor_rect.left,
            monitor_rect.bottom - height,
            width,
            height,
            _SWP_SHOWWINDOW,
        )
        if not positioned:
            error_code = ctypes.get_last_error()
            error_type = (
                OnScreenKeyboardPositionDenied
                if error_code == 5
                else OnScreenKeyboardError
            )
            raise error_type(
                "无法将 Windows 屏幕键盘定位到左下角；"
                f"Windows 错误 {error_code}"
            )

    def window_rect(self, hwnd: int) -> Rect:
        native = _WinRect()
        if not self.user32.GetWindowRect(hwnd, ctypes.byref(native)):
            raise OnScreenKeyboardError("无法读取 Windows 屏幕键盘窗口矩形")
        return Rect(
            int(native.left),
            int(native.top),
            int(native.right),
            int(native.bottom),
        )

    def client_rect_on_screen(self, hwnd: int) -> Rect:
        native = _WinRect()
        if not self.user32.GetClientRect(hwnd, ctypes.byref(native)):
            raise OnScreenKeyboardError("无法读取 Windows 屏幕键盘客户区")
        top_left = _Point(native.left, native.top)
        bottom_right = _Point(native.right, native.bottom)
        if not self.user32.ClientToScreen(hwnd, ctypes.byref(top_left)):
            raise OnScreenKeyboardError("无法换算 Windows 屏幕键盘客户区坐标")
        if not self.user32.ClientToScreen(hwnd, ctypes.byref(bottom_right)):
            raise OnScreenKeyboardError("无法换算 Windows 屏幕键盘客户区坐标")
        return Rect(
            int(top_left.x),
            int(top_left.y),
            int(bottom_right.x),
            int(bottom_right.y),
        )

    def close_window(self, hwnd: int) -> None:
        if not self.user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0):
            error_code = ctypes.get_last_error()
            error_type = (
                OnScreenKeyboardCloseDenied
                if error_code == 5
                else OnScreenKeyboardError
            )
            raise error_type(
                "无法关闭本程序启动的 Windows 屏幕键盘；"
                f"Windows 错误 {error_code}"
            )


class OskLauncher:
    def __init__(
        self,
        *,
        shell32: Any | None = None,
        environ: dict[str, str] | os._Environ[str] = os.environ,
    ) -> None:
        if shell32 is None:
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            shell32.ShellExecuteW.argtypes = (
                wintypes.HWND,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                ctypes.c_int,
            )
            shell32.ShellExecuteW.restype = wintypes.HINSTANCE
        self.shell32 = shell32
        self.environ = environ

    def start(self) -> None:
        windows_dir = self.environ.get("WINDIR")
        if not windows_dir:
            raise OnScreenKeyboardError("WINDIR 未配置，无法启动屏幕键盘")
        executable = Path(windows_dir) / "System32" / "osk.exe"
        result = int(
            self.shell32.ShellExecuteW(
                0,
                "open",
                str(executable),
                None,
                None,
                1,
            )
            or 0
        )
        if result <= 32:
            raise OnScreenKeyboardError(
                "启动 Windows 屏幕键盘失败；"
                f"ShellExecute 返回 {result}"
            )


class OnScreenKeyboardWindow:
    def __init__(
        self,
        *,
        api: Any | None = None,
        launcher: Any | None = None,
        sleep: Callable[[float], None] = real_sleep,
        recorder: Any | None = None,
    ) -> None:
        self.api = api or Win32KeyboardApi()
        self.launcher = launcher or OskLauncher()
        self.sleep = sleep
        self.recorder = recorder
        self._owned = False
        self._hwnd = 0
        self._geometry: KeyboardGeometry | None = None
        self._monitor_rect: Rect | None = None
        self._game_client: Rect | None = None

    def ensure(self, monitor_rect: Rect, game_client: Rect) -> KeyboardGeometry:
        hwnd = int(self.api.find_window() or 0)
        if not hwnd:
            self.launcher.start()
            self._owned = True
            for _ in range(50):
                hwnd = int(self.api.find_window() or 0)
                if hwnd:
                    break
                self.sleep(0.1)
        if not hwnd:
            raise OnScreenKeyboardError("启动 Windows 屏幕键盘超时")

        self._hwnd = hwnd
        self._monitor_rect = monitor_rect
        self._game_client = game_client
        position_denied: OnScreenKeyboardPositionDenied | None = None
        try:
            target_width = min(
                monitor_rect.width,
                max(
                    _CANONICAL_OUTER_WIDTH,
                    round(game_client.width * 2 / 3),
                ),
            )
            self.api.position_bottom_left(
                hwnd,
                monitor_rect,
                target_width,
            )
        except OnScreenKeyboardPositionDenied as error:
            position_denied = error
        self.api.validate_window(hwnd)
        self._geometry = self._read_geometry(hwnd)
        try:
            self._validate_placement(self._geometry, monitor_rect, game_client)
        except OnScreenKeyboardError as error:
            if position_denied is not None:
                raise OnScreenKeyboardError(
                    "Windows 不允许程序移动系统屏幕键盘；"
                    "请手动拖到游戏左下角后重新绑定"
                ) from position_denied
            raise error
        return self._geometry

    def geometry(self) -> KeyboardGeometry:
        if not self._hwnd:
            raise OnScreenKeyboardError("Windows 屏幕键盘尚未准备")
        self.api.validate_window(self._hwnd)
        self._geometry = self._read_geometry(self._hwnd)
        if self._monitor_rect is not None and self._game_client is not None:
            self._validate_placement(
                self._geometry,
                self._monitor_rect,
                self._game_client,
            )
        return self._geometry

    def close(self) -> None:
        hwnd = self._hwnd
        self._hwnd = 0
        self._geometry = None
        self._monitor_rect = None
        self._game_client = None
        if self._owned and hwnd:
            try:
                self.api.close_window(hwnd)
            except OnScreenKeyboardCloseDenied:
                # OSK is a protected accessibility window on some Windows builds.
                # Leaving it open is safer than treating cleanup as an automation
                # failure or force-terminating a system-owned process.
                pass
        self._owned = False

    def _read_geometry(self, hwnd: int) -> KeyboardGeometry:
        window_rect = self.api.window_rect(hwnd)
        client_rect = self.api.client_rect_on_screen(hwnd)
        if client_rect.width <= 0 or client_rect.height <= 0:
            raise OnScreenKeyboardError("Windows 屏幕键盘客户区尺寸无效")
        client_aspect_ratio = client_rect.width / client_rect.height
        if (
            client_rect.width < _MIN_CLIENT_WIDTH
            or client_rect.height < _MIN_CLIENT_HEIGHT
            or not (
                _MIN_CLIENT_ASPECT_RATIO
                <= client_aspect_ratio
                <= _MAX_CLIENT_ASPECT_RATIO
            )
        ):
            raise OnScreenKeyboardError(
                "Windows 屏幕键盘布局尺寸异常，已拒绝点击；"
                "请恢复完整键盘布局后重新绑定"
            )
        key_points = {
            key: (
                client_rect.left + round(client_rect.width * x_ratio),
                client_rect.top + round(client_rect.height * y_ratio),
            )
            for key, (x_ratio, y_ratio) in _KEY_CENTERS.items()
        }
        return KeyboardGeometry(
            hwnd=hwnd,
            window_rect=window_rect,
            client_rect=client_rect,
            key_points=key_points,
        )

    @staticmethod
    def _validate_placement(
        geometry: KeyboardGeometry,
        monitor_rect: Rect,
        game_client: Rect,
    ) -> None:
        window = geometry.window_rect
        if not (
            monitor_rect.left <= window.left
            and monitor_rect.top <= window.top
            and window.right <= monitor_rect.right
            and window.bottom <= monitor_rect.bottom
        ):
            raise OnScreenKeyboardError("Windows 屏幕键盘超出目标显示器")
        critical = (
            TOP_ROI.to_pixels(game_client),
            READY_ROI.to_pixels(game_client),
        )
        if any(_intersects(window, region) for region in critical):
            raise OnScreenKeyboardError("Windows 屏幕键盘遮挡关键识别区域")


class OnScreenKeyboardInputBackend:
    def __init__(
        self,
        *,
        window: OnScreenKeyboardWindow,
        mouse: Any,
        recorder: Any | None = None,
    ) -> None:
        self.window = window
        self.mouse = mouse
        self.recorder = recorder
        self._held_key: str | None = None
        self._geometry: KeyboardGeometry | None = None

    def prepare(self, monitor_rect: Rect, game_client: Rect) -> None:
        self._geometry = self.window.ensure(monitor_rect, game_client)
        self._record(
            "osk.prepared",
            hwnd=self._geometry.hwnd,
            window_rect=self._rect_tuple(self._geometry.window_rect),
            client_rect=self._rect_tuple(self._geometry.client_rect),
        )

    def occlusion_rect(self) -> Rect | None:
        if self._geometry is None:
            return None
        self._geometry = self.window.geometry()
        return self._geometry.window_rect

    def key_down(self, key: str) -> None:
        normalized = key.upper()
        if normalized not in _KEY_CENTERS:
            raise ValueError(f"unsupported key: {key!r}")
        if self._held_key == normalized:
            return
        if self._held_key is not None:
            raise OnScreenKeyboardError(
                f"屏幕键盘仍按住 {self._held_key}，不能按下 {normalized}"
            )

        geometry = self.window.geometry()
        self._geometry = geometry
        point = geometry.key_points[normalized]
        self._record(
            "osk.key_target",
            key=normalized,
            x=point[0],
            y=point[1],
        )
        self.mouse.move(*point)
        try:
            self.mouse.down()
        except Exception as down_error:
            self._record(
                "osk.mouse_down",
                key=normalized,
                success=False,
                error=str(down_error),
            )
            try:
                self.mouse.up()
            except Exception as release_error:
                raise OnScreenKeyboardError(
                    f"{down_error}; 清理鼠标抬起失败: {release_error}"
                ) from release_error
            raise
        self._record(
            "osk.mouse_down",
            key=normalized,
            success=True,
        )
        self._held_key = normalized

    def key_up(self, key: str) -> None:
        normalized = key.upper()
        if self._held_key != normalized:
            return
        try:
            self.mouse.up()
        except Exception as error:
            self._record(
                "osk.mouse_up",
                key=normalized,
                success=False,
                error=str(error),
            )
            raise
        self._record(
            "osk.mouse_up",
            key=normalized,
            success=True,
        )
        self._held_key = None

    def click(self, x: int, y: int) -> None:
        self._release_held()
        self.mouse.click(x, y)

    def mouse_up(self) -> None:
        self.mouse.up()
        self._held_key = None

    def close(self) -> None:
        try:
            self._release_held()
        finally:
            self.window.close()

    def _release_held(self) -> None:
        if self._held_key is None:
            return
        key = self._held_key
        try:
            self.mouse.up()
        except Exception as error:
            self._record(
                "osk.mouse_up",
                key=key,
                success=False,
                error=str(error),
            )
            raise
        self._record("osk.mouse_up", key=key, success=True)
        self._held_key = None

    def _record(self, name: str, **fields: Any) -> None:
        if self.recorder is not None:
            self.recorder.event(name, **fields)

    @staticmethod
    def _rect_tuple(rect: Rect) -> tuple[int, int, int, int]:
        return rect.left, rect.top, rect.right, rect.bottom


def _intersects(first: Rect, second: Rect) -> bool:
    return (
        max(first.left, second.left) < min(first.right, second.right)
        and max(first.top, second.top) < min(first.bottom, second.bottom)
    )
