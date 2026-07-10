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

    def pause(
        self,
        reason: str,
        now: float,
        *,
        code: str = "E_USER_PAUSE",
        replace_existing: bool = False,
    ) -> None:
        with self._lock:
            if self.state_machine.state is FishingState.COMPLETE:
                self._input_blocked.set()
                try:
                    self.input_service.release_all()
                except Exception as error:
                    self.pause_code = "E_INPUT"
                    self._error = (
                        f"{reason}; release_all failed: {error}"
                    )
                return
            was_paused = self.state_machine.state is FishingState.PAUSED
            preserve_existing = was_paused and not replace_existing
            final_reason = self._error if preserve_existing else reason
            final_code = self.pause_code if preserve_existing else code
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

    def cancel_current(self, now: float) -> None:
        with self._lock:
            self._input_blocked.set()
            try:
                self.input_service.release_all()
            except Exception as error:
                raise InputActionError(str(error)) from error
            self.state_machine.cancel_current(now)
            self.bar_missing_frames = 0
            self.result_candidate_frames = 0
            self.pause_code = ""
            self._error = ""
            self._fps = 0.0

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
            self.result_candidate_frames + 1
            if observation.result_candidate
            else 0
        )
        self._input(self.input_service.release_all)
        if observation.result_candidate:
            if self.result_candidate_frames >= 2:
                self.state_machine.handle(Event.BAR_GONE, now)
            return
        if self.bar_missing_frames >= 6:
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
        self._start_decided = threading.Event()
        self._start_decided.set()
        self._start_allowed = False
        self._starting = False
        self._cancelling = False
        self._closing = False
        self._shutdown_started = False
        self._shutdown_event = threading.Event()
        self._cancel_event = threading.Event()
        self._pause_epoch = 0
        self._resume_request: int | None = None
        self._latest_pause_reason = ""
        self._latest_pause_code = "E_USER_PAUSE"
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
            if self.is_running or self._starting or self._cancelling:
                raise RuntimeError("自动化运行中不能更换绑定窗口")
            self._bound = bound

    def start(self, target: int) -> None:
        with self._lifecycle_lock:
            if self._bound is None:
                raise RuntimeError("请先绑定游戏窗口")
            if self.is_running or self._starting or self._cancelling:
                raise RuntimeError("自动化已在运行")
            if self._shutdown_started and not self._cleanup_done.is_set():
                raise RuntimeError("自动化仍在关闭中")
            bound = self._bound

        try:
            activated = bool(self.window_service.activate(bound))
            foreground = activated and bool(
                self.window_service.is_foreground(bound)
            )
        except Exception as error:
            raise RuntimeError(f"无法激活游戏窗口: {error}") from error
        if not foreground:
            raise RuntimeError("无法激活并确认已绑定的游戏窗口")

        with self._lifecycle_lock:
            if self._bound is not bound:
                raise RuntimeError("绑定窗口已变化，请重新开始")
            if self.is_running or self._starting or self._cancelling:
                raise RuntimeError("自动化已在运行")
            if self._shutdown_started and not self._cleanup_done.is_set():
                raise RuntimeError("自动化仍在关闭中")
            self._shutdown_started = False
            self._cleanup_thread = None
            self._closing = False
            self._shutdown_event.clear()
            self._cancel_event.clear()
            self._starting = True
            self._start_resolved = threading.Event()
            self._thread_start_done = threading.Event()
            self._start_decided = threading.Event()
            self._start_allowed = False
            start_resolved = self._start_resolved
            thread_start_done = self._thread_start_done
            start_decided = self._start_decided
            with self._pause_lock:
                self._pause_epoch += 1
                self._resume_request = None
                self._latest_pause_reason = ""
                self._latest_pause_code = "E_USER_PAUSE"
                start_epoch = self._pause_epoch
            self._diagnostic_recorded = False
            self._last_frame = None
            self._last_refresh = float("-inf")

        publish_done = threading.Event()
        core_started = False
        try:
            self.core.start(target, self.clock())
            core_started = True
        except BaseException as error:
            with self._lifecycle_lock:
                self._starting = False
                start_resolved.set()
                thread_start_done.set()
                with self._pause_lock:
                    self._start_allowed = False
                    start_decided.set()
            publish_done.set()
            if core_started:
                self._pause(
                    "E_AUTOMATION",
                    f"无法准备自动化工作线程: {error}",
                    None,
                    save_diagnostic=False,
                )
            raise

        with self._pause_lock:
            start_cancelled = (
                self._closing or self._pause_epoch != start_epoch
            )
        if start_cancelled:
            self._compensate_cancelled_start()
            with self._lifecycle_lock:
                self._starting = False
                start_resolved.set()
                thread_start_done.set()
                with self._pause_lock:
                    self._start_allowed = False
                    start_decided.set()
            publish_done.set()
            raise RuntimeError("startup cancelled by pause or shutdown")

        try:
            worker = threading.Thread(
                target=self._run_after_publish,
                args=(publish_done, start_decided),
                name="auto-fishing-worker",
                daemon=True,
            )
        except BaseException as error:
            with self._lifecycle_lock:
                self._starting = False
                start_resolved.set()
                thread_start_done.set()
                with self._pause_lock:
                    self._start_allowed = False
                    start_decided.set()
            publish_done.set()
            self._pause(
                "E_AUTOMATION",
                f"无法准备自动化工作线程: {error}",
                None,
                save_diagnostic=False,
            )
            raise

        with self._lifecycle_lock:
            with self._pause_lock:
                cancelled = (
                    self._closing
                    or self._shutdown_started
                    or self._pause_epoch != start_epoch
                )
            if not cancelled:
                self._thread = worker
        if cancelled:
            self._compensate_cancelled_start()
            with self._lifecycle_lock:
                self._starting = False
                start_resolved.set()
                thread_start_done.set()
            with self._pause_lock:
                self._start_allowed = False
                start_decided.set()
            publish_done.set()
            raise RuntimeError("startup cancelled by pause or shutdown")
        start_resolved.set()

        with self._pause_lock:
            cancelled = self._closing or self._pause_epoch != start_epoch
        if cancelled:
            self._compensate_cancelled_start()
            with self._lifecycle_lock:
                if self._thread is worker:
                    self._thread = None
                self._starting = False
                thread_start_done.set()
            with self._pause_lock:
                self._start_allowed = False
                start_decided.set()
            publish_done.set()
            raise RuntimeError("startup cancelled by pause or shutdown")

        try:
            worker.start()
        except BaseException as error:
            with self._pause_lock:
                self._start_allowed = False
                start_decided.set()
            publish_done.set()
            with self._lifecycle_lock:
                if self._thread is worker:
                    self._thread = None
                self._starting = False
                thread_start_done.set()
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
        except BaseException as error:
            with self._pause_lock:
                self._start_allowed = False
                start_decided.set()
            publish_done.set()
            worker.join()
            with self._lifecycle_lock:
                if self._thread is worker:
                    self._thread = None
            self._pause(
                "E_AUTOMATION",
                f"无法发布初始自动化状态: {error}",
                None,
                save_diagnostic=False,
            )
            raise
        else:
            publish_done.set()

        with self._pause_lock:
            start_allowed = (
                not self._closing and self._pause_epoch == start_epoch
            )
            self._start_allowed = start_allowed
            start_decided.set()
        if not start_allowed:
            worker.join()
            with self._lifecycle_lock:
                if self._thread is worker:
                    self._thread = None
            raise RuntimeError("startup cancelled during initial publish")

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
            if self._closing or self._start_allowed is not True:
                return
        snapshot = self.core.snapshot
        if snapshot.state is not FishingState.PAUSED:
            return
        try:
            bound = self._require_bound()
            activated = bool(self.window_service.activate(bound))
            foreground = activated and bool(
                self.window_service.is_foreground(bound)
            )
        except Exception as error:
            detail = str(error)
            foreground = False
        else:
            detail = "无法激活并确认已绑定的游戏窗口"
        if not foreground:
            with self._pause_lock:
                if (
                    not self._closing
                    and requested_epoch == self._pause_epoch
                    and self.core.snapshot.state is FishingState.PAUSED
                ):
                    self._pause(
                        "E_WINDOW",
                        detail,
                        self._last_frame,
                        replace_existing=True,
                    )
            return
        with self._pause_lock:
            if (
                not self._closing
                and requested_epoch == self._pause_epoch
                and snapshot.state is FishingState.PAUSED
            ):
                self._resume_request = requested_epoch

    def cancel_current(self) -> None:
        deadline = time.monotonic() + 2.0
        with self._lifecycle_lock:
            if self._closing or self._cancelling:
                return
            if self._starting:
                raise RuntimeError("自动化正在启动，暂时不能重新绑定")
            self._cancelling = True
            self._cancel_event.set()
            with self._pause_lock:
                self._pause_epoch += 1
                self._resume_request = None
                self._start_allowed = False

        self.core.block_input()
        self._stop_capture()
        self._thread_start_done.wait(
            timeout=max(0.0, deadline - time.monotonic())
        )
        worker = self._thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        if worker is not None and worker.is_alive():
            with self._lifecycle_lock:
                self._cancelling = False
            raise RuntimeError("停止当前自动化轮次超时")

        try:
            with self._lifecycle_lock:
                if self._closing:
                    return
                if self._thread is worker:
                    self._thread = None
                self._bound = None
            self.core.cancel_current(self.clock())
            self._diagnostic_recorded = False
            self._last_frame = None
            self._last_refresh = float("-inf")
            self._publish()
        finally:
            with self._lifecycle_lock:
                self._cancelling = False
                if not self._closing:
                    self._cancel_event.clear()

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
                self._latest_pause_reason = "程序关闭"
                self._latest_pause_code = "E_USER_PAUSE"
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

            while not self._stop_requested():
                (
                    paused_without_request,
                    _,
                    operation_epoch,
                ) = self._read_pause_gate()
                if paused_without_request:
                    self._shutdown_event.wait(0.005)
                    continue
                try:
                    packet = self.frame_source.latest()
                except Exception as error:
                    if self._stop_requested():
                        break
                    pause_applied = self._pause(
                        "E_CAPTURE",
                        str(error),
                        self._last_frame,
                        expected_epoch=operation_epoch,
                    )
                    if pause_applied:
                        break
                    continue
                if self._stop_requested():
                    break
                (
                    paused_without_request,
                    resume_token,
                    frame_epoch,
                ) = self._read_pause_gate()
                if paused_without_request:
                    self._shutdown_event.wait(0.005)
                    continue
                now = self.clock()
                self._last_frame = packet.frame

                if now - packet.timestamp > 0.5:
                    self._pause(
                        "E_STALE_FRAME",
                        "截图帧超过 0.5 秒未更新",
                        packet.frame,
                        expected_epoch=frame_epoch,
                    )
                    self._shutdown_event.wait(0.005)
                    continue
                if now - packet.timestamp > 0.2:
                    try:
                        self.core.release_inputs()
                    except InputActionError as error:
                        self._pause(
                            "E_INPUT",
                            str(error),
                            packet.frame,
                            expected_epoch=frame_epoch,
                        )
                    self._shutdown_event.wait(0.005)
                    continue

                try:
                    bound, capture_restarted = (
                        self._refresh_and_validate_window(bound, now)
                    )
                except CaptureActionError as error:
                    self._pause(
                        "E_CAPTURE",
                        str(error),
                        packet.frame,
                        expected_epoch=frame_epoch,
                    )
                    self._shutdown_event.wait(0.005)
                    continue
                except Exception as error:
                    self._pause(
                        "E_WINDOW",
                        str(error),
                        packet.frame,
                        expected_epoch=frame_epoch,
                    )
                    self._shutdown_event.wait(0.005)
                    continue
                if capture_restarted:
                    continue

                try:
                    client_frame = self._crop_client(packet.frame, bound)
                    observation = self.scene_recognizer.observe(
                        client_frame, packet.timestamp
                    )
                except Exception as error:
                    self._pause(
                        "E_VISION",
                        str(error),
                        packet.frame,
                        expected_epoch=frame_epoch,
                    )
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
                            if self._resume_request == resume_token:
                                self._resume_request = None
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
                    snapshot = self.core.process(
                        observation,
                        client_packet,
                        now,
                        bound.client_rect,
                    )
                except InputActionError as error:
                    self._pause(
                        "E_INPUT",
                        str(error),
                        packet.frame,
                        expected_epoch=frame_epoch,
                    )
                    continue
                except VisionActionError as error:
                    self._pause(
                        "E_VISION",
                        str(error),
                        packet.frame,
                        expected_epoch=frame_epoch,
                    )
                    continue
                except WindowActionError as error:
                    self._pause(
                        "E_WINDOW",
                        str(error),
                        packet.frame,
                        expected_epoch=frame_epoch,
                    )
                    continue

                if snapshot.state is FishingState.COMPLETE:
                    self.core.block_input()
                    try:
                        self.core.release_inputs()
                    except InputActionError as error:
                        self.logger.warning("完成后释放输入失败: %s", error)
                    self._publish()
                    break
                if snapshot.state is FishingState.PAUSED:
                    self._pause(
                        self.core.pause_code or "E_TIMEOUT",
                        snapshot.error,
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

    def _run_after_publish(
        self,
        publish_done: threading.Event,
        start_decided: threading.Event,
    ) -> None:
        worker = threading.current_thread()
        try:
            publish_done.wait()
            start_decided.wait()
            with self._pause_lock:
                if self._start_allowed is not True:
                    return
            self._run()
        finally:
            with self._lifecycle_lock:
                if self._thread is worker:
                    self._thread = None

    def _pause(
        self,
        code: str,
        detail: str,
        frame: np.ndarray | None,
        *,
        save_diagnostic: bool = True,
        replace_existing: bool = False,
        expected_epoch: int | None = None,
    ) -> bool:
        with self._pause_lock:
            if (
                expected_epoch is not None
                and expected_epoch != self._pause_epoch
            ):
                return False
            self._pause_epoch += 1
            self._resume_request = None
            pause_epoch = self._pause_epoch
            self._latest_pause_reason = detail
            self._latest_pause_code = code
            replace_existing = replace_existing or self._starting
        self.core.pause(
            detail,
            self.clock(),
            code=code,
            replace_existing=replace_existing,
        )
        actual_code = self.core.pause_code
        actual_detail = self.core.snapshot.error

        save_diagnostic_now = False
        with self._pause_lock:
            if pause_epoch == self._pause_epoch:
                self._latest_pause_reason = actual_detail
                self._latest_pause_code = actual_code
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
        return True

    def _compensate_cancelled_start(self) -> None:
        while True:
            with self._pause_lock:
                pause_epoch = self._pause_epoch
                reason = self._latest_pause_reason or "启动已被暂停"
                code = self._latest_pause_code
            self.core.pause(
                reason,
                self.clock(),
                code=code,
                replace_existing=True,
            )
            with self._pause_lock:
                if pause_epoch == self._pause_epoch:
                    return

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
            if self._stop_requested():
                return False
            self.frame_source.start(device_index, output_index)
            return True

    def _stop_requested(self) -> bool:
        return self._shutdown_event.is_set() or self._cancel_event.is_set()

    def _read_pause_gate(self) -> tuple[bool, int | None, int]:
        with self._pause_lock:
            resume_token = None if self._closing else self._resume_request
            paused = self.core.snapshot.state is FishingState.PAUSED
            return (
                paused and resume_token is None,
                resume_token,
                self._pause_epoch,
            )

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
