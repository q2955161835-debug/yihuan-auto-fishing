from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, SimpleQueue
import threading
import tkinter
from typing import Any

from auto_fishing.model import FishingState, RuntimeSnapshot
from auto_fishing.ui.main_window import MainWindow


BindTick = Callable[[int], None]
BindDone = Callable[[str | None, str | None], None]
Scheduler = Callable[[int, Callable[[], None]], Any]


class AppController:
    """Bridge UI commands to the engine and own the binding countdown."""

    def __init__(
        self,
        engine: Any,
        window_service: Any,
        schedule: Scheduler,
    ) -> None:
        self.engine = engine
        self.window_service = window_service
        self.schedule = schedule
        self._countdown_generation = 0
        self._binding = False
        self._closed = False
        self._starting = False
        self._state = FishingState.UNBOUND
        self._callbacks: list[Callable[[RuntimeSnapshot], None]] = []
        self._engine_subscribed = False
        self._snapshot_generation = 0
        self._last_bound_title: str | None = None
        self._command_condition = threading.Condition(threading.RLock())
        self._active_commands = 0
        self._snapshot_queue: SimpleQueue[RuntimeSnapshot] = SimpleQueue()
        self._snapshot_poll_started = False
        self._pending_complete: tuple[RuntimeSnapshot, int] | None = None

    def subscribe(self, callback: Callable[[RuntimeSnapshot], None]) -> None:
        self._callbacks.append(callback)
        if not self._engine_subscribed:
            self._engine_subscribed = True
            self.engine.subscribe(self._enqueue_snapshot)
        if not self._snapshot_poll_started:
            self._snapshot_poll_started = True
            self.schedule(10, self._drain_snapshots)

    def _enqueue_snapshot(self, snapshot: RuntimeSnapshot) -> None:
        with self._command_condition:
            if self._closed:
                return
            self._state = snapshot.state
            self._snapshot_queue.put(snapshot)

    def _drain_snapshots(self) -> None:
        with self._command_condition:
            if self._closed:
                return

        while True:
            try:
                snapshot = self._snapshot_queue.get_nowait()
            except Empty:
                break
            self._accept_snapshot(snapshot)

        self._deliver_completed_if_stopped()
        with self._command_condition:
            if self._closed:
                return
        self.schedule(10, self._drain_snapshots)

    def _accept_snapshot(self, snapshot: RuntimeSnapshot) -> None:
        with self._command_condition:
            if self._closed:
                return
            self._snapshot_generation += 1
            generation = self._snapshot_generation
            if snapshot.state is FishingState.COMPLETE:
                self._pending_complete = (snapshot, generation)
                return
            self._pending_complete = None
        self._deliver_snapshot(snapshot)

    def _deliver_completed_if_stopped(self) -> None:
        with self._command_condition:
            pending = self._pending_complete
            if self._closed or pending is None or self.engine.is_running:
                return
            snapshot, generation = pending
            if generation != self._snapshot_generation:
                return
            self._pending_complete = None
        self._deliver_snapshot(snapshot)

    def _deliver_snapshot(self, snapshot: RuntimeSnapshot) -> None:
        for callback in tuple(self._callbacks):
            callback(snapshot)

    def bind_after_countdown(
        self,
        on_tick: BindTick,
        on_done: BindDone,
    ) -> None:
        self._start_binding_countdown(on_tick, on_done)

    def rebind(self, on_tick: BindTick, on_done: BindDone) -> None:
        with self._command_condition:
            paused = self._state is FishingState.PAUSED
        if paused:
            if not self._begin_command():
                return
            try:
                self.engine.cancel_current()
            except Exception as error:
                with self._command_condition:
                    title = self._last_bound_title
                on_done(title, str(error))
                return
            finally:
                self._finish_command()
            with self._command_condition:
                self._last_bound_title = None
        self._start_binding_countdown(on_tick, on_done)

    def _start_binding_countdown(
        self,
        on_tick: BindTick,
        on_done: BindDone,
    ) -> None:
        with self._command_condition:
            if self._closed:
                return
            if self._binding:
                on_done(None, "绑定倒计时正在进行")
                return
            self._binding = True
            self._countdown_generation += 1
            generation = self._countdown_generation

        def advance(seconds: int) -> None:
            with self._command_condition:
                if self._closed or generation != self._countdown_generation:
                    return
                if seconds > 0:
                    on_tick(seconds)
                    self.schedule(1000, lambda: advance(seconds - 1))
                    return
                self._active_commands += 1

            title: str | None = None
            error_message: str | None = None
            try:
                bound = self.window_service.bind_foreground()
                self.engine.bind(bound)
            except Exception as error:
                with self._command_condition:
                    title = self._last_bound_title
                error_message = str(error)
            else:
                title = bound.title
                with self._command_condition:
                    self._last_bound_title = title
            finally:
                self._finish_command()
                with self._command_condition:
                    self._binding = False
            on_done(title, error_message)

        try:
            advance(3)
        except BaseException:
            with self._command_condition:
                self._binding = False
                self._countdown_generation += 1
            raise

    def start(self, target: int) -> None:
        with self._command_condition:
            if self._closed:
                return
            self._starting = True
            self._active_commands += 1
        try:
            self.engine.start(target)
        finally:
            with self._command_condition:
                self._starting = False
            self._finish_command()

    def pause(self, reason: str = "按钮暂停") -> None:
        with self._command_condition:
            if self._closed:
                return
            if (
                reason.strip().upper().startswith("F8")
                and not self._starting
                and self._state in {FishingState.UNBOUND, FishingState.COMPLETE}
            ):
                return
            self._active_commands += 1
        try:
            self.engine.pause(reason)
        finally:
            self._finish_command()

    def resume(self) -> None:
        if not self._begin_command():
            return
        try:
            self.engine.resume()
        finally:
            self._finish_command()

    def _begin_command(self) -> bool:
        with self._command_condition:
            if self._closed:
                return False
            self._active_commands += 1
            return True

    def _finish_command(self) -> None:
        with self._command_condition:
            self._active_commands -= 1
            if self._active_commands == 0:
                self._command_condition.notify_all()

    def shutdown(self) -> None:
        with self._command_condition:
            if self._closed:
                return
            self._closed = True
            self._binding = False
            self._countdown_generation += 1
            self._snapshot_generation += 1
            self._pending_complete = None
            while self._active_commands:
                self._command_condition.wait()
        self.engine.shutdown()


