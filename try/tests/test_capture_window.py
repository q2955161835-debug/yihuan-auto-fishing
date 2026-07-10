from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable

import numpy as np
import pytest

from auto_fishing.capture.dxcam_source import DxcamFrameSource
from auto_fishing.model import Rect
from auto_fishing.platform.hotkey import GlobalHotkey, WM_HOTKEY
from auto_fishing.platform.windowing import BoundWindow, WindowBindingError, WindowService


class FakeCamera:
    def __init__(self, results: list[tuple[np.ndarray, float] | None] | None = None) -> None:
        self.results = list(results or [])
        self.started_fps: int | None = None
        self.stop_calls = 0
        self.release_calls = 0

    def start(self, target_fps: int) -> None:
        self.started_fps = target_fps

    def get_latest_frame(self, with_timestamp: bool = True):
        assert with_timestamp is True
        return self.results.pop(0) if self.results else None

    def stop(self) -> None:
        self.stop_calls += 1

    def release(self) -> None:
        self.release_calls += 1


def test_capture_uses_latest_frame_and_reports_rate() -> None:
    camera = FakeCamera(
        [
            (np.zeros((10, 10, 3), dtype=np.uint8), 1.0),
            (np.ones((10, 10, 3), dtype=np.uint8), 1.04),
        ]
    )
    source = DxcamFrameSource(camera_factory=lambda _: camera)

    source.start(0)
    source.latest()
    packet = source.latest()

    assert camera.started_fps == 30
    assert packet.frame.mean() == 1
    assert packet.timestamp == 1.04
    assert packet.fps == pytest.approx(25.0)


def test_capture_reuses_last_packet_when_dxcam_has_no_new_frame() -> None:
    frame = np.ones((2, 2, 3), dtype=np.uint8)
    camera = FakeCamera([(frame, 1.0), None])
    source = DxcamFrameSource(camera_factory=lambda _: camera)
    source.start(0)

    first = source.latest()
    second = source.latest()

    assert second is first
    assert second.frame is frame


def test_capture_reports_missing_initial_frame() -> None:
    source = DxcamFrameSource(camera_factory=lambda _: FakeCamera([None]))
    source.start(0)

    with pytest.raises(RuntimeError, match="暂无可用截屏帧"):
        source.latest()


def test_capture_repeated_start_releases_old_camera_and_stop_is_idempotent() -> None:
    cameras = [FakeCamera(), FakeCamera()]
    source = DxcamFrameSource(camera_factory=lambda _: cameras.pop(0))

    source.start(0)
    first = source.camera
    source.start(1)

    assert first.stop_calls == 1
    assert first.release_calls == 1

    second = source.camera
    source.stop()
    source.stop()

    assert second.stop_calls == 1
    assert second.release_calls == 1


class FakeUser32:
    def __init__(self) -> None:
        self.foreground = 100
        self.iconic: set[int] = set()
        self.titles = {100: "异环"}
        self.client_sizes = {100: (1920, 1080)}
        self.client_origins = {100: (100, 50)}
        self.window_monitors = {100: 22}
        self.monitors = [(-1920, 0, 0, 1080, 11), (0, 0, 1920, 1080, 22)]
        self.valid_windows = {100}
        self.dpi_context_result = True
        self.dpi_fallback_calls = 0
        self.affinity_result = True
        self.activated_hwnd: int | None = None

    def SetProcessDpiAwarenessContext(self, _context) -> bool:
        return self.dpi_context_result

    def SetProcessDPIAware(self) -> bool:
        self.dpi_fallback_calls += 1
        return True

    def GetForegroundWindow(self) -> int:
        return self.foreground

    def IsWindow(self, hwnd: int) -> bool:
        return hwnd in self.valid_windows

    def IsIconic(self, hwnd: int) -> bool:
        return hwnd in self.iconic

    def GetWindowTextLengthW(self, hwnd: int) -> int:
        return len(self.titles.get(hwnd, ""))

    def GetWindowTextW(self, hwnd: int, buffer, size: int) -> int:
        title = self.titles.get(hwnd, "")[: max(0, size - 1)]
        buffer.value = title
        return len(title)

    def GetClientRect(self, hwnd: int, rect_pointer) -> bool:
        if hwnd not in self.client_sizes:
            return False
        width, height = self.client_sizes[hwnd]
        rect = rect_pointer._obj
        rect.left, rect.top, rect.right, rect.bottom = 0, 0, width, height
        return True

    def ClientToScreen(self, hwnd: int, point_pointer) -> bool:
        if hwnd not in self.client_origins:
            return False
        point = point_pointer._obj
        origin_x, origin_y = self.client_origins[hwnd]
        point.x += origin_x
        point.y += origin_y
        return True

    def MonitorFromWindow(self, hwnd: int, _flags: int) -> int:
        return self.window_monitors.get(hwnd, 0)

    def EnumDisplayMonitors(self, _hdc, _clip, callback, _data) -> bool:
        for left, top, right, bottom, handle in self.monitors:
            rect = callback._rect_type(left, top, right, bottom)
            if not callback(handle, 0, ctypes.pointer(rect), 0):
                break
        return True

    def ShowWindow(self, hwnd: int, _command: int) -> bool:
        self.activated_hwnd = hwnd
        return True

    def SetForegroundWindow(self, hwnd: int) -> bool:
        self.foreground = hwnd
        return True

    def SetWindowDisplayAffinity(self, _hwnd: int, _affinity: int) -> bool:
        return self.affinity_result


