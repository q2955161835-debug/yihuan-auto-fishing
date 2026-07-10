from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from auto_fishing.automation.state_machine import (
    TIMEOUTS,
    Event,
    FishingStateMachine,
)
from auto_fishing.model import (
    FishingState,
    FramePacket,
    Rect,
    RuntimeSnapshot,
    SceneObservation,
)


class InputActionError(RuntimeError):
    """An input boundary failed while applying a core decision."""


class VisionActionError(RuntimeError):
    """A vision boundary failed while processing the current frame."""


class WindowActionError(RuntimeError):
    """The bound game window cannot safely receive input."""


class CaptureActionError(RuntimeError):
    """The capture source failed while changing outputs."""


class AutomationCore:
    """Thread-independent fishing decisions built on the formal state machine."""

    def __init__(
        self,
        *,
        state_machine: FishingStateMachine,
        controller: Any,
        input_service: Any,
        scene_recognizer: Any,
        activate_game: Callable[[], bool],
    ) -> None:
        self.state_machine = state_machine
        self.controller = controller
        self.input_service = input_service
        self.scene_recognizer = scene_recognizer
        self.activate_game = activate_game
        self.bar_missing_frames = 0
        self.result_candidate_frames = 0
        self.pause_code = ""
        self._error = ""
        self._fps = 0.0
        self._lock = threading.RLock()
        self._input_blocked = threading.Event()
        self._input_blocked.set()

    @property
    def snapshot(self) -> RuntimeSnapshot:
        with self._lock:
            return self.state_machine.snapshot(self._fps, self._error)

    @property
    def input_blocked(self) -> bool:
        with self._lock:
            return self._input_blocked.is_set()

    def start(self, target: int, now: float) -> None:
        with self._lock:
            self.state_machine.start(target, now)
            self.bar_missing_frames = 0
            self.result_candidate_frames = 0
            self.pause_code = ""
            self._error = ""
            self._fps = 0.0
            self._input_blocked.clear()

    def block_input(self) -> None:
        """Prevent subsequent decisions from creating new input actions."""
        with self._lock:
            self._input_blocked.set()

    def release_inputs(self) -> None:
        """Release held input, surfacing failure through the input boundary."""
        with self._lock:
            try:
                self.input_service.release_all()
            except Exception as error:
                raise InputActionError(str(error)) from error

    def pause(self, reason: str, now: float, *, code: str = "E_USER_PAUSE") -> None:
        with self._lock:
            was_paused = self.state_machine.state is FishingState.PAUSED
            final_reason = self._error if was_paused else reason
            final_code = self.pause_code if was_paused else code
            self._input_blocked.set()
            try:
                self.input_service.release_all()
            except Exception as error:
                final_code = "E_INPUT"
                final_reason = (
                    f"{final_reason}; release_all failed: {error}"
                )
            self.state_machine.pause(final_reason, now)
            self.pause_code = final_code
            self._error = final_reason

    def resume(self, observation: SceneObservation, now: float) -> bool:
        with self._lock:
            if self.state_machine.state is not FishingState.PAUSED:
                return False
            event = None
            if observation.progress is not None:
                event = Event.RESUME_CONTROL
            elif observation.result:
                event = Event.RESUME_RESULT
            elif observation.ready:
                event = Event.RESUME_READY
            if event is None:
                return False

            self.state_machine.handle(event, now)
            self.bar_missing_frames = 0
            self.result_candidate_frames = 0
            self.pause_code = ""
            self._error = ""
            self._input_blocked.clear()
            return True

    def process(
        self,
        observation: SceneObservation,
        packet: FramePacket | None,
        now: float,
        client_rect: Rect | None,
    ) -> RuntimeSnapshot:
        with self._lock:
            return self._process_locked(observation, packet, now, client_rect)

    def _process_locked(
        self,
        observation: SceneObservation,
        packet: FramePacket | None,
        now: float,
        client_rect: Rect | None,
    ) -> RuntimeSnapshot:
        if packet is not None:
            self._fps = packet.fps
            age = now - packet.timestamp
            if age > 0.5:
                self.pause("截图帧超过 0.5 秒未更新", now, code="E_STALE_FRAME")
                return self.snapshot
            if age > 0.2:
                try:
                    self.input_service.release_all()
                except Exception as error:
                    self._input_blocked.set()
                    reason = (
                        "截图帧超过 0.2 秒未更新; "
                        f"release_all failed: {error}"
                    )
                    self.state_machine.pause(reason, now)
                    self.pause_code = "E_INPUT"
                    self._error = reason
                return self.snapshot

        timeout = TIMEOUTS.get(self.state_machine.state)
        if timeout is not None and now - self.state_machine.entered_at > timeout:
            reason = f"{self.state_machine.state.value}超时"
            self.pause(reason, now, code="E_TIMEOUT")
            return self.snapshot

        state = self.state_machine.state
        if state is FishingState.READY:
            self._activate()
            self._input(self.input_service.tap_f)
            if packet is not None:
                try:
                    self.scene_recognizer.set_bite_baseline(packet.frame)
                except Exception as error:
                    raise VisionActionError(str(error)) from error
            self.state_machine.handle(Event.CAST_SENT, now)
        elif state is FishingState.WAIT_BITE and observation.bite:
            self._input(self.input_service.tap_f)
            self.state_machine.handle(Event.REEL_SENT, now)
        elif state is FishingState.WAIT_BAR and observation.progress is not None:
            self.state_machine.handle(Event.BAR_DETECTED, now)
        elif state is FishingState.CONTROL:
            self._control(observation, now)
        elif state is FishingState.WAIT_RESULT and observation.result:
            self._input(self.input_service.release_all)
            self.state_machine.handle(Event.RESULT_DETECTED, now)
        elif state is FishingState.DISMISS_RESULT:
            self._dismiss_result(observation, now, client_rect)
        elif (
            state is FishingState.INTER_ROUND
            and self.state_machine.check_interval(now)
        ):
            self.state_machine.handle(Event.INTERVAL_ELAPSED, now)

        return self.snapshot

    def _activate(self) -> None:
        if self._input_blocked.is_set():
            return
        try:
            activated = self.activate_game()
        except Exception as error:
            raise WindowActionError(str(error)) from error
        if not activated:
            raise WindowActionError("无法激活已绑定的游戏窗口")

    def _input(self, action: Callable[[], None]) -> None:
        with self._lock:
            if self._input_blocked.is_set():
                return
            try:
                action()
            except Exception as error:
                raise InputActionError(str(error)) from error

    def _control(self, observation: SceneObservation, now: float) -> None:
        if observation.progress is not None:
            self.bar_missing_frames = 0
            self.result_candidate_frames = 0
            direction = self.controller.decide(observation.progress)
            self._input(lambda: self.input_service.set_direction(direction))
            return

        self.bar_missing_frames += 1
        self.result_candidate_frames = (
            self.result_candidate_frames + 1 if observation.result else 0
        )
        self._input(self.input_service.release_all)
        if self.result_candidate_frames >= 2:
            self.state_machine.handle(Event.BAR_GONE, now)
            return
        if self.bar_missing_frames >= 6 and not observation.result:
            self.pause(
                "连续六帧未识别进度条",
                now,
                code="E_PROGRESS_LOST",
            )

    def _dismiss_result(
        self,
        observation: SceneObservation,
        now: float,
        client_rect: Rect | None,
    ) -> None:
        if not self.state_machine.result_clicked:
            if not observation.result or client_rect is None:
                return
            x = client_rect.left + round(client_rect.width * 0.15)
            y = client_rect.top + round(client_rect.height * 0.55)
            self._input(lambda: self.input_service.click(x, y))
            self.state_machine.handle(Event.RESULT_CLICKED, now)
            return
        if observation.ready:
            self.state_machine.handle(Event.READY_DETECTED, now)
            if self.state_machine.state is FishingState.COMPLETE:
                self._input(self.input_service.release_all)


