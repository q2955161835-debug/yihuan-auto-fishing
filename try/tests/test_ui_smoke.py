from pathlib import Path
import subprocess
import sys
import threading
import tkinter as tk

import pytest

from auto_fishing.app import (
    DEFAULT_DATA_DIR,
    AppController,
    Application,
    ApplicationServices,
)
from auto_fishing.platform.on_screen_keyboard import OnScreenKeyboardInputBackend
from auto_fishing.product import v2_profile
from auto_fishing.model import FishingState, RuntimeSnapshot
from auto_fishing.storage.memory_diagnostics import MemoryDiagnosticRecorder
from auto_fishing.storage.diagnostic_bundles import DiagnosticReportResult
from auto_fishing.storage.settings import AppSettings
from auto_fishing.ui.main_window import MainWindow


class FakeController:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.bind_callbacks = None
        self.bind_start_callbacks = None
        self.rebind_callbacks = None
        self.start_callbacks = None
        self.resume_callbacks = None

    def bind_after_countdown(self, on_tick, on_done) -> None:
        self.calls.append("bind")
        self.bind_callbacks = (on_tick, on_done)

    def bind_and_start_after_countdown(
        self, target, on_tick, on_done
    ) -> None:
        self.calls.append(("bind_and_start", target))
        self.bind_start_callbacks = (on_tick, on_done)

    def rebind(self, on_tick, on_done) -> None:
        self.calls.append("rebind")
        self.rebind_callbacks = (on_tick, on_done)

    def start(self, target: int, *, activate: bool = False) -> None:
        self.calls.append(
            ("start", target, True) if activate else ("start", target)
        )

    def start_after_countdown(self, target, on_tick, on_done) -> None:
        self.calls.append(("start_after_countdown", target))
        self.start_callbacks = (on_tick, on_done)

    def pause(self, reason: str = "按钮暂停") -> None:
        self.calls.append("pause")

    def resume(self, *, activate: bool = False) -> None:
        self.calls.append(("resume", True) if activate else "resume")

    def resume_after_countdown(self, on_tick, on_done) -> None:
        self.calls.append("resume_after_countdown")
        self.resume_callbacks = (on_tick, on_done)

    def cancel_current(self) -> None:
        self.calls.append("cancel_current")

    def shutdown(self) -> None:
        self.calls.append("shutdown")

    def report_error(self) -> None:
        self.calls.append("report_error")

    def open_report_location(self, path: Path) -> None:
        self.calls.append(("open_report_location", path))

    def subscribe(self, callback) -> None:
        self.callback = callback


class FakeSettings:
    def __init__(self, loaded: AppSettings | None = None) -> None:
        self.saved: AppSettings | None = None
        self.loaded = loaded or AppSettings()

    def load(self) -> AppSettings:
        return self.loaded

    def save(self, settings: AppSettings) -> None:
        self.saved = settings


@pytest.fixture(scope="module")
def tk_master():
    master = tk.Tk()
    master.withdraw()
    yield master
    master.destroy()


@pytest.fixture
def root(tk_master):
    window = tk.Toplevel(tk_master)
    window.withdraw()
    yield window
    if window.winfo_exists():
        window.destroy()


def test_window_is_topmost_and_validates_count(root) -> None:
    root.withdraw()
    controller = FakeController()
    window = MainWindow(
        root,
        controller,
        FakeSettings(AppSettings(auto_activate_game=False)),
    )
    root.update_idletasks()
    assert root.attributes("-topmost") == 1

    window.count_var.set("0")
    window.on_start()
    assert controller.calls == []

    window.count_var.set("3")
    window.on_start()
    assert controller.calls == []

    window.on_rebind()
    assert controller.rebind_callbacks is not None
    _on_tick, on_done = controller.rebind_callbacks
    on_done("异环", None)
    controller.calls.clear()
    window.on_start()
    assert controller.calls == [("start_after_countdown", 3)]
    assert controller.start_callbacks is not None
    on_tick, on_done = controller.start_callbacks
    for seconds in (3, 2, 1):
        on_tick(seconds)
        assert window.state_var.get() == f"开始倒计时：{seconds}"
        assert window.error_var.get() == "请在倒计时结束前切回已绑定的游戏窗口"
        assert window.start_button.instate(["disabled"])
        assert window.count_spinbox.instate(["disabled"])
    on_done("请在倒计时结束前切回已绑定的游戏窗口")
    assert window.error_var.get() == "请在倒计时结束前切回已绑定的游戏窗口"
    assert not window.start_button.instate(["disabled"])


def test_successful_start_countdown_keeps_controls_locked_until_snapshot(root) -> None:
    controller = FakeController()
    window = MainWindow(
        root,
        controller,
        FakeSettings(AppSettings(auto_activate_game=False)),
    )
    window.on_rebind()
    assert controller.rebind_callbacks is not None
    _on_tick, on_bind_done = controller.rebind_callbacks
    on_bind_done("异环", None)

    window.on_start()
    assert controller.start_callbacks is not None
    _on_tick, on_start_done = controller.start_callbacks
    on_start_done(None)

    assert window.start_button.instate(["disabled"])
    assert window.count_spinbox.instate(["disabled"])
    assert window.bind_button.instate(["disabled"])


def test_window_geometry_supports_negative_monitor_coordinates(root) -> None:
    controller = FakeController()
    original_geometry = root.geometry
    requested: list[str] = []
    root.geometry = lambda value: requested.append(value)  # type: ignore[method-assign]
    try:
        MainWindow(
            root,
            controller,
            FakeSettings(AppSettings(window_x=-1920, window_y=20)),
        )
    finally:
        root.geometry = original_geometry  # type: ignore[method-assign]

    assert requested == ["400x240-1920+20"]


def test_window_geometry_leaves_room_for_right_side_status(root) -> None:
    controller = FakeController()
    requested: list[str] = []
    original_geometry = root.geometry
    root.geometry = lambda value: requested.append(value)  # type: ignore[method-assign]
    try:
        MainWindow(root, controller, FakeSettings())
    finally:
        root.geometry = original_geometry  # type: ignore[method-assign]

    assert requested == ["400x240+20+20"]


