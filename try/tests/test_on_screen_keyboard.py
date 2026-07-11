from __future__ import annotations

from dataclasses import dataclass

from auto_fishing.model import Rect
import pytest

from auto_fishing.platform.on_screen_keyboard import (
    OskLauncher,
    OnScreenKeyboardError,
    OnScreenKeyboardWindow,
    Win32KeyboardApi,
)


MONITOR = Rect(0, 0, 1920, 1080)
GAME_CLIENT = Rect(0, 0, 1920, 1080)


@dataclass
class FakeLauncher:
    started: int = 0

    def start(self) -> None:
        self.started += 1


class FakeKeyboardApi:
    def __init__(
        self,
        find_results: list[int],
        *,
        window_rect: Rect = Rect(0, 665, 1365, 1080),
        client_rect: Rect = Rect(7, 695, 1357, 1072),
    ) -> None:
        self.find_results = list(find_results)
        self._window_rect = window_rect
        self._client_rect = client_rect
        self.positioned: list[tuple[int, Rect]] = []
        self.closed: list[int] = []

    def find_window(self) -> int:
        if len(self.find_results) > 1:
            return self.find_results.pop(0)
        return self.find_results[0]

    def position_bottom_left(self, hwnd: int, monitor_rect: Rect) -> None:
        self.positioned.append((hwnd, monitor_rect))

    def validate_window(self, hwnd: int) -> None:
        assert hwnd in {55, 77}

    def window_rect(self, hwnd: int) -> Rect:
        return self._window_rect

    def client_rect_on_screen(self, hwnd: int) -> Rect:
        return self._client_rect

    def close_window(self, hwnd: int) -> None:
        self.closed.append(hwnd)


def test_ensure_reuses_existing_keyboard_without_owning_it() -> None:
    api = FakeKeyboardApi([55])
    launcher = FakeLauncher()
    keyboard = OnScreenKeyboardWindow(
        api=api,
        launcher=launcher,
        sleep=lambda _: None,
    )

    geometry = keyboard.ensure(MONITOR, GAME_CLIENT)
    keyboard.close()

    assert geometry.hwnd == 55
    assert launcher.started == 0
    assert api.positioned == [(55, MONITOR)]
    assert api.closed == []


def test_ensure_launches_missing_keyboard_and_closes_owned_window() -> None:
    api = FakeKeyboardApi([0, 77])
    launcher = FakeLauncher()
    keyboard = OnScreenKeyboardWindow(
        api=api,
        launcher=launcher,
        sleep=lambda _: None,
    )

    geometry = keyboard.ensure(MONITOR, GAME_CLIENT)
    keyboard.close()

    assert geometry.hwnd == 77
    assert launcher.started == 1
    assert api.closed == [77]


def test_geometry_maps_default_layout_keys_inside_client_rect() -> None:
    client = Rect(-1913, 695, -563, 1072)
    keyboard = OnScreenKeyboardWindow(
        api=FakeKeyboardApi(
            [55],
            window_rect=Rect(-1920, 665, -555, 1080),
            client_rect=client,
        ),
        launcher=FakeLauncher(),
        sleep=lambda _: None,
    )

    geometry = keyboard.ensure(Rect(-1920, 0, 0, 1080), Rect(-1920, 0, 0, 1080))

    a_x, a_y = geometry.key_points["A"]
    d_x, d_y = geometry.key_points["D"]
    f_x, f_y = geometry.key_points["F"]
    assert client.left <= a_x < d_x < f_x < client.right
    assert a_y == d_y == f_y
    assert client.top <= a_y < client.bottom


def test_ensure_rejects_invalid_client_geometry() -> None:
    keyboard = OnScreenKeyboardWindow(
        api=FakeKeyboardApi([55], client_rect=Rect(7, 695, 7, 1072)),
        launcher=FakeLauncher(),
        sleep=lambda _: None,
    )

    with pytest.raises(OnScreenKeyboardError, match="客户区尺寸无效"):
        keyboard.ensure(MONITOR, GAME_CLIENT)


def test_ensure_reports_launch_timeout_without_closing_unknown_window() -> None:
    api = FakeKeyboardApi([0])
    launcher = FakeLauncher()
    keyboard = OnScreenKeyboardWindow(
        api=api,
        launcher=launcher,
        sleep=lambda _: None,
    )

    with pytest.raises(OnScreenKeyboardError, match="启动 Windows 屏幕键盘超时"):
        keyboard.ensure(MONITOR, GAME_CLIENT)
    keyboard.close()

    assert launcher.started == 1
    assert api.closed == []


class FakeUser32:
    def __init__(self) -> None:
        self.positioned: list[tuple[int, int, int, int, int, int]] = []
        self.messages: list[tuple[int, int]] = []

    def FindWindowW(self, class_name: str, title: object) -> int:
        assert class_name == "OSKMainClass"
        assert title is None
        return 55

    def IsWindow(self, hwnd: int) -> int:
        return hwnd == 55

    def IsWindowVisible(self, hwnd: int) -> int:
        return hwnd == 55

    def IsIconic(self, hwnd: int) -> int:
        return 0

    def GetClassNameW(self, hwnd: int, buffer: object, size: int) -> int:
        buffer.value = "OSKMainClass"
        return len(buffer.value)

    def GetWindowRect(self, hwnd: int, rect_pointer: object) -> int:
        rect = rect_pointer._obj
        rect.left, rect.top, rect.right, rect.bottom = 80, 80, 1445, 495
        return 1

    def GetClientRect(self, hwnd: int, rect_pointer: object) -> int:
        rect = rect_pointer._obj
        rect.left, rect.top, rect.right, rect.bottom = 0, 0, 1350, 377
        return 1

    def ClientToScreen(self, hwnd: int, point_pointer: object) -> int:
        point = point_pointer._obj
        point.x += 87
        point.y += 110
        return 1

    def SetWindowPos(
        self,
        hwnd: int,
        insert_after: int,
        x: int,
        y: int,
        width: int,
        height: int,
        flags: int,
    ) -> int:
        self.positioned.append((hwnd, x, y, width, height, flags))
        return 1

    def PostMessageW(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        self.messages.append((hwnd, message))
        return 1


def test_win32_api_reads_valid_keyboard_geometry_and_positions_bottom_left() -> None:
    user32 = FakeUser32()
    api = Win32KeyboardApi(user32=user32)

    hwnd = api.find_window()
    api.validate_window(hwnd)
    api.position_bottom_left(hwnd, Rect(0, 0, 1920, 1080))

    assert api.window_rect(hwnd) == Rect(80, 80, 1445, 495)
    assert api.client_rect_on_screen(hwnd) == Rect(87, 110, 1437, 487)
    assert user32.positioned == [(55, 0, 665, 1365, 415, 0x0040)]


def test_win32_api_closes_owned_keyboard_with_wm_close() -> None:
    user32 = FakeUser32()
    api = Win32KeyboardApi(user32=user32)

    api.close_window(55)

    assert user32.messages == [(55, 0x0010)]


def test_osk_launcher_starts_system32_executable_without_shell() -> None:
    calls: list[tuple[list[str], bool]] = []

    def popen(command: list[str], *, shell: bool) -> object:
        calls.append((command, shell))
        return object()

    launcher = OskLauncher(
        popen=popen,
        environ={"WINDIR": r"C:\Windows"},
    )

    launcher.start()

    assert calls == [([r"C:\Windows\System32\osk.exe"], False)]
