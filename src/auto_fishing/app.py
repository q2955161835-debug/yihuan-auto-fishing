from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import tkinter
from typing import Any

from auto_fishing.automation.engine import AutomationCore, AutomationEngine
from auto_fishing.automation.state_machine import FishingStateMachine
from auto_fishing.capture.dxcam_source import DxcamFrameSource
from auto_fishing.model import FishingState, RuntimeSnapshot
from auto_fishing.platform.hotkey import GlobalHotkey
from auto_fishing.platform.input import SafeInput, Win32InputBackend
from auto_fishing.platform.windowing import WindowService
from auto_fishing.storage.diagnostics import DiagnosticsStore
from auto_fishing.storage.settings import SettingsStore
from auto_fishing.ui.main_window import MainWindow
from auto_fishing.vision.progress import ProgressController
from auto_fishing.vision.scenes import SceneRecognizer


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

    def subscribe(self, callback: Callable[[RuntimeSnapshot], None]) -> None:
        self._callbacks.append(callback)
        if not self._engine_subscribed:
            self._engine_subscribed = True
            self.engine.subscribe(self._publish_snapshot)

    def _publish_snapshot(self, snapshot: RuntimeSnapshot) -> None:
        self._state = snapshot.state
        for callback in tuple(self._callbacks):
            callback(snapshot)

    def bind_after_countdown(
        self,
        on_tick: BindTick,
        on_done: BindDone,
    ) -> None:
        self._start_binding_countdown(on_tick, on_done)

    def rebind(self, on_tick: BindTick, on_done: BindDone) -> None:
        self._start_binding_countdown(on_tick, on_done)

    def _start_binding_countdown(
        self,
        on_tick: BindTick,
        on_done: BindDone,
    ) -> None:
        if self._closed:
            on_done(None, "程序正在关闭")
            return
        if self._binding:
            on_done(None, "绑定倒计时正在进行")
            return
        self._binding = True
        self._countdown_generation += 1
        generation = self._countdown_generation

        def advance(seconds: int) -> None:
            if self._closed or generation != self._countdown_generation:
                return
            if seconds > 0:
                on_tick(seconds)
                self.schedule(1000, lambda: advance(seconds - 1))
                return
            try:
                bound = self.window_service.bind_foreground()
                self.engine.bind(bound)
            except Exception as error:
                on_done(None, str(error))
            else:
                on_done(bound.title, None)
            finally:
                self._binding = False

        try:
            advance(3)
        except BaseException:
            self._binding = False
            self._countdown_generation += 1
            raise

    def start(self, target: int) -> None:
        self._starting = True
        try:
            self.engine.start(target)
        finally:
            self._starting = False

    def pause(self, reason: str = "按钮暂停") -> None:
        if (
            reason.strip().upper().startswith("F8")
            and not self._starting
            and self._state in {FishingState.UNBOUND, FishingState.COMPLETE}
        ):
            return
        self.engine.pause(reason)

    def resume(self) -> None:
        self.engine.resume()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._binding = False
        self._countdown_generation += 1
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
            services.window_service.own_hwnd = root.winfo_id()
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
            services.window_service.exclude_from_capture(root.winfo_id())
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
            settings=SettingsStore(data_dir / "settings.json"),
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