def test_binding_callbacks_update_visible_status(root) -> None:
    root.withdraw()
    controller = FakeController()
    window = MainWindow(root, controller, FakeSettings())
    window.on_bind()
    assert controller.calls == [("bind_and_start", 1)]
    assert window.start_button.instate(["disabled"])
    window.on_start()
    assert controller.calls == [("bind_and_start", 1)]
    assert controller.bind_start_callbacks is not None
    on_tick, on_done = controller.bind_start_callbacks

    for seconds in (3, 2, 1):
        on_tick(seconds)
        assert window.binding_var.get() == f"绑定倒计时：{seconds}"
    on_done("异环", None)
    assert window.binding_var.get() == "已绑定：异环"

    assert window.bind_button.instate(["disabled"])
    assert window.rebind_button.instate(["disabled"])


def test_bind_button_uses_one_countdown_then_marks_runtime_active(root) -> None:
    controller = FakeController()
    window = MainWindow(root, controller, FakeSettings())
    window.count_var.set("2")

    assert window.bind_button.cget("text") == "绑定并开始"
    window.on_bind()

    assert controller.calls == [("bind_and_start", 2)]
    assert controller.bind_start_callbacks is not None
    on_tick, on_done = controller.bind_start_callbacks
    on_tick(3)
    assert window.binding_var.get() == "绑定倒计时：3"
    on_done("异环", None)

    assert window.binding_var.get() == "已绑定：异环"
    assert window.start_button.instate(["disabled"])
    assert window.pause_button.instate(["!disabled"])


def test_start_and_resume_use_explicit_activation_when_enabled(root) -> None:
    controller = FakeController()
    window = MainWindow(root, controller, FakeSettings())
    window._has_binding = True
    window._refresh_control_states()

    window.on_start()
    assert controller.calls[-1] == ("start", 1, True)

    window.apply_snapshot(RuntimeSnapshot(FishingState.PAUSED, 0, 1, 30.0))
    window.on_pause_or_resume()
    assert controller.calls[-1] == ("resume", True)


def test_start_and_resume_keep_manual_countdown_when_activation_disabled(
    root,
) -> None:
    controller = FakeController()
    settings = FakeSettings(AppSettings(auto_activate_game=False))
    window = MainWindow(root, controller, settings)
    window._has_binding = True
    window._refresh_control_states()

    window.on_start()
    assert controller.calls[-1] == ("start_after_countdown", 1)

    window._on_start_done("测试取消")
    window.apply_snapshot(RuntimeSnapshot(FishingState.PAUSED, 0, 1, 30.0))
    window.on_pause_or_resume()
    assert controller.calls[-1] == "resume_after_countdown"


def test_snapshot_is_queued_on_tk_thread_and_locks_runtime_controls(root) -> None:
    root.withdraw()
    controller = FakeController()
    window = MainWindow(
        root,
        controller,
        FakeSettings(AppSettings(auto_activate_game=False)),
    )
    original_after = root.after
    scheduled: list[tuple[int, object]] = []
    root.after = lambda delay, callback: scheduled.append((delay, callback))  # type: ignore[method-assign]
    try:
        snapshot = RuntimeSnapshot(
            state=FishingState.PAUSED,
            completed=2,
            target=7,
            fps=29.75,
            error="窗口失去前台",
        )
        controller.callback(snapshot)
        assert scheduled and scheduled[0][0] == 0
        assert window.state_var.get() == FishingState.UNBOUND.value

        scheduled[0][1]()  # type: ignore[operator]
        assert window.state_var.get() == FishingState.PAUSED.value
        assert window.progress_var.get() == "2/7"
        assert window.fps_var.get() == "29.8 FPS"
        assert window.error_var.get() == "窗口失去前台"
        assert window.count_spinbox.instate(["disabled"])
        assert window.bind_button.instate(["disabled"])
        assert not window.rebind_button.instate(["disabled"])
        assert window.pause_button.cget("text") == "继续"

        window.on_pause_or_resume()
        assert controller.calls[-1] == "resume_after_countdown"
        assert controller.resume_callbacks is not None
        on_tick, on_done = controller.resume_callbacks
        on_tick(3)
        assert window.state_var.get() == "继续倒计时：3"
        assert window.error_var.get() == "请在倒计时结束前切回已绑定的游戏窗口"
        assert window.pause_button.instate(["disabled"])
        on_done(None)
        window.apply_snapshot(
            RuntimeSnapshot(FishingState.READY, 0, 7, 30.0)
        )
        window.on_pause_or_resume()
        assert controller.calls[-1] == "pause"
    finally:
        root.after = original_after  # type: ignore[method-assign]


def test_paused_window_rebind_cancels_current_round_before_countdown(root) -> None:
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    controller = AppController(engine, BindingService(), scheduler)
    window = MainWindow(root, controller, FakeSettings())
    engine.publish(
        RuntimeSnapshot(FishingState.PAUSED, 0, 2, 0.0, "窗口已失效")
    )
    engine.running = True
    window.apply_snapshot(
        RuntimeSnapshot(FishingState.PAUSED, 0, 2, 0.0, "窗口已失效")
    )

    window.on_rebind()

    assert "cancel_current" in engine.calls
    assert window.binding_var.get() == "绑定倒计时：3"


def test_start_can_be_blocked_when_f8_registration_fails(root) -> None:
    root.withdraw()
    controller = FakeController()
    window = MainWindow(root, controller, FakeSettings())
    window.block_start("F8 注册失败，请关闭占用 F8 的程序")
    window.count_var.set("3")
    window.on_start()
    assert controller.calls == []
    assert window.start_button.instate(["disabled"])
    assert window.error_var.get() == "F8 注册失败，请关闭占用 F8 的程序"


def test_close_saves_position_and_shuts_down_controller(root) -> None:
    root.withdraw()
    controller = FakeController()
    settings = FakeSettings()
    window = MainWindow(root, controller, settings)
    root.geometry("320x240+41+52")
    root.update_idletasks()
    window.count_var.set("9")

    window.close()

    assert controller.calls[-1] == "shutdown"
    assert settings.saved == AppSettings(target_count=9, window_x=41, window_y=52)
    assert root.winfo_exists() == 0


def test_auto_activate_setting_is_visible_and_saved(root) -> None:
    root.withdraw()
    controller = FakeController()
    settings = FakeSettings(AppSettings(auto_activate_game=False))
    window = MainWindow(root, controller, settings)

    assert window.auto_activate_var.get() is False
    assert window.auto_activate_check.cget("text") == "自动切回游戏"

    window.auto_activate_var.set(True)
    window.close()

    assert settings.saved is not None
    assert settings.saved.auto_activate_game is True