def make_window_service(
    user32: FakeUser32,
    outputs: list[tuple[int, int]] | None = None,
    own_hwnd: int | None = None,
) -> WindowService:
    return WindowService(
        user32=user32,
        own_hwnd=own_hwnd,
        output_resolutions=lambda: outputs
        if outputs is not None
        else [(1920, 1080), (1920, 1080)],
    )


def test_bind_foreground_returns_screen_client_rect_and_unique_output() -> None:
    user32 = FakeUser32()
    service = make_window_service(user32)

    bound = service.bind_foreground()

    assert bound == BoundWindow(
        hwnd=100,
        title="异环",
        client_rect=Rect(100, 50, 2020, 1130),
        monitor_rect=Rect(0, 0, 1920, 1080),
        output_index=1,
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda api: setattr(api, "foreground", 0), "没有可绑定的前台窗口"),
        (lambda api: api.iconic.add(100), "窗口已最小化"),
        (lambda api: api.titles.update({100: ""}), "窗口标题为空"),
        (lambda api: api.client_sizes.update({100: (959, 540)}), "客户区尺寸过小"),
    ],
)
def test_bind_foreground_rejects_unsafe_targets(
    change: Callable[[FakeUser32], None], message: str
) -> None:
    user32 = FakeUser32()
    change(user32)

    with pytest.raises(WindowBindingError, match=message):
        make_window_service(user32).bind_foreground()


def test_bind_foreground_rejects_own_control_window() -> None:
    user32 = FakeUser32()

    with pytest.raises(WindowBindingError, match="不能绑定控制窗口"):
        make_window_service(user32, own_hwnd=100).bind_foreground()


@pytest.mark.parametrize(
    "outputs",
    [[(1920, 1080)], [(1920, 1080), (2560, 1440)]],
)
def test_bind_foreground_rejects_missing_or_mismatched_dxcam_output(
    outputs: list[tuple[int, int]],
) -> None:
    with pytest.raises(WindowBindingError, match="无法映射游戏所在显示器"):
        make_window_service(FakeUser32(), outputs=outputs).bind_foreground()


def test_refresh_rechecks_original_window_and_activate_verifies_foreground() -> None:
    user32 = FakeUser32()
    service = make_window_service(user32)
    original = service.bind_foreground()
    user32.client_origins[100] = (120, 80)

    refreshed = service.refresh(original)

    assert refreshed.client_rect == Rect(120, 80, 2040, 1160)
    user32.foreground = 200
    assert service.is_foreground(original) is False
    assert service.activate(original) is True
    assert user32.activated_hwnd == 100


def test_refresh_rejects_destroyed_window() -> None:
    user32 = FakeUser32()
    service = make_window_service(user32)
    bound = service.bind_foreground()
    user32.valid_windows.clear()

    with pytest.raises(WindowBindingError, match="窗口已失效"):
        service.refresh(bound)


def test_dpi_awareness_falls_back_and_capture_exclusion_reports_result() -> None:
    user32 = FakeUser32()
    user32.dpi_context_result = False
    service = make_window_service(user32)

    service.enable_dpi_awareness()

    assert user32.dpi_fallback_calls == 1
    assert service.exclude_from_capture(123) is True


class FakeHotkeyUser32:
    def __init__(self, register_result: bool = True) -> None:
        self.register_result = register_result
        self.messages: list[int] = []
        self.condition = threading.Condition()
        self.unregister_calls = 0

    def RegisterHotKey(self, _window, hotkey_id: int, modifiers: int, key: int) -> bool:
        assert (hotkey_id, modifiers, key) == (1, 0, 0x77)
        return self.register_result

    def GetMessageW(self, message_pointer, _window, _minimum: int, _maximum: int) -> int:
        with self.condition:
            self.condition.wait_for(lambda: bool(self.messages), timeout=1)
            if not self.messages:
                return -1
            message = self.messages.pop(0)
        if message == 0x0012:
            return 0
        message_pointer._obj.message = message
        return 1

    def PostThreadMessageW(self, _thread_id: int, message: int, _wparam: int, _lparam: int) -> bool:
        with self.condition:
            self.messages.append(message)
            self.condition.notify_all()
        return True

    def UnregisterHotKey(self, _window, hotkey_id: int) -> bool:
        assert hotkey_id == 1
        self.unregister_calls += 1
        return True

    def emit_hotkey(self) -> None:
        with self.condition:
            self.messages.append(WM_HOTKEY)
            self.condition.notify_all()


class FakeKernel32:
    def GetCurrentThreadId(self) -> int:
        return 4321


def test_hotkey_registration_failure_returns_false_without_callback() -> None:
    callback_called = threading.Event()
    api = FakeHotkeyUser32(register_result=False)
    hotkey = GlobalHotkey(user32=api, kernel32=FakeKernel32())

    assert hotkey.start(callback_called.set) is False
    hotkey.stop()

    assert callback_called.is_set() is False
    assert api.unregister_calls == 0


def test_hotkey_dispatches_f8_and_stops_message_thread_reliably() -> None:
    callback_called = threading.Event()
    api = FakeHotkeyUser32()
    hotkey = GlobalHotkey(user32=api, kernel32=FakeKernel32())

    assert hotkey.start(callback_called.set) is True
    api.emit_hotkey()

    assert callback_called.wait(timeout=1)
    hotkey.stop()
    hotkey.stop()

    assert api.unregister_calls == 1
    assert hotkey.is_running is False