class AutomationEngine:
    """Own the automation worker and all capture/window lifecycle boundaries."""

    def __init__(
        self,
        *,
        core: AutomationCore,
        window_service: Any,
        frame_source: Any,
        scene_recognizer: Any,
        diagnostics: Any,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        self.core = core
        self.window_service = window_service
        self.frame_source = frame_source
        self.scene_recognizer = scene_recognizer
        self.diagnostics = diagnostics
        self.clock = clock
        self.logger = logger or logging.getLogger(__name__)
        self._bound: Any | None = None
        self._thread: threading.Thread | None = None
        self._cleanup_thread: threading.Thread | None = None
        self._cleanup_done = threading.Event()
        self._cleanup_done.set()
        self._start_resolved = threading.Event()
        self._start_resolved.set()
        self._thread_start_done = threading.Event()
        self._thread_start_done.set()
        self._starting = False
        self._closing = False
        self._shutdown_started = False
        self._shutdown_event = threading.Event()
        self._pause_epoch = 0
        self._resume_request: int | None = None
        self._callbacks: list[Callable[[RuntimeSnapshot], None]] = []
        self._lifecycle_lock = threading.RLock()
        self._capture_lock = threading.Lock()
        self._pause_lock = threading.RLock()
        self._diagnostic_recorded = False
        self._last_frame: np.ndarray | None = None
        self._last_refresh = float("-inf")
        self.core.activate_game = self._activate_bound

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def subscribe(self, callback: Callable[[RuntimeSnapshot], None]) -> None:
        self._callbacks.append(callback)

    def bind(self, bound: Any) -> None:
        with self._lifecycle_lock:
            if self.is_running or self._starting:
                raise RuntimeError("自动化运行中不能更换绑定窗口")
            self._bound = bound

    def start(self, target: int) -> None:
        with self._lifecycle_lock:
            if self._bound is None:
                raise RuntimeError("请先绑定游戏窗口")
            if self.is_running or self._starting:
                raise RuntimeError("自动化已在运行")
            if self._shutdown_started and not self._cleanup_done.is_set():
                raise RuntimeError("自动化仍在关闭中")
            self._shutdown_started = False
            self._cleanup_thread = None
            self._closing = False
            self._shutdown_event.clear()
            self._starting = True
            self._start_resolved = threading.Event()
            self._thread_start_done = threading.Event()
            start_resolved = self._start_resolved
            thread_start_done = self._thread_start_done
            with self._pause_lock:
                self._pause_epoch += 1
                self._resume_request = None
            self._diagnostic_recorded = False
            self._last_frame = None
            self._last_refresh = float("-inf")

        publish_done = threading.Event()
        core_started = False
        try:
            self.core.start(target, self.clock())
            core_started = True
            worker = threading.Thread(
                target=self._run_after_publish,
                args=(publish_done,),
                name="auto-fishing-worker",
                daemon=True,
            )
        except BaseException as error:
            with self._lifecycle_lock:
                self._starting = False
                start_resolved.set()
                thread_start_done.set()
            if core_started:
                self._pause(
                    "E_AUTOMATION",
                    f"无法准备自动化工作线程: {error}",
                    None,
                    save_diagnostic=False,
                )
            raise

        with self._lifecycle_lock:
            cancelled = self._closing or self._shutdown_started
            if not cancelled:
                self._thread = worker
            self._starting = False if cancelled else self._starting
            start_resolved.set()
            if cancelled:
                thread_start_done.set()
        if cancelled:
            raise RuntimeError("自动化启动已被关闭取消")

        try:
            worker.start()
        except BaseException as error:
            with self._lifecycle_lock:
                if self._thread is worker:
                    self._thread = None
                self._starting = False
                thread_start_done.set()
            publish_done.set()
            self._pause(
                "E_AUTOMATION",
                f"无法启动自动化工作线程: {error}",
                None,
                save_diagnostic=False,
            )
            raise
        else:
            with self._lifecycle_lock:
                self._starting = False
                thread_start_done.set()

        try:
            self._publish()
        finally:
            publish_done.set()

    def pause(self, reason: str) -> None:
        frame = (
            None
            if reason.strip().upper().startswith("F8")
            else self._last_frame
        )
        self._pause("E_USER_PAUSE", reason, frame)

    def resume(self) -> None:
        with self._pause_lock:
            requested_epoch = self._pause_epoch
            if self._closing:
                return
        snapshot = self.core.snapshot
        with self._pause_lock:
            if (
                not self._closing
                and requested_epoch == self._pause_epoch
                and snapshot.state is FishingState.PAUSED
            ):
                self._resume_request = requested_epoch

    def shutdown(self) -> None:
        deadline = time.monotonic() + 2.0
        with self._lifecycle_lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
            self._closing = True
            self._shutdown_event.set()
            with self._pause_lock:
                self._pause_epoch += 1
                self._resume_request = None
            self._cleanup_done.clear()
            self._cleanup_thread = threading.Thread(
                target=self._cleanup_shutdown,
                name="auto-fishing-cleanup",
                daemon=True,
            )
            cleanup_thread = self._cleanup_thread
            cleanup_thread.start()

        cleanup_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if cleanup_thread.is_alive():
            self.logger.warning("自动化清理在 2 秒内未退出")

    def _run(self) -> None:
        try:
            bound = self._require_bound()
            try:
                capture_started = self._start_capture(
                    bound.device_index, bound.output_index
                )
            except Exception as error:
                self._pause("E_CAPTURE", str(error), None)
                return
            if not capture_started:
                return

            while not self._shutdown_event.is_set():
                try:
                    packet = self.frame_source.latest()
                except Exception as error:
                    self._pause("E_CAPTURE", str(error), self._last_frame)
                    break
                if self._shutdown_event.is_set():
                    break
                now = self.clock()
                self._last_frame = packet.frame

                if now - packet.timestamp > 0.5:
                    self._pause(
                        "E_STALE_FRAME",
                        "截图帧超过 0.5 秒未更新",
                        packet.frame,
                    )
                    self._shutdown_event.wait(0.005)
                    continue
                if now - packet.timestamp > 0.2:
                    try:
                        self.core.release_inputs()
                    except InputActionError as error:
                        self._pause("E_INPUT", str(error), packet.frame)
                    self._shutdown_event.wait(0.005)
                    continue

                try:
                    bound, capture_restarted = (
                        self._refresh_and_validate_window(bound, now)
                    )
                except CaptureActionError as error:
                    self._pause("E_CAPTURE", str(error), packet.frame)
                    self._shutdown_event.wait(0.005)
                    continue
                except Exception as error:
                    self._pause("E_WINDOW", str(error), packet.frame)
                    self._shutdown_event.wait(0.005)
                    continue
                if capture_restarted:
                    continue

                with self._pause_lock:
                    resume_token = (
                        None if self._closing else self._resume_request
                    )
                if (
                    self.core.snapshot.state is FishingState.PAUSED
                    and resume_token is None
                ):
                    self._shutdown_event.wait(0.005)
                    continue

                try:
                    client_frame = self._crop_client(packet.frame, bound)
                    observation = self.scene_recognizer.observe(
                        client_frame, packet.timestamp
                    )
                except Exception as error:
                    self._pause("E_VISION", str(error), packet.frame)
                    self._shutdown_event.wait(0.005)
                    continue

                if self.core.snapshot.state is FishingState.PAUSED:
                    with self._pause_lock:
                        resume_attempted = (
                            not self._closing
                            and resume_token is not None
                            and resume_token == self._resume_request
                            and resume_token == self._pause_epoch
                            and self.core.snapshot.state
                            is FishingState.PAUSED
                        )
                        if resume_attempted:
                            if self._resume_request == resume_token:
                                self._resume_request = None
                    if not resume_attempted:
                        self._shutdown_event.wait(0.005)
                        continue
                    resumed = self.core.resume(observation, now)
                    with self._pause_lock:
                        resume_invalidated = (
                            self._closing
                            or resume_token != self._pause_epoch
                        )
                        if resumed and not resume_invalidated:
                            self._diagnostic_recorded = False
                    if resumed and resume_invalidated:
                        self.core.pause(
                            "恢复请求已失效",
                            now,
                            code="E_USER_PAUSE",
                        )
                    self._publish()
                    self._shutdown_event.wait(0.001)
                    continue

                client_packet = FramePacket(
                    frame=client_frame,
                    timestamp=packet.timestamp,
                    fps=packet.fps,
                )
                try:
                    self.core.process(
                        observation,
                        client_packet,
                        now,
                        bound.client_rect,
                    )
                except InputActionError as error:
                    self._pause("E_INPUT", str(error), packet.frame)
                    continue
                except VisionActionError as error:
                    self._pause("E_VISION", str(error), packet.frame)
                    continue
                except WindowActionError as error:
                    self._pause("E_WINDOW", str(error), packet.frame)
                    continue

                if self.core.snapshot.state is FishingState.PAUSED:
                    self._pause(
                        self.core.pause_code or "E_TIMEOUT",
                        self.core.snapshot.error,
                        packet.frame,
                    )
                else:
                    self._publish()
                self._shutdown_event.wait(0.001)
        except Exception as error:
            self._pause("E_AUTOMATION", str(error), self._last_frame)
        finally:
            if not self._closing:
                self._stop_capture()

    def _run_after_publish(self, publish_done: threading.Event) -> None:
        publish_done.wait()
        self._run()

    def _pause(
        self,
        code: str,
        detail: str,
        frame: np.ndarray | None,
        *,
        save_diagnostic: bool = True,
    ) -> None:
        with self._pause_lock:
            self._pause_epoch += 1
            self._resume_request = None
        self.core.pause(detail, self.clock(), code=code)
        actual_code = self.core.pause_code
        actual_detail = self.core.snapshot.error

        save_diagnostic_now = False
        with self._pause_lock:
            should_save = save_diagnostic or actual_code == "E_INPUT"
            if (
                should_save
                and not self._diagnostic_recorded
                and frame is not None
            ):
                self._diagnostic_recorded = True
                save_diagnostic_now = True
        if save_diagnostic_now:
            try:
                self.diagnostics.save(frame, actual_code, actual_detail)
            except Exception as error:
                self.logger.warning("保存诊断失败: %s", error)
        self._publish()

    def _cleanup_shutdown(self) -> None:
        try:
            self._start_resolved.wait()
            self._pause(
                "E_USER_PAUSE",
                "程序关闭",
                self._last_frame,
                save_diagnostic=False,
            )
            self._stop_capture()
            self._thread_start_done.wait()
            worker = self._thread
            if worker is not None and worker is not threading.current_thread():
                worker.join()
                with self._lifecycle_lock:
                    if self._thread is worker:
                        self._thread = None
        finally:
            self._cleanup_done.set()

    def _stop_capture(self) -> None:
        try:
            with self._capture_lock:
                self.frame_source.stop()
        except Exception as error:
            self.logger.warning("停止截屏失败: %s", error)

    def _start_capture(self, device_index: int, output_index: int) -> bool:
        with self._capture_lock:
            if self._shutdown_event.is_set():
                return False
            self.frame_source.start(device_index, output_index)
            return True

    def _publish(self) -> None:
        snapshot = self.core.snapshot
        for callback in tuple(self._callbacks):
            try:
                callback(snapshot)
            except Exception as error:
                self.logger.warning("状态回调失败: %s", error)

    def _require_bound(self) -> Any:
        bound = self._bound
        if bound is None:
            raise RuntimeError("游戏窗口未绑定")
        return bound

    def _activate_bound(self) -> bool:
        return bool(self.window_service.activate(self._require_bound()))

    def _refresh_and_validate_window(
        self, bound: Any, now: float
    ) -> tuple[Any, bool]:
        if not self.window_service.is_foreground(bound):
            raise WindowActionError("游戏窗口已失去前台")
        if now - self._last_refresh < 0.5:
            return bound, False

        refreshed = self.window_service.refresh(bound)
        self._last_refresh = now
        if (
            refreshed.device_index != bound.device_index
            or refreshed.output_index != bound.output_index
        ):
            try:
                capture_started = self._start_capture(
                    refreshed.device_index,
                    refreshed.output_index,
                )
            except Exception as error:
                raise CaptureActionError(str(error)) from error
            if not capture_started:
                return refreshed, True
            self._bound = refreshed
            return refreshed, True
        self._bound = refreshed
        return refreshed, False

    @staticmethod
    def _crop_client(frame: np.ndarray, bound: Any) -> np.ndarray:
        client = bound.client_rect
        monitor = bound.monitor_rect
        left = client.left - monitor.left
        top = client.top - monitor.top
        right = client.right - monitor.left
        bottom = client.bottom - monitor.top
        height, width = frame.shape[:2]
        if not (0 <= left < right <= width and 0 <= top < bottom <= height):
            raise WindowActionError("客户区超出当前截屏边界")
        return frame[top:bottom, left:right].copy()