class ManualScheduler:
    def __init__(self) -> None:
        self.delays: list[int] = []
        self.pending: list[object] = []
        self.pending_delays: list[int] = []
        self.calling_threads: list[int] = []

    def __call__(self, delay: int, callback) -> None:
        self.delays.append(delay)
        self.pending.append(callback)
        self.pending_delays.append(delay)
        self.calling_threads.append(threading.get_ident())

    def run_next(self, delay: int | None = None) -> None:
        index = 0 if delay is None else self.pending_delays.index(delay)
        callback = self.pending.pop(index)
        self.pending_delays.pop(index)
        callback()  # type: ignore[operator]


class BridgeEngine:
    def __init__(self, events: list[object] | None = None) -> None:
        self.calls: list[object] = []
        self.events = events
        self.running = False

    @property
    def is_running(self) -> bool:
        return self.running

    def subscribe(self, callback) -> None:
        self.calls.append(("subscribe", callback))
        self.subscriber = callback

    def publish(self, snapshot: RuntimeSnapshot) -> None:
        self.subscriber(snapshot)

    def bind(self, bound) -> None:
        self.calls.append(("bind", bound))

    def start(self, target: int, *, activate: bool = False) -> None:
        self.calls.append(
            ("start", target, True) if activate else ("start", target)
        )

    def pause(self, reason: str) -> None:
        self.calls.append(("pause", reason))

    def resume(self, *, activate: bool = False) -> None:
        self.calls.append(("resume", True) if activate else "resume")

    def cancel_current(self) -> None:
        self.calls.append("cancel_current")

    def shutdown(self) -> None:
        self.calls.append("shutdown")
        if self.events is not None:
            self.events.append("engine.shutdown")


class RunningStartEngine(BridgeEngine):
    def start(self, target: int, *, activate: bool = False) -> None:
        self.running = True
        super().start(target, activate=activate)


