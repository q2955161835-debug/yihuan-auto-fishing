import tkinter as tk

import pytest

from auto_fishing.app import AppController, Application, ApplicationServices
from auto_fishing.model import FishingState, RuntimeSnapshot
from auto_fishing.storage.settings import AppSettings
from auto_fishing.ui.main_window import MainWindow


class FakeController:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.bind_callbacks = None
        self.rebind_callbacks = None

    def bind_after_countdown(self, on_tick, on_done) -> None:
        self.calls.append("bind")
        self.bind_callbacks = (on_tick, on_done)

    def rebind(self, on_tick, on_done) -> None:
        self.calls.append("rebind")
        self.rebind_callbacks = (on_tick, on_done)

    def start(self, target: int) -> None:
        self.calls.append(("start", target))

    def pause(self, reason: str = "按钮暂停") -> None:
        self.calls.append("pause")

    def resume(self) -> None:
        self.calls.append("resume")

    def shutdown(self) -> None:
        self.calls.append("shutdown")

    def subscribe(self, callback) -> None:
        self.callback = callback


class FakeSettings:
    def __init__(self) -> None:
        self.saved: AppSettings | None = None

    def load(self) -> AppSettings:
        return AppSettings()

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
    window = MainWindow(root, controller, FakeSettings())
    root.update_idletasks()
    assert root.attributes("-topmost") == 1

    window.count_var.set("0")
    window.on_start()
    assert controller.calls == []

    window.count_var.set("3")
    window.on_start()
    assert controller.calls == [("start", 3)]


def test_binding_callbacks_update_visible_status(root) -> None:
    root.withdraw()
    controller = FakeController()
    window = MainWindow(root, controller, FakeSettings())
    window.on_bind()
    assert controller.calls == ["bind"]
    assert window.start_button.instate(["disabled"])
    window.on_start()
    assert controller.calls == ["bind"]
    assert controller.bind_callbacks is not None
    on_tick, on_done = controller.bind_callbacks

    for seconds in (3, 2, 1):
        on_tick(seconds)
        assert window.binding_var.get() == f"绑定倒计时：{seconds}"
    on_done("异环", None)
    assert window.binding_var.get() == "已绑定：异环"

    window.on_rebind()
    assert controller.calls[-1] == "rebind"


def test_snapshot_is_queued_on_tk_thread_and_locks_runtime_controls(root) -> None:
    root.withdraw()
    controller = FakeController()
    window = MainWindow(root, controller, FakeSettings())
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
        assert window.rebind_button.instate(["disabled"])
        assert window.pause_button.cget("text") == "继续"

        window.on_pause_or_resume()
        assert controller.calls[-1] == "resume"
        window.apply_snapshot(
            RuntimeSnapshot(FishingState.READY, 0, 7, 30.0)
        )
        window.on_pause_or_resume()
        assert controller.calls[-1] == "pause"
    finally:
        root.after = original_after  # type: ignore[method-assign]


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


class ManualScheduler:
    def __init__(self) -> None:
        self.delays: list[int] = []
        self.pending: list[object] = []

    def __call__(self, delay: int, callback) -> None:
        self.delays.append(delay)
        self.pending.append(callback)

    def run_next(self) -> None:
        callback = self.pending.pop(0)
        callback()  # type: ignore[operator]


class BridgeEngine:
    def __init__(self, events: list[object] | None = None) -> None:
        self.calls: list[object] = []
        self.events = events

    def subscribe(self, callback) -> None:
        self.calls.append(("subscribe", callback))
        self.subscriber = callback

    def publish(self, snapshot: RuntimeSnapshot) -> None:
        self.subscriber(snapshot)

    def bind(self, bound) -> None:
        self.calls.append(("bind", bound))

    def start(self, target: int) -> None:
        self.calls.append(("start", target))

    def pause(self, reason: str) -> None:
        self.calls.append(("pause", reason))

    def resume(self) -> None:
        self.calls.append("resume")

    def shutdown(self) -> None:
        self.calls.append("shutdown")
        if self.events is not None:
            self.events.append("engine.shutdown")


class BindingService:
    def __init__(self) -> None:
        self.bound = type("Bound", (), {"title": "异环"})()
        self.calls = 0

    def bind_foreground(self):
        self.calls += 1
        return self.bound


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


def test_controller_only_forwards_f8_while_automation_can_pause() -> None:
    engine = BridgeEngine()
    controller = AppController(engine, BindingService(), ManualScheduler())
    snapshots: list[RuntimeSnapshot] = []
    controller.subscribe(snapshots.append)

    controller.pause("F8 紧急暂停")
    assert not any(call == ("pause", "F8 紧急暂停") for call in engine.calls)

    ready = RuntimeSnapshot(FishingState.READY, 0, 3, 30.0)
    engine.publish(ready)
    controller.pause("F8 紧急暂停")

    assert snapshots == [ready]
    assert engine.calls[-1] == ("pause", "F8 紧急暂停")


class AppWindowService:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.own_hwnd = None

    def enable_dpi_awareness(self) -> None:
        self.events.append("dpi")

    def exclude_from_capture(self, hwnd: int) -> bool:
        self.events.append(("exclude", hwnd))
        return True


class AppRoot:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.destroyed = False
        self.on_mainloop = lambda: None

    def after(self, _delay: int, callback) -> None:
        callback()

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


class AppDiagnostics:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    def cleanup(self) -> None:
        self.events.append("diagnostics.cleanup")


class AppMainWindow:
    def __init__(self, root, controller, settings, events) -> None:
        self.events = events
        self.events.append("window")
        controller.subscribe(lambda _snapshot: None)

    def block_start(self, reason: str) -> None:
        self.events.append(("block_start", reason))


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

    assert services.window_service.own_hwnd == 123
    assert ("pause", "F8 紧急暂停") not in engine.calls
    assert ("block_start", "F8 注册失败，请关闭占用 F8 的程序") in events
    assert events == [
        "dpi",
        "diagnostics.cleanup",
        "window",
        "update",
        ("exclude", 123),
        "hotkey.start",
        ("block_start", "F8 注册失败，请关闭占用 F8 的程序"),
        "mainloop",
        "hotkey.stop",
        "engine.shutdown",
        "input.release_all",
        "root.destroy",
    ]


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
    root.on_mainloop = lambda: (
        engine.publish(RuntimeSnapshot(FishingState.READY, 0, 2, 30.0)),
        hotkey.trigger(),
    )

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
