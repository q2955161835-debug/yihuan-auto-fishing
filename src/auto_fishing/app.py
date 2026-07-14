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
from auto_fishing.product import ProductProfile, V1_DATA_DIR, v1_profile
from auto_fishing.ui.main_window import MainWindow


DEFAULT_DATA_DIR = V1_DATA_DIR


BindTick = Callable[[int], None]
BindDone = Callable[[str | None, str | None], None]
StartTick = Callable[[int], None]
StartDone = Callable[[str | None], None]
ResumeTick = Callable[[int], None]
ResumeDone = Callable[[str | None], None]
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
        self._countdown_active = False
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
        self._ui_callback_queue: SimpleQueue[Callable[[], None]] = SimpleQueue()
        self._snapshot_poll_started = False
        self._pending_complete: tuple[RuntimeSnapshot, int] | None = None
        self._pending_start_done: StartDone | None = None
        self._pending_resume_done: ResumeDone | None = None
        self._pending_bind_start_done: BindDone | None = None

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
                callback = self._ui_callback_queue.get_nowait()
            except Empty:
                break
            callback()

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
        cancelled = self._cancel_pending_start_countdown()
        if cancelled is not None:
            cancelled("开始倒计时已取消")
        cancelled_resume = self._cancel_pending_resume_countdown()
        if cancelled_resume is not None:
            cancelled_resume("继续倒计时已取消")
        self._start_binding_countdown(on_tick, on_done)

    def bind_and_start_after_countdown(
        self,
        target: int,
        on_tick: BindTick,
        on_done: BindDone,
    ) -> None:
        cancelled = self._cancel_pending_start_countdown()
        if cancelled is not None:
            cancelled("开始倒计时已取消")
        cancelled_resume = self._cancel_pending_resume_countdown()
        if cancelled_resume is not None:
            cancelled_resume("继续倒计时已取消")
        with self._command_condition:
            if self._closed:
                return
            if self._countdown_active:
                on_done(self._last_bound_title, "倒计时正在进行")
                return
            if self._starting or self.engine.is_running:
                on_done(self._last_bound_title, "自动化已在运行")
                return
            self._pending_bind_start_done = on_done
        self._start_binding_countdown(
            on_tick,
            on_done,
            start_target=target,
        )

    def rebind(self, on_tick: BindTick, on_done: BindDone) -> None:
        cancelled = self._cancel_pending_start_countdown()
        if cancelled is not None:
            cancelled("开始倒计时已取消")
        cancelled_resume = self._cancel_pending_resume_countdown()
        if cancelled_resume is not None:
            cancelled_resume("继续倒计时已取消")
        with self._command_condition:
            paused = self._state is FishingState.PAUSED
            if self._starting or (self.engine.is_running and not paused):
                on_done(None, "自动化已在启动或运行，不能重新绑定")
                return
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
        *,
        start_target: int | None = None,
    ) -> None:
        with self._command_condition:
            if self._closed:
                return
            if self._countdown_active:
                on_done(None, "绑定倒计时正在进行")
                return
            self._countdown_active = True
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
                if start_target is not None:
                    self._starting = True

            title: str | None = None
            error_message: str | None = None
            try:
                bound = self.window_service.bind_foreground()
                self.engine.bind(bound)
                title = bound.title
                with self._command_condition:
                    self._last_bound_title = title
                    still_current = generation == self._countdown_generation
                if start_target is not None and still_current:
                    self.engine.start(start_target)
            except Exception as error:
                with self._command_condition:
                    title = self._last_bound_title
                error_message = str(error)
            else:
                title = bound.title
            finally:
                with self._command_condition:
                    if start_target is not None:
                        self._starting = False
                    if generation == self._countdown_generation:
                        self._countdown_active = False
                        if self._pending_bind_start_done is on_done:
                            self._pending_bind_start_done = None
                        deliver = not self._closed
                    else:
                        deliver = False
                self._finish_command()
            if deliver:
                on_done(title, error_message)

        try:
            advance(3)
        except BaseException:
            with self._command_condition:
                self._countdown_active = False
                self._countdown_generation += 1
                if self._pending_bind_start_done is on_done:
                    self._pending_bind_start_done = None
            raise

    def _cancel_pending_start_countdown(self) -> StartDone | None:
        with self._command_condition:
            if self._pending_start_done is None:
                return None
            cancelled = self._pending_start_done
            self._pending_start_done = None
            self._countdown_active = False
            self._countdown_generation += 1
            return cancelled

    def _cancel_pending_resume_countdown(self) -> ResumeDone | None:
        with self._command_condition:
            if self._pending_resume_done is None:
                return None
            cancelled = self._pending_resume_done
            self._pending_resume_done = None
            self._countdown_active = False
            self._countdown_generation += 1
            return cancelled

    def _cancel_pending_countdowns_for_pause(
        self,
    ) -> list[Callable[[], None]]:
        cancelled: list[Callable[[], None]] = []
        with self._command_condition:
            if self._pending_start_done is not None:
                callback = self._pending_start_done
                cancelled.append(
                    lambda done=callback: done("开始倒计时已被紧急暂停取消")
                )
                self._pending_start_done = None
            if self._pending_resume_done is not None:
                callback = self._pending_resume_done
                cancelled.append(
                    lambda done=callback: done("继续倒计时已被紧急暂停取消")
                )
                self._pending_resume_done = None
            if self._pending_bind_start_done is not None:
                callback = self._pending_bind_start_done
                title = self._last_bound_title
                cancelled.append(
                    lambda done=callback, current_title=title: done(
                        current_title,
                        "绑定并开始倒计时已被紧急暂停取消",
                    )
                )
                self._pending_bind_start_done = None
            if cancelled:
                self._countdown_active = False
                self._countdown_generation += 1
        return cancelled

    def _defer_ui_callbacks(
        self,
        callbacks: list[Callable[[], None]],
    ) -> None:
        for callback in callbacks:
            self._ui_callback_queue.put(callback)

    def start_after_countdown(
        self,
        target: int,
        on_tick: StartTick,
        on_done: StartDone,
    ) -> None:
        with self._command_condition:
            if self._closed:
                return
            if self._countdown_active:
                on_done("倒计时正在进行")
                return
            if self._starting or self.engine.is_running:
                on_done("自动化已在运行")
                return
            self._countdown_active = True
            self._countdown_generation += 1
            generation = self._countdown_generation
            self._pending_start_done = on_done

        def advance(seconds: int) -> None:
            with self._command_condition:
                if self._closed or generation != self._countdown_generation:
                    return
                if seconds > 0:
                    on_tick(seconds)
                    self.schedule(1000, lambda: advance(seconds - 1))
                    return
                self._countdown_active = False
                self._pending_start_done = None
                self._starting = True
                self._active_commands += 1

            error_message: str | None = None
            try:
                self.engine.start(target)
            except Exception as error:
                error_message = str(error)
            finally:
                with self._command_condition:
                    self._starting = False
                self._finish_command()
            on_done(error_message)

        try:
            advance(3)
        except BaseException:
            with self._command_condition:
                self._countdown_active = False
                self._countdown_generation += 1
                self._pending_start_done = None
            raise

    def resume_after_countdown(
        self,
        on_tick: ResumeTick,
        on_done: ResumeDone,
    ) -> None:
        with self._command_condition:
            if self._closed:
                return
            if self._countdown_active:
                on_done("倒计时正在进行")
                return
            self._countdown_active = True
            self._countdown_generation += 1
            generation = self._countdown_generation
            self._pending_resume_done = on_done

        def advance(seconds: int) -> None:
            with self._command_condition:
                if self._closed or generation != self._countdown_generation:
                    return
                if seconds > 0:
                    on_tick(seconds)
                    self.schedule(1000, lambda: advance(seconds - 1))
                    return
                self._countdown_active = False
                self._pending_resume_done = None
                self._active_commands += 1

            error_message: str | None = None
            try:
                self.engine.resume()
            except Exception as error:
                error_message = str(error)
            finally:
                self._finish_command()
            on_done(error_message)

        try:
            advance(3)
        except BaseException:
            with self._command_condition:
                self._countdown_active = False
                self._countdown_generation += 1
                self._pending_resume_done = None
            raise

    def start(self, target: int, *, activate: bool = False) -> None:
        with self._command_condition:
            if self._closed:
                return
            self._starting = True
            self._active_commands += 1
        try:
            self.engine.start(target, activate=activate)
        finally:
            with self._command_condition:
                self._starting = False
            self._finish_command()

    def pause(self, reason: str = "按钮暂停") -> None:
        cancelled = self._cancel_pending_countdowns_for_pause()
        self._defer_ui_callbacks(cancelled)
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

    def report_error(self) -> None:
        cancelled = self._cancel_pending_countdowns_for_pause()
        self._defer_ui_callbacks(cancelled)
        if not self._begin_command():
            return
        try:
            self.engine.report_error()
        finally:
            self._finish_command()

    def open_report_location(self, path: Path) -> None:
        if not self._begin_command():
            return
        try:
            self.engine.open_report_location(path)
        finally:
            self._finish_command()

    def resume(self, *, activate: bool = False) -> None:
        if not self._begin_command():
            return
        try:
            self.engine.resume(activate=activate)
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
            self._countdown_active = False
            self._countdown_generation += 1
            self._pending_start_done = None
            self._pending_resume_done = None
            self._pending_bind_start_done = None
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
    runtime_log: Any | None = None
    diagnostic_reporter: Any | None = None