class BlockingPauseEngine(BridgeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.pause_entered = threading.Event()
        self.allow_pause = threading.Event()
        self.shutdown_entered = threading.Event()

    def pause(self, reason: str) -> None:
        self.pause_entered.set()
        if not self.allow_pause.wait(1.0):
            raise RuntimeError("test did not release pause")
        super().pause(reason)

    def shutdown(self) -> None:
        self.shutdown_entered.set()
        super().shutdown()


class BlockingShutdownEngine(BridgeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.shutdown_entered = threading.Event()
        self.allow_shutdown = threading.Event()

    def shutdown(self) -> None:
        self.shutdown_entered.set()
        if not self.allow_shutdown.wait(1.0):
            raise RuntimeError("test did not release shutdown")
        super().shutdown()


class PublishingShutdownEngine(BridgeEngine):
    def shutdown(self) -> None:
        publish_thread = threading.Thread(
            target=lambda: self.subscriber(
                RuntimeSnapshot(FishingState.PAUSED, 0, 2, 0.0)
            )
        )
        publish_thread.start()
        publish_thread.join(0.2)
        if publish_thread.is_alive():
            raise RuntimeError("shutdown callback deadlocked")
        super().shutdown()


class BlockingStartEngine(BridgeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.start_entered = threading.Event()
        self.allow_start = threading.Event()
        self.pause_entered = threading.Event()

    def start(self, target: int, *, activate: bool = False) -> None:
        self.start_entered.set()
        if not self.allow_start.wait(1.0):
            raise RuntimeError("test did not release start")
        super().start(target, activate=activate)

    def pause(self, reason: str) -> None:
        self.pause_entered.set()
        super().pause(reason)


class PublishingPauseEngine(BridgeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.pause_entered = threading.Event()

    def pause(self, reason: str) -> None:
        self.pause_entered.set()
        self.subscriber(
            RuntimeSnapshot(FishingState.PAUSED, 0, 2, 30.0, reason)
        )
        super().pause(reason)


class BindingService:
    def __init__(self) -> None:
        self.bound = type("Bound", (), {"title": "异环"})()
        self.calls = 0

    def bind_foreground(self):
        self.calls += 1
        return self.bound


class SequenceBindingService:
    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)

    def bind_foreground(self):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_controller_counts_down_asynchronously_before_binding() -> None:
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    window_service = BindingService()
    controller = AppController(engine, window_service, scheduler)
    ticks: list[int] = []
    completed: list[tuple[str | None, str | None]] = []

    controller.bind_after_countdown(
        ticks.append,
        lambda title, error: completed.append((title, error)),
    )

    assert ticks == [3]
    assert window_service.calls == 0
    assert scheduler.delays == [1000]
    scheduler.run_next()
    scheduler.run_next()
    assert ticks == [3, 2, 1]
    assert window_service.calls == 0
    scheduler.run_next()

    assert window_service.calls == 1
    assert engine.calls == [("bind", window_service.bound)]
    assert completed == [("异环", None)]


def test_controller_binds_and_starts_after_one_countdown() -> None:
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    window_service = BindingService()
    controller = AppController(engine, window_service, scheduler)
    ticks: list[int] = []
    completed: list[tuple[str | None, str | None]] = []

    controller.bind_and_start_after_countdown(
        2,
        ticks.append,
        lambda title, error: completed.append((title, error)),
    )

    assert ticks == [3]
    for _ in range(3):
        scheduler.run_next(1000)

    assert window_service.calls == 1
    assert engine.calls == [
        ("bind", window_service.bound),
        ("start", 2),
    ]
    assert completed == [("异环", None)]


def test_f8_cancels_pending_bind_and_start_before_engine_calls() -> None:
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    window_service = BindingService()
    controller = AppController(engine, window_service, scheduler)
    controller.subscribe(lambda _snapshot: None)
    completed: list[tuple[str | None, str | None]] = []

    controller.bind_and_start_after_countdown(
        2,
        lambda _seconds: None,
        lambda title, error: completed.append((title, error)),
    )
    controller.pause("F8 紧急暂停")
    scheduler.run_next(10)
    while 1000 in scheduler.pending_delays:
        scheduler.run_next(1000)

    assert completed == [
        (None, "绑定并开始倒计时已被紧急暂停取消")
    ]
    assert window_service.calls == 0
    assert not any(call == ("start", 2) for call in engine.calls)


def test_controller_counts_down_before_starting_engine() -> None:
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    controller = AppController(engine, BindingService(), scheduler)
    ticks: list[int] = []
    completed: list[str | None] = []

    controller.start_after_countdown(4, ticks.append, completed.append)

    assert ticks == [3]
    assert ("start", 4) not in engine.calls
    for _ in range(2):
        scheduler.run_next(1000)
    assert ticks == [3, 2, 1]
    assert ("start", 4) not in engine.calls
    scheduler.run_next(1000)

    assert engine.calls == [("start", 4)]
    assert completed == [None]


def test_controller_counts_down_before_resuming_engine() -> None:
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    controller = AppController(engine, BindingService(), scheduler)
    ticks: list[int] = []
    completed: list[str | None] = []

    controller.resume_after_countdown(ticks.append, completed.append)

    assert ticks == [3]
    assert "resume" not in engine.calls
    for _ in range(2):
        scheduler.run_next(1000)
    assert ticks == [3, 2, 1]
    assert "resume" not in engine.calls
    scheduler.run_next(1000)

    assert engine.calls == ["resume"]
    assert completed == [None]


def test_f8_cancels_pending_resume_countdown_before_engine_call() -> None:
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    controller = AppController(engine, BindingService(), scheduler)
    controller.subscribe(lambda _snapshot: None)
    engine.publish(RuntimeSnapshot(FishingState.PAUSED, 0, 2, 30.0))
    scheduler.run_next(10)
    completed: list[str | None] = []

    controller.resume_after_countdown(
        lambda _seconds: None,
        completed.append,
    )
    controller.pause("F8 紧急暂停")

    scheduler.run_next(10)
    assert completed == ["继续倒计时已被紧急暂停取消"]
    scheduler.run_next(1000)

    assert "resume" not in engine.calls
    assert engine.calls[-1] == ("pause", "F8 紧急暂停")


def test_f8_cancels_pending_start_countdown_before_engine_call() -> None:
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    controller = AppController(engine, BindingService(), scheduler)
    controller.subscribe(lambda _snapshot: None)
    engine.publish(RuntimeSnapshot(FishingState.READY, 0, 2, 30.0))
    scheduler.run_next(10)
    completed: list[str | None] = []

    controller.start_after_countdown(
        2,
        lambda _seconds: None,
        completed.append,
    )
    controller.pause("F8 紧急暂停")

    scheduler.run_next(10)
    assert completed == ["开始倒计时已被紧急暂停取消"]
    scheduler.run_next(1000)

    assert not any(call == ("start", 2) for call in engine.calls)
    assert engine.calls[-1] == ("pause", "F8 紧急暂停")


def test_f8_delivers_countdown_cancellation_on_main_thread() -> None:
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    controller = AppController(engine, BindingService(), scheduler)
    controller.subscribe(lambda _snapshot: None)
    engine.publish(RuntimeSnapshot(FishingState.PAUSED, 0, 2, 30.0))
    scheduler.run_next(10)
    completed: list[tuple[str | None, int]] = []
    main_thread = threading.get_ident()

    controller.resume_after_countdown(
        lambda _seconds: None,
        lambda error: completed.append((error, threading.get_ident())),
    )
    pause_thread = threading.Thread(
        target=lambda: controller.pause("F8 紧急暂停")
    )
    pause_thread.start()
    pause_thread.join(1.0)

    assert not pause_thread.is_alive()
    assert completed == []
    scheduler.run_next(10)

    assert completed == [("继续倒计时已被紧急暂停取消", main_thread)]


def test_shutdown_cancels_pending_start_countdown() -> None:
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    controller = AppController(engine, BindingService(), scheduler)
    completed: list[str | None] = []

    controller.start_after_countdown(2, lambda _seconds: None, completed.append)
    controller.shutdown()
    while scheduler.pending:
        scheduler.run_next()

    assert ("start", 2) not in engine.calls
    assert completed == []
    assert engine.calls[-1] == "shutdown"


def test_controller_rejects_second_start_after_engine_reports_running() -> None:
    scheduler = ManualScheduler()
    engine = RunningStartEngine()
    controller = AppController(engine, BindingService(), scheduler)
    controller.start_after_countdown(2, lambda _seconds: None, lambda _error: None)
    for _ in range(3):
        scheduler.run_next(1000)

    ticks: list[int] = []
    completed: list[str | None] = []
    controller.start_after_countdown(3, ticks.append, completed.append)

    assert ticks == []
    assert completed == ["自动化已在运行"]
    assert engine.calls == [("start", 2)]


def test_rebind_cancels_pending_start_countdown() -> None:
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    binding_service = BindingService()
    controller = AppController(engine, binding_service, scheduler)
    start_done: list[str | None] = []
    bind_ticks: list[int] = []
    bind_done: list[tuple[str | None, str | None]] = []

    controller.start_after_countdown(
        2,
        lambda _seconds: None,
        start_done.append,
    )
    controller.rebind(
        bind_ticks.append,
        lambda title, error: bind_done.append((title, error)),
    )

    assert start_done == ["开始倒计时已取消"]
    assert bind_ticks == [3]
    while scheduler.pending:
        scheduler.run_next(1000)

    assert engine.calls == [("bind", binding_service.bound)]
    assert bind_done == [("异环", None)]


def test_first_binding_failure_stays_unbound_and_keeps_start_disabled(root) -> None:
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    controller = AppController(
        engine,
        SequenceBindingService(RuntimeError("无法读取前台窗口")),
        scheduler,
    )
    window = MainWindow(root, controller, FakeSettings())

    window.on_bind()
    for _ in range(3):
        scheduler.run_next(1000)

    assert window.binding_var.get() == "未绑定"
    assert window.start_button.instate(["disabled"])
    assert window.error_var.get() == "无法读取前台窗口"


def test_failed_rebind_preserves_old_binding_and_can_start(root) -> None:
    old_bound = type("Bound", (), {"title": "旧窗口"})()
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    controller = AppController(
        engine,
        SequenceBindingService(old_bound, RuntimeError("新窗口无效")),
        scheduler,
    )
    window = MainWindow(
        root,
        controller,
        FakeSettings(AppSettings(auto_activate_game=False)),
    )

    window.on_rebind()
    for _ in range(3):
        scheduler.run_next(1000)
    assert window.binding_var.get() == "已绑定：旧窗口"

    window.on_rebind()
    for _ in range(3):
        scheduler.run_next(1000)
    assert window.binding_var.get() == "绑定失败，仍绑定：旧窗口"
    assert not window.start_button.instate(["disabled"])

    window.count_var.set("2")
    window.on_start()
    for _ in range(3):
        scheduler.run_next(1000)
    assert engine.calls[-1] == ("start", 2)
    assert engine.calls.count(("bind", old_bound)) == 1


def test_controller_bridges_engine_commands_and_cancels_countdown_on_shutdown() -> None:
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    controller = AppController(engine, BindingService(), scheduler)
    callback = object()
    completed: list[tuple[str | None, str | None]] = []

    controller.subscribe(callback)
    controller.start(4)
    controller.pause()
    controller.resume()
    controller.bind_after_countdown(
        lambda _seconds: None,
        lambda title, error: completed.append((title, error)),
    )
    controller.shutdown()
    scheduler.run_next()

    assert engine.calls[0][0] == "subscribe"
    assert engine.calls[1:] == [
        ("start", 4), ("pause", "按钮暂停"), "resume", "shutdown"
    ]
    assert completed == []


def test_controller_forwards_explicit_game_activation() -> None:
    engine = BridgeEngine()
    controller = AppController(engine, BindingService(), ManualScheduler())

    controller.start(2, activate=True)
    controller.resume(activate=True)

    assert engine.calls == [("start", 2, True), ("resume", True)]


def test_controller_ignores_all_commands_after_shutdown() -> None:
    engine = BridgeEngine()
    controller = AppController(engine, BindingService(), ManualScheduler())
    controller.shutdown()
    calls_after_shutdown = list(engine.calls)
    ticks: list[int] = []
    completed: list[tuple[str | None, str | None]] = []

    controller.start(2)
    controller.pause("F8 紧急暂停")
    controller.pause()
    controller.resume()
    controller.bind_after_countdown(
        ticks.append,
        lambda title, error: completed.append((title, error)),
    )
    controller.start_after_countdown(
        2,
        ticks.append,
        lambda error: completed.append((None, error)),
    )
    controller.rebind(
        ticks.append,
        lambda title, error: completed.append((title, error)),
    )

    assert engine.calls == calls_after_shutdown
    assert ticks == []
    assert completed == []


def test_shutdown_does_not_enter_engine_while_f8_pause_is_in_flight() -> None:
    engine = BlockingPauseEngine()
    scheduler = ManualScheduler()
    controller = AppController(engine, BindingService(), scheduler)
    controller.subscribe(lambda _snapshot: None)
    engine.publish(RuntimeSnapshot(FishingState.READY, 0, 2, 30.0))
    scheduler.run_next(10)
    pause_thread = threading.Thread(
        target=lambda: controller.pause("F8 紧急暂停")
    )
    shutdown_thread = threading.Thread(target=controller.shutdown)

    pause_thread.start()
    assert engine.pause_entered.wait(1.0)
    shutdown_thread.start()
    try:
        assert not engine.shutdown_entered.wait(0.1)
    finally:
        engine.allow_pause.set()
        pause_thread.join(1.0)
        shutdown_thread.join(1.0)
    assert not pause_thread.is_alive()
    assert not shutdown_thread.is_alive()
    assert engine.calls[-2:] == [("pause", "F8 紧急暂停"), "shutdown"]


def test_f8_returns_during_in_flight_shutdown_without_engine_call() -> None:
    engine = BlockingShutdownEngine()
    scheduler = ManualScheduler()
    controller = AppController(engine, BindingService(), scheduler)
    controller.subscribe(lambda _snapshot: None)
    engine.publish(RuntimeSnapshot(FishingState.READY, 0, 2, 30.0))
    scheduler.run_next(10)
    pause_finished = threading.Event()
    shutdown_thread = threading.Thread(target=controller.shutdown)
    pause_thread = threading.Thread(
        target=lambda: (
            controller.pause("F8 紧急暂停"),
            pause_finished.set(),
        )
    )

    shutdown_thread.start()
    assert engine.shutdown_entered.wait(1.0)
    pause_thread.start()
    try:
        assert pause_finished.wait(0.1)
    finally:
        engine.allow_shutdown.set()
        shutdown_thread.join(1.0)
        pause_thread.join(1.0)

    assert pause_finished.is_set()
    assert engine.calls == [
        ("subscribe", engine.subscriber),
        "shutdown",
    ]


def test_shutdown_does_not_hold_command_lock_during_engine_callbacks() -> None:
    engine = PublishingShutdownEngine()
    controller = AppController(engine, BindingService(), ManualScheduler())
    controller.subscribe(lambda _snapshot: None)

    controller.shutdown()

    assert engine.calls[-1] == "shutdown"


def test_f8_can_cancel_while_start_is_blocked() -> None:
    engine = BlockingStartEngine()
    controller = AppController(engine, BindingService(), ManualScheduler())
    start_thread = threading.Thread(target=lambda: controller.start(2))
    pause_thread = threading.Thread(
        target=lambda: controller.pause("F8 紧急暂停")
    )

    start_thread.start()
    assert engine.start_entered.wait(1.0)
    pause_thread.start()
    try:
        assert engine.pause_entered.wait(0.1)
    finally:
        engine.allow_start.set()
        start_thread.join(1.0)
        pause_thread.join(1.0)

    assert not start_thread.is_alive()
    assert not pause_thread.is_alive()
    assert ("pause", "F8 紧急暂停") in engine.calls


def test_pause_publish_cannot_deadlock_main_thread_shutdown() -> None:
    scheduler = ManualScheduler()
    engine = PublishingPauseEngine()
    controller = AppController(engine, BindingService(), scheduler)
    callback_entered = threading.Event()
    release_callback = threading.Event()
    shutdown_finished = threading.Event()

    def subscriber(snapshot: RuntimeSnapshot) -> None:
        if snapshot.state is FishingState.PAUSED:
            callback_entered.set()
            release_callback.wait(1.0)

    controller.subscribe(subscriber)
    engine.publish(RuntimeSnapshot(FishingState.READY, 0, 2, 30.0))
    if scheduler.pending:
        scheduler.run_next()
    pause_thread = threading.Thread(
        target=lambda: controller.pause("F8 紧急暂停")
    )
    shutdown_thread = threading.Thread(
        target=lambda: (controller.shutdown(), shutdown_finished.set())
    )

    pause_thread.start()
    assert engine.pause_entered.wait(1.0)
    shutdown_thread.start()
    try:
        assert not callback_entered.wait(0.1)
        assert shutdown_finished.wait(0.2)
    finally:
        release_callback.set()
        pause_thread.join(1.0)
        shutdown_thread.join(1.0)

    assert not pause_thread.is_alive()
    assert not shutdown_thread.is_alive()


def test_worker_snapshot_publish_never_calls_tk_scheduler() -> None:
    main_thread = threading.get_ident()
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    controller = AppController(engine, BindingService(), scheduler)
    received: list[RuntimeSnapshot] = []
    controller.subscribe(received.append)
    snapshot = RuntimeSnapshot(FishingState.READY, 0, 2, 30.0)

    worker = threading.Thread(target=lambda: engine.publish(snapshot))
    worker.start()
    worker.join(1.0)

    assert not worker.is_alive()
    assert scheduler.calling_threads == [main_thread]
    assert received == []
    scheduler.run_next()
    assert received == [snapshot]


def test_snapshot_state_guards_f8_before_main_thread_drain() -> None:
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    controller = AppController(engine, BindingService(), scheduler)
    controller.subscribe(lambda _snapshot: None)

    engine.publish(RuntimeSnapshot(FishingState.READY, 0, 2, 30.0))
    controller.pause("F8 紧急暂停")
    assert engine.calls[-1] == ("pause", "F8 紧急暂停")

    engine.publish(RuntimeSnapshot(FishingState.COMPLETE, 2, 2, 30.0))
    calls_before_f8 = list(engine.calls)
    controller.pause("F8 紧急暂停")
    assert engine.calls == calls_before_f8


def test_draining_older_snapshots_cannot_roll_back_f8_guard_state() -> None:
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    controller = AppController(engine, BindingService(), scheduler)

    def pause_on_ready(snapshot: RuntimeSnapshot) -> None:
        if snapshot.state is FishingState.READY:
            controller.pause("F8 紧急暂停")

    controller.subscribe(pause_on_ready)
    engine.publish(RuntimeSnapshot(FishingState.READY, 0, 2, 30.0))
    engine.publish(RuntimeSnapshot(FishingState.COMPLETE, 2, 2, 30.0))
    calls_before_drain = list(engine.calls)

    scheduler.run_next(10)

    assert engine.calls == calls_before_drain


def test_controller_only_forwards_f8_while_automation_can_pause() -> None:
    engine = BridgeEngine()
    scheduler = ManualScheduler()
    controller = AppController(engine, BindingService(), scheduler)
    snapshots: list[RuntimeSnapshot] = []
    controller.subscribe(snapshots.append)

    controller.pause("F8 紧急暂停")
    assert not any(call == ("pause", "F8 紧急暂停") for call in engine.calls)

    ready = RuntimeSnapshot(FishingState.READY, 0, 3, 30.0)
    engine.publish(ready)
    scheduler.run_next(10)
    controller.pause("F8 紧急暂停")

    assert snapshots == [ready]
    assert engine.calls[-1] == ("pause", "F8 紧急暂停")


def test_completed_snapshot_unlocks_ui_only_after_worker_exits(root) -> None:
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    controller = AppController(engine, BindingService(), scheduler)
    window = MainWindow(root, controller, FakeSettings())
    ready = RuntimeSnapshot(FishingState.READY, 0, 2, 30.0)
    completed = RuntimeSnapshot(FishingState.COMPLETE, 2, 2, 30.0)

    window.on_bind()
    for _ in range(3):
        scheduler.run_next(1000)

    engine.running = True
    engine.publish(ready)
    scheduler.run_next(10)
    root.update()
    assert window.start_button.instate(["disabled"])

    engine.publish(completed)
    root.update()
    assert window.start_button.instate(["disabled"])
    assert scheduler.pending
    scheduler.run_next(10)
    assert window.start_button.instate(["disabled"])

    engine.running = False
    scheduler.run_next(10)
    root.update()
    assert not window.start_button.instate(["disabled"])


class AppWindowService:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.own_hwnd = None

    def enable_dpi_awareness(self) -> None:
        self.events.append("dpi")

    def resolve_top_level(self, hwnd: int) -> int:
        self.events.append(("resolve_top_level", hwnd))
        return 900

    def exclude_from_capture(self, hwnd: int) -> bool:
        self.events.append(("exclude", hwnd))
        return True


class AppRoot:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.destroyed = False
        self.on_mainloop = lambda: None
        self.after_callbacks: list[object] = []

    def after(self, _delay: int, callback) -> None:
        self.after_callbacks.append(callback)

    def run_next_after(self) -> None:
        callback = self.after_callbacks.pop(0)
        callback()  # type: ignore[operator]

    def update_idletasks(self) -> None:
        self.events.append("update")

    def winfo_id(self) -> int:
        return 123

    def mainloop(self) -> None:
        self.events.append("mainloop")
        self.on_mainloop()

    def winfo_exists(self) -> bool:
        return not self.destroyed

    def destroy(self) -> None:
        self.destroyed = True
        self.events.append("root.destroy")


class AppHotkey:
    def __init__(self, events: list[object], *, succeeds: bool = False) -> None:
        self.events = events
        self.succeeds = succeeds

    def start(self, callback) -> bool:
        self.events.append("hotkey.start")
        self.callback = callback
        return self.succeeds

    def trigger(self) -> None:
        self.callback()

    def stop(self) -> None:
        self.events.append("hotkey.stop")


class AppSafeInput:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    def release_all(self) -> None:
        self.events.append("input.release_all")

    def close(self) -> None:
        self.events.append("input.close")


class FailingCloseAppSafeInput(AppSafeInput):
    def close(self) -> None:
        self.events.append("input.close")
        raise OSError("关闭屏幕键盘失败")


class AppDiagnostics:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    def cleanup(self) -> None:
        self.events.append("diagnostics.cleanup")


class AppRuntimeLog:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    def start(self) -> Path:
        self.events.append("runtime_log.start")
        return Path("runs")

    def event(self, name: str, **fields: object) -> None:
        self.events.append(("runtime_log.event", name, fields))

    def close(self) -> None:
        self.events.append("runtime_log.close")


class FailingAppRuntimeLog(AppRuntimeLog):
    def start(self) -> Path:
        self.events.append("runtime_log.start")
        raise OSError("磁盘不可写")


class AppReporter:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.requests: list[dict[str, object]] = []

    def subscribe(self, callback) -> None:
        self.callback = callback
        self.events.append("reporter.subscribe")

    def request_report(self, **fields: object):
        self.requests.append(dict(fields))
        self.events.append(("reporter.request", fields["code"]))
        from concurrent.futures import Future

        future = Future()
        future.set_result(None)
        return future

    def close(self, timeout: float = 2.0) -> None:
        self.events.append(("reporter.close", timeout))


class AppMainWindow:
    def __init__(self, root, controller, settings, events) -> None:
        self.events = events
        self.events.append("window")
        controller.subscribe(lambda _snapshot: None)

    def block_start(self, reason: str) -> None:
        self.events.append(("block_start", reason))

    def show_warning(self, reason: str) -> None:
        self.events.append(("warning", reason))


def test_application_wires_f8_and_always_cleans_up_resources() -> None:
    events: list[object] = []
    root = AppRoot(events)
    engine = BridgeEngine(events)
    services = ApplicationServices(
        window_service=AppWindowService(events),
        hotkey=AppHotkey(events),
        safe_input=AppSafeInput(events),
        engine=engine,
        diagnostics=AppDiagnostics(events),
        settings=FakeSettings(),
    )

    Application(
        services=services,
        root_factory=lambda: root,
        main_window_factory=lambda root, controller, settings: AppMainWindow(
            root, controller, settings, events
        ),
    ).run()

    assert services.window_service.own_hwnd == 900
    assert ("pause", "F8 紧急暂停") not in engine.calls
    assert ("block_start", "F8 注册失败，请关闭占用 F8 的程序") in events
    assert events == [
        "dpi",
        "diagnostics.cleanup",
        ("resolve_top_level", 123),
        "window",
        "update",
        ("exclude", 900),
        "hotkey.start",
        ("block_start", "F8 注册失败，请关闭占用 F8 的程序"),
        "mainloop",
        "hotkey.stop",
        "engine.shutdown",
        "input.release_all",
        "input.close",
        "root.destroy",
    ]


def test_application_starts_and_closes_runtime_log() -> None:
    events: list[object] = []
    root = AppRoot(events)
    runtime_log = AppRuntimeLog(events)
    services = ApplicationServices(
        window_service=AppWindowService(events),
        hotkey=AppHotkey(events, succeeds=True),
        safe_input=AppSafeInput(events),
        engine=BridgeEngine(events),
        diagnostics=AppDiagnostics(events),
        settings=FakeSettings(),
        runtime_log=runtime_log,
    )

    Application(
        services=services,
        root_factory=lambda: root,
        main_window_factory=lambda root, controller, settings: AppMainWindow(
            root, controller, settings, events
        ),
    ).run()

    assert "runtime_log.start" in events
    assert any(
        event[0:2] == ("runtime_log.event", "application.started")
        for event in events
        if isinstance(event, tuple)
    )
    assert events.index("runtime_log.start") < events.index("window")
    assert events.index("runtime_log.close") > events.index("input.release_all")
    assert events.index("input.release_all") < events.index("input.close")
    assert events.index("input.close") < events.index("runtime_log.close")


def test_application_reports_mainloop_error_and_closes_reporter_before_recorder() -> None:
    events: list[object] = []
    root = AppRoot(events)
    root.on_mainloop = lambda: (_ for _ in ()).throw(RuntimeError("界面失败"))
    runtime_log = AppRuntimeLog(events)
    reporter = AppReporter(events)
    services = ApplicationServices(
        window_service=AppWindowService(events),
        hotkey=AppHotkey(events, succeeds=True),
        safe_input=AppSafeInput(events),
        engine=BridgeEngine(events),
        diagnostics=AppDiagnostics(events),
        settings=FakeSettings(),
        runtime_log=runtime_log,
        diagnostic_reporter=reporter,
    )

    with pytest.raises(RuntimeError, match="界面失败"):
        Application(
            services=services,
            root_factory=lambda: root,
            main_window_factory=lambda root, controller, settings: AppMainWindow(
                root, controller, settings, events
            ),
        ).run()

    assert reporter.requests[0]["report_type"] == "automatic"
    assert reporter.requests[0]["code"] == "E_APPLICATION"
    assert events.index(("reporter.close", 2.0)) < events.index(
        "runtime_log.close"
    )


def test_application_records_cleanup_failure_before_closing_runtime_log() -> None:
    events: list[object] = []
    root = AppRoot(events)
    runtime_log = AppRuntimeLog(events)
    services = ApplicationServices(
        window_service=AppWindowService(events),
        hotkey=AppHotkey(events, succeeds=True),
        safe_input=FailingCloseAppSafeInput(events),
        engine=BridgeEngine(events),
        diagnostics=AppDiagnostics(events),
        settings=FakeSettings(),
        runtime_log=runtime_log,
    )

    with pytest.raises(BaseExceptionGroup, match="程序关闭清理失败"):
        Application(
            services=services,
            root_factory=lambda: root,
            main_window_factory=lambda root, controller, settings: AppMainWindow(
                root, controller, settings, events
            ),
        ).run()

    failure = (
        "runtime_log.event",
        "application.cleanup_failed",
        {
            "step": "关闭屏幕键盘输入",
            "error_type": "OSError",
            "detail": "关闭屏幕键盘失败",
        },
    )
    assert failure in events
    assert events.index(failure) < events.index("runtime_log.close")


def test_application_blocks_start_when_runtime_log_initialization_fails() -> None:
    events: list[object] = []
    services = ApplicationServices(
        window_service=AppWindowService(events),
        hotkey=AppHotkey(events, succeeds=True),
        safe_input=AppSafeInput(events),
        engine=BridgeEngine(events),
        diagnostics=AppDiagnostics(events),
        settings=FakeSettings(),
        runtime_log=FailingAppRuntimeLog(events),
    )

    Application(
        services=services,
        root_factory=lambda: AppRoot(events),
        main_window_factory=lambda root, controller, settings: AppMainWindow(
            root, controller, settings, events
        ),
    ).run()

    assert ("block_start", "运行日志初始化失败：磁盘不可写") in events


def test_application_reports_capture_exclusion_failure_without_blocking_start() -> None:
    events: list[object] = []
    root = AppRoot(events)
    engine = BridgeEngine(events)
    window_service = AppWindowService(events)

    def fail_exclusion(hwnd: int) -> bool:
        events.append(("exclude", hwnd))
        return False

    window_service.exclude_from_capture = fail_exclusion  # type: ignore[method-assign]
    services = ApplicationServices(
        window_service=window_service,
        hotkey=AppHotkey(events, succeeds=True),
        safe_input=AppSafeInput(events),
        engine=engine,
        diagnostics=AppDiagnostics(events),
        settings=FakeSettings(),
    )

    Application(
        services=services,
        root_factory=lambda: root,
        main_window_factory=lambda root, controller, settings: AppMainWindow(
            root, controller, settings, events
        ),
    ).run()

    assert ("warning", "控制窗口无法从截图中排除，请勿遮挡游戏识别区域") in events
    assert not any(
        isinstance(event, tuple) and event[0] == "block_start"
        for event in events
    )


def test_application_routes_a_registered_f8_press_through_controller() -> None:
    events: list[object] = []
    root = AppRoot(events)
    engine = BridgeEngine(events)
    hotkey = AppHotkey(events, succeeds=True)
    services = ApplicationServices(
        window_service=AppWindowService(events),
        hotkey=hotkey,
        safe_input=AppSafeInput(events),
        engine=engine,
        diagnostics=AppDiagnostics(events),
        settings=FakeSettings(),
    )
    def exercise_f8() -> None:
        engine.publish(RuntimeSnapshot(FishingState.READY, 0, 2, 30.0))
        root.run_next_after()
        hotkey.trigger()

    root.on_mainloop = exercise_f8

    Application(
        services=services,
        root_factory=lambda: root,
        main_window_factory=lambda root, controller, settings: AppMainWindow(
            root, controller, settings, events
        ),
    ).run()

    assert ("pause", "F8 紧急暂停") in engine.calls
    assert not any(
        isinstance(event, tuple) and event[0] == "block_start"
        for event in events
    )


def test_importing_app_does_not_create_tk_or_load_platform_dependencies() -> None:
    source_root = Path(__file__).resolve().parents[2] / "src"
    script = f"""
import sys
import tkinter

sys.path.insert(0, {str(source_root)!r})
tk_calls = []

def forbidden_tk():
    tk_calls.append(True)
    raise AssertionError("Tk must not be created during import")

tkinter.Tk = forbidden_tk
before = set(sys.modules)
import auto_fishing.app
loaded = set(sys.modules) - before
assert not tk_calls
assert "dxcam" not in loaded
assert not any(name.startswith("auto_fishing.capture") for name in loaded)
assert not any(name.startswith("auto_fishing.platform") for name in loaded)
assert "auto_fishing.automation.engine" not in loaded
print("APP_IMPORT_SAFE")
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "APP_IMPORT_SAFE"


def test_application_builds_settings_store_at_specified_config_path(tmp_path) -> None:
    services = Application._build_services(tmp_path)

    assert services.settings.path == tmp_path / "config.json"


def test_default_data_directory_is_on_d_drive() -> None:
    assert DEFAULT_DATA_DIR == Path(r"D:\29551\异环自动钓鱼数据")


def test_application_build_services_shares_storage_quota(tmp_path) -> None:
    services = Application._build_services(tmp_path)

    assert services.runtime_log.quota is services.diagnostics.quota
    assert services.runtime_log.quota is services.settings.quota
    assert services.runtime_log.quota.root == tmp_path.resolve()


def test_application_build_services_shares_runtime_log_with_input_and_engine(tmp_path) -> None:
    services = Application._build_services(tmp_path)

    assert services.safe_input.recorder is services.runtime_log
    assert services.safe_input.backend.recorder is services.runtime_log
    assert services.engine.runtime_log is services.runtime_log


def test_application_builds_on_screen_keyboard_input_backend(tmp_path) -> None:
    services = Application._build_services(tmp_path)

    assert isinstance(services.safe_input.backend, OnScreenKeyboardInputBackend)
    assert services.safe_input.backend.window.recorder is services.runtime_log
    assert services.safe_input.backend.mouse.recorder is services.runtime_log


def test_v2_profile_uses_local_app_data_and_explicit_version(tmp_path) -> None:
    profile = v2_profile({"LOCALAPPDATA": str(tmp_path)})

    assert profile.version == "2.0.0"
    assert profile.window_title == "异环自动钓鱼 V2"
    assert profile.data_dir == tmp_path / "异环自动钓鱼V2"
    assert profile.use_disk_runtime_log is False
    assert profile.use_bundle_diagnostics is True


def test_v2_services_use_memory_recorder_and_never_create_runs(tmp_path) -> None:
    profile = v2_profile({"LOCALAPPDATA": str(tmp_path)})
    services = Application._build_services(profile)

    assert isinstance(services.runtime_log, MemoryDiagnosticRecorder)
    assert services.diagnostic_reporter is not None
    assert services.settings.path == profile.data_dir / "config.json"
    services.runtime_log.start()

    assert not (profile.data_dir / "runs").exists()
    assert not profile.data_dir.exists()


def test_v2_window_shows_version_and_report_controls(root) -> None:
    controller = FakeController()
    window = MainWindow(
        root,
        controller,
        FakeSettings(),
        window_title="异环自动钓鱼 V2",
        diagnostics_enabled=True,
    )

    assert root.title() == "异环自动钓鱼 V2"
    window.report_button.invoke()
    assert controller.calls[-1] == "report_error"
    assert window.open_report_button.instate(["disabled"])


def test_report_result_enables_open_location(root, tmp_path) -> None:
    controller = FakeController()
    window = MainWindow(
        root,
        controller,
        FakeSettings(),
        window_title="异环自动钓鱼 V2",
        diagnostics_enabled=True,
    )
    path = tmp_path / "诊断.zip"
    path.write_bytes(b"zip")

    window.show_diagnostic_result(DiagnosticReportResult(path, None))
    window.open_report_button.invoke()

    assert str(path) in window.diagnostic_path_var.get()
    assert window.open_report_button.instate(["!disabled"])
    assert controller.calls[-1] == ("open_report_location", path)


def test_report_failure_is_visible_and_keeps_open_disabled(root) -> None:
    window = MainWindow(
        root,
        FakeController(),
        FakeSettings(),
        window_title="异环自动钓鱼 V2",
        diagnostics_enabled=True,
    )

    window.show_diagnostic_result(DiagnosticReportResult(None, "写入失败"))

    assert window.error_var.get() == "诊断包生成失败：写入失败"
    assert window.open_report_button.instate(["disabled"])