@dataclass(frozen=True)
class ApplicationServices:
    window_service: Any
    hotkey: Any
    safe_input: Any
    engine: Any
    diagnostics: Any
    settings: Any


class Application:
    """Create application dependencies and own their top-level lifecycle."""

    def __init__(
        self,
        *,
        services: ApplicationServices | None = None,
        root_factory: Callable[[], Any] = tkinter.Tk,
        main_window_factory: Callable[[Any, Any, Any], Any] = MainWindow,
        data_dir: Path | None = None,
    ) -> None:
        self._services = services
        self._root_factory = root_factory
        self._main_window_factory = main_window_factory
        self._data_dir = data_dir

    def run(self) -> None:
        services = self._services
        if services is None:
            data_dir = self._data_dir or (
                Path(os.environ["LOCALAPPDATA"]) / "异环自动钓鱼"
            )
            services = self._build_services(data_dir)

        root: Any | None = None
        run_error: BaseException | None = None
        try:
            services.window_service.enable_dpi_awareness()
            root = self._root_factory()
            services.diagnostics.cleanup()
            control_hwnd = services.window_service.resolve_top_level(
                root.winfo_id()
            )
            services.window_service.own_hwnd = control_hwnd
            controller = AppController(
                services.engine,
                services.window_service,
                root.after,
            )
            main_window = self._main_window_factory(
                root,
                controller,
                services.settings,
            )
            root.update_idletasks()
            if not services.window_service.exclude_from_capture(control_hwnd):
                main_window.show_warning(
                    "控制窗口无法从截图中排除，请勿遮挡游戏识别区域"
                )
            if not services.hotkey.start(
                lambda: controller.pause("F8 紧急暂停")
            ):
                main_window.block_start("F8 注册失败，请关闭占用 F8 的程序")
            root.mainloop()
        except BaseException as error:
            run_error = error

        cleanup_errors = self._cleanup(services, root)
        if run_error is not None:
            if cleanup_errors:
                raise BaseExceptionGroup(
                    "程序运行和关闭均发生错误",
                    [run_error, *cleanup_errors],
                )
            raise run_error.with_traceback(run_error.__traceback__)
        if cleanup_errors:
            raise BaseExceptionGroup("程序关闭清理失败", cleanup_errors)

    @staticmethod
    def _build_services(data_dir: Path) -> ApplicationServices:
        from auto_fishing.automation.engine import AutomationCore, AutomationEngine
        from auto_fishing.automation.state_machine import FishingStateMachine
        from auto_fishing.capture.dxcam_source import DxcamFrameSource
        from auto_fishing.platform.hotkey import GlobalHotkey
        from auto_fishing.platform.input import SafeInput, Win32InputBackend
        from auto_fishing.platform.windowing import WindowService
        from auto_fishing.storage.diagnostics import DiagnosticsStore
        from auto_fishing.storage.settings import SettingsStore
        from auto_fishing.vision.progress import ProgressController
        from auto_fishing.vision.scenes import SceneRecognizer

        window_service = WindowService()
        safe_input = SafeInput(Win32InputBackend())
        scene_recognizer = SceneRecognizer()
        core = AutomationCore(
            state_machine=FishingStateMachine(),
            controller=ProgressController(),
            input_service=safe_input,
            scene_recognizer=scene_recognizer,
            activate_game=lambda: False,
        )
        diagnostics = DiagnosticsStore(data_dir / "diagnostics")
        engine = AutomationEngine(
            core=core,
            window_service=window_service,
            frame_source=DxcamFrameSource(),
            scene_recognizer=scene_recognizer,
            diagnostics=diagnostics,
        )
        return ApplicationServices(
            window_service=window_service,
            hotkey=GlobalHotkey(),
            safe_input=safe_input,
            engine=engine,
            diagnostics=diagnostics,
            settings=SettingsStore(data_dir / "config.json"),
        )

    @staticmethod
    def _cleanup(
        services: ApplicationServices,
        root: Any | None,
    ) -> list[BaseException]:
        errors: list[BaseException] = []
        for label, action in (
            ("停止 F8 热键", services.hotkey.stop),
            ("关闭自动化引擎", services.engine.shutdown),
            ("释放输入", services.safe_input.release_all),
        ):
            try:
                action()
            except BaseException as error:
                error.add_note(label)
                errors.append(error)

        if root is not None:
            try:
                if root.winfo_exists():
                    root.destroy()
            except tkinter.TclError:
                pass
            except BaseException as error:
                error.add_note("销毁 Tk 主窗口")
                errors.append(error)
        return errors