class Application:
    """Create application dependencies and own their top-level lifecycle."""

    def __init__(
        self,
        *,
        services: ApplicationServices | None = None,
        root_factory: Callable[[], Any] = tkinter.Tk,
        main_window_factory: Callable[[Any, Any, Any], Any] = MainWindow,
        data_dir: Path | None = None,
        profile: ProductProfile | None = None,
    ) -> None:
        self._services = services
        self._root_factory = root_factory
        self._main_window_factory = main_window_factory
        self._data_dir = data_dir
        self._profile = profile

    def run(self) -> None:
        services = self._services
        if services is None:
            profile = self._profile or v1_profile()
            if self._data_dir is not None:
                profile = profile.with_data_dir(self._data_dir)
            services = self._build_services(profile)

        root: Any | None = None
        run_error: BaseException | None = None
        runtime_log_error: BaseException | None = None
        runtime_log_started = False
        try:
            services.window_service.enable_dpi_awareness()
            root = self._root_factory()
            services.diagnostics.cleanup()
            if services.runtime_log is not None:
                try:
                    services.runtime_log.start()
                    services.runtime_log.event(
                        "application.started", pid=os.getpid()
                    )
                    runtime_log_started = True
                except BaseException as error:
                    runtime_log_error = error
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
                **(
                    {
                        "window_title": (
                            self._profile.window_title
                            if self._profile is not None
                            else "异环自动钓鱼"
                        ),
                        "diagnostics_enabled": (
                            services.diagnostic_reporter is not None
                        ),
                    }
                    if self._main_window_factory is MainWindow
                    else {}
                ),
            )
            if services.diagnostic_reporter is not None:
                services.diagnostic_reporter.subscribe(
                    lambda result: root.after(
                        0,
                        lambda current=result: (
                            main_window.show_diagnostic_result(current)
                        ),
                    )
                )
            if runtime_log_error is not None:
                main_window.block_start(
                    f"运行日志初始化失败：{runtime_log_error}"
                )
            root.update_idletasks()
            capture_excluded = services.window_service.exclude_from_capture(
                control_hwnd
            )
            if runtime_log_started:
                services.runtime_log.event(
                    "capture.exclusion",
                    hwnd=control_hwnd,
                    success=capture_excluded,
                )
            if not capture_excluded:
                main_window.show_warning(
                    "控制窗口无法从截图中排除，请勿遮挡游戏识别区域"
                )
            hotkey_registered = services.hotkey.start(
                lambda: controller.pause("F8 紧急暂停")
            )
            if runtime_log_started:
                services.runtime_log.event(
                    "hotkey.registration", success=hotkey_registered
                )
            if not hotkey_registered:
                main_window.block_start("F8 注册失败，请关闭占用 F8 的程序")
            root.mainloop()
        except BaseException as error:
            run_error = error
            if services.diagnostic_reporter is not None:
                try:
                    services.diagnostic_reporter.request_report(
                        report_type="automatic",
                        code="E_APPLICATION",
                        detail=str(error),
                        state="应用异常",
                        frame=None,
                        context={"phase": "run"},
                    )
                except BaseException:
                    pass

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
    def _build_services(
        profile_or_data_dir: ProductProfile | Path,
    ) -> ApplicationServices:
        from auto_fishing.automation.engine import AutomationCore, AutomationEngine
        from auto_fishing.automation.state_machine import FishingStateMachine
        from auto_fishing.capture.dxcam_source import DxcamFrameSource
        from auto_fishing.platform.hotkey import GlobalHotkey
        from auto_fishing.platform.input import SafeInput, Win32MouseDriver
        from auto_fishing.platform.on_screen_keyboard import (
            OnScreenKeyboardInputBackend,
            OnScreenKeyboardWindow,
        )
        from auto_fishing.platform.windowing import WindowService
        from auto_fishing.storage.diagnostics import DiagnosticsStore
        from auto_fishing.storage.diagnostic_bundles import (
            DiagnosticBundleService,
            NullDiagnosticsStore,
        )
        from auto_fishing.storage.memory_diagnostics import (
            MemoryDiagnosticRecorder,
        )
        from auto_fishing.storage.quota import StorageQuotaManager
        from auto_fishing.storage.runtime_logging import RuntimeLogStore
        from auto_fishing.storage.settings import SettingsStore
        from auto_fishing.vision.progress import ProgressController
        from auto_fishing.vision.scenes import SceneRecognizer

        profile = (
            v1_profile(profile_or_data_dir)
            if isinstance(profile_or_data_dir, Path)
            else profile_or_data_dir
        )
        data_dir = profile.data_dir
        window_service = WindowService()
        quota: StorageQuotaManager | None
        diagnostic_reporter: DiagnosticBundleService | None
        if profile.use_disk_runtime_log:
            quota = StorageQuotaManager(data_dir)
            quota.initialize()
            runtime_log: Any = RuntimeLogStore(data_dir / "runs", quota=quota)
            diagnostics: Any = DiagnosticsStore(
                data_dir / "diagnostics",
                quota=quota,
            )
            diagnostic_reporter = None
        else:
            quota = None
            runtime_log = MemoryDiagnosticRecorder()
            diagnostics = NullDiagnosticsStore()
            diagnostic_reporter = DiagnosticBundleService(
                data_dir / "diagnostics",
                recorder=runtime_log,
                version=profile.version,
            )
        keyboard_window = OnScreenKeyboardWindow(recorder=runtime_log)
        safe_input = SafeInput(
            OnScreenKeyboardInputBackend(
                window=keyboard_window,
                mouse=Win32MouseDriver(recorder=runtime_log),
                recorder=runtime_log,
            ),
            recorder=runtime_log,
        )
        scene_recognizer = SceneRecognizer()
        core = AutomationCore(
            state_machine=FishingStateMachine(),
            controller=ProgressController(),
            input_service=safe_input,
            scene_recognizer=scene_recognizer,
            event_recorder=runtime_log,
        )
        engine = AutomationEngine(
            core=core,
            window_service=window_service,
            frame_source=DxcamFrameSource(),
            scene_recognizer=scene_recognizer,
            diagnostics=diagnostics,
            runtime_log=runtime_log,
            diagnostic_reporter=diagnostic_reporter,
        )
        return ApplicationServices(
            window_service=window_service,
            hotkey=GlobalHotkey(),
            safe_input=safe_input,
            engine=engine,
            diagnostics=diagnostics,
            settings=SettingsStore(data_dir / "config.json", quota=quota),
            runtime_log=runtime_log,
            diagnostic_reporter=diagnostic_reporter,
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
            ("关闭屏幕键盘输入", services.safe_input.close),
        ):
            try:
                action()
            except BaseException as error:
                error.add_note(label)
                errors.append(error)
                Application._record_cleanup_failure(services, label, error)

        if root is not None:
            try:
                if root.winfo_exists():
                    root.destroy()
            except tkinter.TclError:
                pass
            except BaseException as error:
                error.add_note("销毁 Tk 主窗口")
                errors.append(error)
                Application._record_cleanup_failure(services, "销毁 Tk 主窗口", error)

        if services.diagnostic_reporter is not None:
            try:
                services.diagnostic_reporter.close(timeout=2.0)
            except BaseException as error:
                error.add_note("关闭诊断报告服务")
                errors.append(error)

        if services.runtime_log is not None:
            try:
                services.runtime_log.event(
                    "application.cleanup_finished",
                    error_count=len(errors),
                )
            except BaseException as error:
                error.add_note("记录关闭诊断")
                errors.append(error)
            try:
                services.runtime_log.close()
            except BaseException as error:
                error.add_note("关闭运行日志")
                errors.append(error)
        return errors

    @staticmethod
    def _record_cleanup_failure(
        services: ApplicationServices,
        label: str,
        error: BaseException,
    ) -> None:
        if services.runtime_log is None:
            return
        try:
            services.runtime_log.event(
                "application.cleanup_failed",
                step=label,
                error_type=type(error).__name__,
                detail=str(error),
            )
        except BaseException:
            pass
