from __future__ import annotations

from dataclasses import dataclass

from auto_fishing.model import Rect
import pytest

from auto_fishing.platform.on_screen_keyboard import (
    KeyboardGeometry,
    OskLauncher,
    OnScreenKeyboardError,
    OnScreenKeyboardInputBackend,
    OnScreenKeyboardPositionDenied,
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
        position_error: Exception | None = None,
    ) -> None:
        self.find_results = list(find_results)
        self._window_rect = window_rect
        self._client_rect = client_rect
        self.position_error = position_error
        self.positioned: list[tuple[int, Rect, int]] = []
        self.closed: list[int] = []

    def find_window(self) -> int:
        if len(self.find_results) > 1:
            return self.find_results.pop(0)
        return self.find_results[0]

    def position_bottom_left(
        self,
        hwnd: int,
        monitor_rect: Rect,
        max_width: int,
    ) -> None:
        self.positioned.append((hwnd, monitor_rect, max_width))
        if self.position_error is not None:
            raise self.position_error

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
    assert api.positioned == [(55, MONITOR, 1536)]
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
        self.position_result = 1

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
        return self.position_result

    def PostMessageW(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        self.messages.append((hwnd, message))
        return 1


def test_win32_api_reads_valid_keyboard_geometry_and_positions_bottom_left() -> None:
    user32 = FakeUser32()
    api = Win32KeyboardApi(user32=user32)

    hwnd = api.find_window()
    api.validate_window(hwnd)
    api.position_bottom_left(hwnd, Rect(0, 0, 1920, 1080), 1000)

    assert api.window_rect(hwnd) == Rect(80, 80, 1445, 495)
    assert api.client_rect_on_screen(hwnd) == Rect(87, 110, 1437, 487)
    assert user32.positioned == [(55, 0, 776, 1000, 304, 0x0040)]


def test_win32_api_closes_owned_keyboard_with_wm_close() -> None:
    user32 = FakeUser32()
    api = Win32KeyboardApi(user32=user32)

    api.close_window(55)

    assert user32.messages == [(55, 0x0010)]


def test_win32_api_distinguishes_access_denied_position_error(monkeypatch) -> None:
    user32 = FakeUser32()
    user32.position_result = 0
    monkeypatch.setattr("ctypes.get_last_error", lambda: 5)
    api = Win32KeyboardApi(user32=user32)

    with pytest.raises(OnScreenKeyboardPositionDenied, match="Windows 错误 5"):
        api.position_bottom_left(55, MONITOR, 1000)


class FakeShell32:
    def __init__(self, result: int = 33) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    def ShellExecuteW(
        self,
        owner: int,
        operation: str,
        executable: str,
        parameters: object,
        directory: object,
        show: int,
    ) -> int:
        self.calls.append(
            (owner, operation, executable, parameters, directory, show)
        )
        return self.result


def test_osk_launcher_uses_windows_shell_for_accessibility_executable() -> None:
    shell32 = FakeShell32()
    launcher = OskLauncher(
        shell32=shell32,
        environ={"WINDIR": r"C:\Windows"},
    )

    launcher.start()

    assert shell32.calls == [
        (
            0,
            "open",
            r"C:\Windows\System32\osk.exe",
            None,
            None,
            1,
        )
    ]


def test_osk_launcher_reports_shell_execute_failure_code() -> None:
    launcher = OskLauncher(
        shell32=FakeShell32(result=5),
        environ={"WINDIR": r"C:\Windows"},
    )

    with pytest.raises(OnScreenKeyboardError, match="ShellExecute 返回 5"):
        launcher.start()


@pytest.mark.parametrize(
    "window_rect",
    [
        Rect(400, 0, 1400, 400),
        Rect(0, 650, 1800, 1080),
    ],
)
def test_ensure_rejects_keyboard_over_critical_recognition_regions(
    window_rect: Rect,
) -> None:
    keyboard = OnScreenKeyboardWindow(
        api=FakeKeyboardApi([55], window_rect=window_rect),
        launcher=FakeLauncher(),
        sleep=lambda _: None,
    )

    with pytest.raises(OnScreenKeyboardError, match="遮挡关键识别区域"):
        keyboard.ensure(MONITOR, Rect(0, 0, 1920, 1080))


def test_ensure_accepts_safe_existing_position_when_windows_denies_move() -> None:
    api = FakeKeyboardApi(
        [55],
        position_error=OnScreenKeyboardPositionDenied("Windows 错误 5"),
    )
    keyboard = OnScreenKeyboardWindow(
        api=api,
        launcher=FakeLauncher(),
        sleep=lambda _: None,
    )

    geometry = keyboard.ensure(MONITOR, GAME_CLIENT)

    assert geometry.window_rect == Rect(0, 665, 1365, 1080)


def test_ensure_keeps_owned_handle_when_manual_position_is_required() -> None:
    api = FakeKeyboardApi(
        [0, 77],
        window_rect=Rect(400, 0, 1400, 400),
        position_error=OnScreenKeyboardPositionDenied("Windows 错误 5"),
    )
    keyboard = OnScreenKeyboardWindow(
        api=api,
        launcher=FakeLauncher(),
        sleep=lambda _: None,
    )

    with pytest.raises(OnScreenKeyboardError, match="请手动拖到游戏左下角"):
        keyboard.ensure(MONITOR, GAME_CLIENT)
    keyboard.close()

    assert api.closed == [77]


class ReadyKeyboard:
    def __init__(self) -> None:
        self.prepared: list[tuple[Rect, Rect]] = []
        self.closed = 0
        self.current = KeyboardGeometry(
            hwnd=55,
            window_rect=Rect(0, 665, 1365, 1080),
            client_rect=Rect(0, 0, 1350, 377),
            key_points={"A": (160, 205), "D": (310, 205), "F": (383, 205)},
        )

    def ensure(self, monitor_rect: Rect, game_client: Rect) -> KeyboardGeometry:
        self.prepared.append((monitor_rect, game_client))
        return self.current

    def geometry(self) -> KeyboardGeometry:
        return self.current

    def close(self) -> None:
        self.closed += 1


class RecordingMouse:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []
        self.fail_down = False
        self.fail_up = 0

    def move(self, x: int, y: int) -> None:
        self.events.append(("move", x, y))

    def down(self) -> None:
        self.events.append(("down",))
        if self.fail_down:
            raise OnScreenKeyboardError("mouse down failed")

    def up(self) -> None:
        self.events.append(("up",))
        if self.fail_up:
            self.fail_up -= 1
            raise OnScreenKeyboardError("mouse up failed")

    def click(self, x: int, y: int) -> None:
        self.events.append(("click", x, y))


class RecordingLog:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def event(self, name: str, **fields: object) -> None:
        self.events.append({"event": name, **fields})


def test_keyboard_backend_holds_direction_and_releases_before_switch() -> None:
    mouse = RecordingMouse()
    backend = OnScreenKeyboardInputBackend(window=ReadyKeyboard(), mouse=mouse)

    backend.key_down("A")
    backend.key_up("A")
    backend.key_down("D")

    assert mouse.events == [
        ("move", 160, 205),
        ("down",),
        ("up",),
        ("move", 310, 205),
        ("down",),
    ]


def test_keyboard_backend_f_down_and_up_are_balanced() -> None:
    mouse = RecordingMouse()
    backend = OnScreenKeyboardInputBackend(window=ReadyKeyboard(), mouse=mouse)

    backend.key_down("F")
    backend.key_up("F")

    assert mouse.events == [("move", 383, 205), ("down",), ("up",)]


def test_keyboard_backend_prepares_geometry_and_reports_occlusion() -> None:
    window = ReadyKeyboard()
    backend = OnScreenKeyboardInputBackend(window=window, mouse=RecordingMouse())

    backend.prepare(MONITOR, GAME_CLIENT)

    assert window.prepared == [(MONITOR, GAME_CLIENT)]
    assert backend.occlusion_rect() == Rect(0, 665, 1365, 1080)


def test_keyboard_backend_cleans_up_after_mouse_down_failure() -> None:
    mouse = RecordingMouse()
    mouse.fail_down = True
    backend = OnScreenKeyboardInputBackend(window=ReadyKeyboard(), mouse=mouse)

    with pytest.raises(OnScreenKeyboardError, match="mouse down failed"):
        backend.key_down("A")

    assert mouse.events == [("move", 160, 205), ("down",), ("up",)]


def test_keyboard_backend_retries_failed_key_release() -> None:
    mouse = RecordingMouse()
    mouse.fail_up = 1
    backend = OnScreenKeyboardInputBackend(window=ReadyKeyboard(), mouse=mouse)
    backend.key_down("D")

    with pytest.raises(OnScreenKeyboardError, match="mouse up failed"):
        backend.key_up("D")
    backend.key_up("D")

    assert mouse.events[-2:] == [("up",), ("up",)]


def test_keyboard_backend_releases_direction_before_direct_click() -> None:
    mouse = RecordingMouse()
    backend = OnScreenKeyboardInputBackend(window=ReadyKeyboard(), mouse=mouse)
    backend.key_down("A")

    backend.click(1500, 600)

    assert mouse.events[-2:] == [("up",), ("click", 1500, 600)]


def test_keyboard_backend_closes_window_after_releasing_mouse() -> None:
    window = ReadyKeyboard()
    mouse = RecordingMouse()
    backend = OnScreenKeyboardInputBackend(window=window, mouse=mouse)
    backend.key_down("D")

    backend.close()

    assert mouse.events[-1] == ("up",)
    assert window.closed == 1


def test_keyboard_backend_records_geometry_target_and_balanced_mouse() -> None:
    recorder = RecordingLog()
    backend = OnScreenKeyboardInputBackend(
        window=ReadyKeyboard(),
        mouse=RecordingMouse(),
        recorder=recorder,
    )

    backend.prepare(MONITOR, GAME_CLIENT)
    backend.key_down("F")
    backend.key_up("F")

    assert recorder.events == [
        {
            "event": "osk.prepared",
            "hwnd": 55,
            "window_rect": (0, 665, 1365, 1080),
            "client_rect": (0, 0, 1350, 377),
        },
        {"event": "osk.key_target", "key": "F", "x": 383, "y": 205},
        {"event": "osk.mouse_down", "key": "F", "success": True},
        {"event": "osk.mouse_up", "key": "F", "success": True},
    ]
