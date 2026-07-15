from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from random import uniform as real_uniform
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
from auto_fishing.platform.input import InputTargetUnavailable
from auto_fishing.storage.runtime_logging import RuntimeLogError
from auto_fishing.vision.geometry import crop_normalized
from auto_fishing.vision.regions import TOP_ROI


_RESULT_CLICK_DELAY_MIN = 3.10
_RESULT_CLICK_DELAY_MAX = 3.60
_STRUCTURED_PROGRESS_LOSS_LIMIT = 60
_BLANK_PROGRESS_LOSS_LIMIT = 60
_PROGRESS_ENTRY_DELAY = 0.60
_PROGRESS_ENTRY_CONFIRM_FRAMES = 3
_PROGRESS_ENTRY_MAX_FRAME_GAP = 0.20


def _held_keys(input_service: Any) -> list[str]:
    held = getattr(input_service, "held", ())
    try:
        return sorted(str(key) for key in held)
    except TypeError:
        return []


class InputActionError(RuntimeError):
    """An input boundary failed while applying a core decision."""


class VisionActionError(RuntimeError):
    """A vision boundary failed while processing the current frame."""


class WindowActionError(RuntimeError):
    """The bound game window cannot safely receive input."""


class ForegroundLostError(WindowActionError):
    """The game lost foreground immediately before a new input action."""


class CaptureActionError(RuntimeError):
    """The capture source failed while changing outputs."""


class OnScreenKeyboardActionError(RuntimeError):
    """The Windows on-screen keyboard cannot safely provide input."""


class AutomationCore:
    """Thread-independent fishing decisions built on the formal state machine."""

    def __init__(
        self,
        *,
        state_machine: FishingStateMachine,
        controller: Any,
        input_service: Any,
        scene_recognizer: Any,
        random_uniform: Callable[[float, float], float] = real_uniform,
        event_recorder: Any | None = None,
    ) -> None:
        self.state_machine = state_machine
        self.controller = controller
        self.input_service = input_service
        self.scene_recognizer = scene_recognizer
        self.random_uniform = random_uniform
        self.event_recorder = event_recorder
        self.structured_missing_frames = 0
        self.blank_missing_frames = 0
        self.bar_valid_frames = 0
        self._progress_entry_armed_at: float | None = None
        self._progress_entry_confirm_frames = 0
        self._progress_entry_last_timestamp: float | None = None
        self.result_next_click_at: float | None = None
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
            self._reset_progress_tracking()
            self._reset_progress_entry()
            self.bar_valid_frames = 0
            self._reset_result_dismissal()
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

    def restart_round(self, now: float) -> bool:
        with self._lock:
            if self.state_machine.state is not FishingState.PAUSED:
                return False
            try:
                self.input_service.release_all()
            except Exception as error:
                raise InputActionError(str(error)) from error
            if not self.state_machine.restart_round(now):
                return False
            self._reset_progress_tracking()
            self._reset_progress_entry()
            self.controller.decide(None)
            try:
                self.scene_recognizer.reset_progress_tracking()
            except Exception as error:
                raise VisionActionError(str(error)) from error
            self.bar_valid_frames = 0
            self._reset_result_dismissal()
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
            self._reset_progress_tracking()
            self._reset_progress_entry()
            self.bar_valid_frames = 0
            self._reset_result_dismissal()
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
            self._reset_progress_entry()
            self.scene_recognizer.reset_progress_tracking()
        elif state is FishingState.WAIT_BAR:
            self._await_progress_entry(observation, packet, now)
        elif state is FishingState.CONTROL:
            self._control(observation, now)
        elif state is FishingState.WAIT_RESULT:
            self._click_result_after_delay(now, client_rect)
        elif (
            state is FishingState.INTER_ROUND
            and self.state_machine.check_interval(now)
        ):
            self.state_machine.handle(Event.INTERVAL_ELAPSED, now)

        return self.snapshot

    def _input(self, action: Callable[[], None]) -> None:
        with self._lock:
            if self._input_blocked.is_set():
                return
            try:
                action()
            except InputTargetUnavailable as error:
                raise ForegroundLostError(str(error)) from error
            except Exception as error:
                raise InputActionError(str(error)) from error

    def _control(self, observation: SceneObservation, now: float) -> None:
        if observation.progress is not None:
            self._reset_progress_tracking()
            self.bar_valid_frames += 1
            progress = observation.progress
            green_width = progress.green_right - progress.green_left
            green_center = (progress.green_left + progress.green_right) / 2
            instantaneous_error = (
                None
                if green_width <= 0
                else (progress.yellow_x - green_center) / green_width
            )
            held_keys_before = _held_keys(self.input_service)
            direction = self.controller.decide(progress)
            requested_key = {
                "left": "A",
                "right": "D",
            }.get(direction.value)
            self._record(
                "progress.control",
                direction=direction.value,
                sample_count=self.controller.sample_count,
                weighted_error=self.controller.weighted_error,
                confidence=progress.confidence,
                frame_timestamp=progress.timestamp,
                green_left=progress.green_left,
                green_right=progress.green_right,
                yellow_x=progress.yellow_x,
                instantaneous_error=instantaneous_error,
                requested_key=requested_key,
                held_keys_before=held_keys_before,
            )
            self._input(lambda: self.input_service.set_direction(direction))
            return

        self.controller.decide(None)
        self._input(self.input_service.release_all)
        has_structure = (
            observation.progress_scanlines > 0
            or observation.progress_candidates > 0
        )
        if has_structure:
            self.structured_missing_frames += 1
            self.blank_missing_frames = 0
            if (
                self.structured_missing_frames
                >= _STRUCTURED_PROGRESS_LOSS_LIMIT
            ):
                self.pause(
                    "连续六十帧进度条结构不稳定",
                    now,
                    code="E_PROGRESS_LOST",
                )
            return

        self.blank_missing_frames += 1
        self.structured_missing_frames = 0
        clean_disappearance = (
            observation.progress_scanlines == 0
            and observation.progress_candidates == 0
            and observation.progress_rejection == "yellow_missing"
        )
        if (
            self.bar_valid_frames >= 15
            and self.blank_missing_frames >= 3
            and clean_disappearance
        ):
            self.bar_valid_frames = 0
            self._enter_wait_result(now)
            return
        if self.blank_missing_frames >= _BLANK_PROGRESS_LOSS_LIMIT:
            self.pause(
                "连续六十帧未识别进度条",
                now,
                code="E_PROGRESS_LOST",
            )

    def _enter_wait_result(self, now: float) -> None:
        self.state_machine.handle(Event.BAR_GONE, now)
        self._reset_progress_entry()
        self._reset_result_dismissal()
        self._schedule_result_click(
            now,
            _RESULT_CLICK_DELAY_MIN,
            _RESULT_CLICK_DELAY_MAX,
        )

    def _click_result_after_delay(
        self,
        now: float,
        client_rect: Rect | None,
    ) -> None:
        if self.result_next_click_at is None:
            self._schedule_result_click(
                now,
                _RESULT_CLICK_DELAY_MIN,
                _RESULT_CLICK_DELAY_MAX,
            )
            return
        if now < self.result_next_click_at:
            return
        if client_rect is None:
            return

        point = self._result_click_point(client_rect)
        if point is None:
            self.pause(
                "Windows 屏幕键盘遮挡全部结算安全点击点",
                now,
                code="E_OSK",
            )
            return
        x, y = point
        self._record(
            "result.dismiss_attempt",
            attempt=1,
            x=x,
            y=y,
            trigger="timer_elapsed",
        )
        self._input(lambda: self.input_service.click(x, y))
        self._record(
            "result.dismiss_confirmed",
            attempts=1,
            signal="click_succeeded",
        )
        self.state_machine.handle(Event.RESULT_CLICKED, now)
        self._reset_result_dismissal()

    def _schedule_result_click(
        self,
        now: float,
        minimum: float,
        maximum: float,
    ) -> None:
        delay = float(self.random_uniform(minimum, maximum))
        if not math.isfinite(delay):
            raise ValueError("结算点击随机延迟必须为有限数")
        delay = min(maximum, max(minimum, delay))
        self.result_next_click_at = now + delay
        self._record(
            "result.dismiss_scheduled",
            attempt=1,
            delay=delay,
            scheduled_at=self.result_next_click_at,
        )

    def _result_click_point(self, client_rect: Rect) -> tuple[int, int] | None:
        occlusion = self.input_service.occlusion_rect()
        for horizontal, vertical in (
            (0.80, 0.55),
            (0.85, 0.45),
            (0.70, 0.35),
        ):
            x = client_rect.left + round(client_rect.width * horizontal)
            y = client_rect.top + round(client_rect.height * vertical)
            if occlusion is None or not (
                occlusion.left <= x < occlusion.right
                and occlusion.top <= y < occlusion.bottom
            ):
                return x, y
        return None

    def _reset_result_dismissal(self) -> None:
        self.result_next_click_at = None

    def _reset_progress_tracking(self) -> None:
        self.structured_missing_frames = 0
        self.blank_missing_frames = 0

    def _reset_progress_entry(self) -> None:
        self._progress_entry_armed_at = None
        self._progress_entry_confirm_frames = 0
        self._progress_entry_last_timestamp = None

    def _reset_progress_entry_confirmation(self) -> None:
        self._progress_entry_confirm_frames = 0
        self._progress_entry_last_timestamp = None
        self.controller.decide(None)
        self.scene_recognizer.reset_progress_tracking()

    def _hold_progress_entry(self) -> None:
        self.controller.decide(None)
        self._input(self.input_service.release_all)

    def _await_progress_entry(
        self,
        observation: SceneObservation,
        packet: FramePacket | None,
        now: float,
    ) -> None:
        if self._progress_entry_armed_at is None:
            self._progress_entry_armed_at = now + _PROGRESS_ENTRY_DELAY
            self._reset_progress_entry_confirmation()
            self._record(
                "progress.entry_armed",
                delay_seconds=_PROGRESS_ENTRY_DELAY,
                required_frames=_PROGRESS_ENTRY_CONFIRM_FRAMES,
                armed_at=self._progress_entry_armed_at,
            )

        if now < self._progress_entry_armed_at:
            self._reset_progress_entry_confirmation()
            self._hold_progress_entry()
            self._record(
                "progress.entry_ignored",
                reason="arming",
                remaining=max(0.0, self._progress_entry_armed_at - now),
            )
            return

        progress = observation.progress
        if progress is None:
            self._reset_progress_entry_confirmation()
            self._hold_progress_entry()
            self._record(
                "progress.entry_ignored",
                reason="progress_missing",
            )
            return

        timestamp = packet.timestamp if packet is not None else progress.timestamp
        previous = self._progress_entry_last_timestamp
        if previous is not None and timestamp <= previous:
            self._reset_progress_entry_confirmation()
            self._hold_progress_entry()
            self._record(
                "progress.entry_ignored",
                reason="non_increasing_timestamp",
                frame_timestamp=timestamp,
                previous_timestamp=previous,
            )
            return

        if (
            previous is not None
            and timestamp - previous > _PROGRESS_ENTRY_MAX_FRAME_GAP
        ):
            self._reset_progress_entry_confirmation()

        self._progress_entry_confirm_frames += 1
        self._progress_entry_last_timestamp = timestamp
        self._hold_progress_entry()
        self._record(
            "progress.entry_confirming",
            count=self._progress_entry_confirm_frames,
            required_frames=_PROGRESS_ENTRY_CONFIRM_FRAMES,
            frame_timestamp=timestamp,
        )
        if self._progress_entry_confirm_frames < _PROGRESS_ENTRY_CONFIRM_FRAMES:
            return

        self.state_machine.handle(Event.BAR_DETECTED, now)
        self._record(
            "progress.entry_confirmed",
            count=self._progress_entry_confirm_frames,
            frame_timestamp=timestamp,
        )
        self._reset_progress_entry()

    def _record(self, name: str, **fields: object) -> None:
        if self.event_recorder is not None:
            self.event_recorder.event(name, **fields)


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
        runtime_log: Any | None = None,
        diagnostic_reporter: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        self.core = core
        self.window_service = window_service
        self.frame_source = frame_source
        self.scene_recognizer = scene_recognizer
        self.diagnostics = diagnostics
        self.runtime_log = runtime_log
        self.diagnostic_reporter = diagnostic_reporter
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
        self._last_client_frame: np.ndarray | None = None
        self._progress_frames: deque[np.ndarray] = deque(maxlen=12)
        self._last_refresh = float("-inf")
        self._last_logged_status: tuple[str, int, int, str] | None = None

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
            try:
                self.core.input_service.prepare(
                    bound.monitor_rect,
                    bound.client_rect,
                )
            except Exception as error:
                raise RuntimeError(f"屏幕键盘准备失败: {error}") from error
            self._bound = bound
            set_target_guard = getattr(
                self.core.input_service,
                "set_target_guard",
                None,
            )
            if set_target_guard is not None:
                set_target_guard(self._input_target_is_foreground)
        self._runtime_event(
            "automation.bound",
            title=getattr(bound, "title", ""),
            device_index=getattr(bound, "device_index", None),
            output_index=getattr(bound, "output_index", None),
        )

    def start(self, target: int, *, activate: bool = False) -> None:
        with self._lifecycle_lock:
            if self._bound is None:
                raise RuntimeError("请先绑定游戏窗口")
            if self.is_running or self._starting or self._cancelling:
                raise RuntimeError("自动化已在运行")
            if self._shutdown_started and not self._cleanup_done.is_set():
                raise RuntimeError("自动化仍在关闭中")
            bound = self._bound

        if activate:
            self._activate_bound(bound)
        try:
            foreground = bool(self.window_service.is_foreground(bound))
        except Exception as error:
            raise RuntimeError(f"无法确认游戏窗口前台状态: {error}") from error
        if not foreground:
            raise RuntimeError("请在倒计时结束前切回已绑定的游戏窗口")
        self._runtime_event("automation.start_requested", target=target)

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
            self._last_client_frame = None
            self._progress_frames.clear()
            self._last_refresh = float("-inf")
            self._last_logged_status = None

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

    def report_error(self) -> Any | None:
        pre_report_snapshot = self.core.snapshot
        held_keys_before_report = _held_keys(self.core.input_service)
        state = pre_report_snapshot.state
        if state in {FishingState.UNBOUND, FishingState.COMPLETE}:
            self.core.block_input()
            try:
                self.core.release_inputs()
            except InputActionError as error:
                release_detail = f"；释放输入失败：{error}"
            else:
                release_detail = ""
        else:
            release_detail = ""
            self._pause(
                "E_USER_PAUSE",
                "主动报告错误",
                self._last_frame,
                save_diagnostic=False,
                replace_existing=True,
            )
        if self.diagnostic_reporter is None:
            return None
        frame = (
            None
            if self._last_client_frame is None
            else np.ascontiguousarray(self._last_client_frame).copy()
        )
        return self.diagnostic_reporter.request_report(
            report_type="manual_report",
            code="MANUAL_REPORT",
            detail=f"用户主动报告错误{release_detail}",
            state=self.core.snapshot.state.value,
            frame=frame,
            context=self._diagnostic_context(
                pre_snapshot=pre_report_snapshot,
                held_keys=held_keys_before_report,
            ),
        )

    def open_report_location(self, path: Any) -> None:
        if self.diagnostic_reporter is None:
            raise RuntimeError("当前版本没有诊断报告服务")
        self.diagnostic_reporter.open_location(path)

    def resume(self, *, activate: bool = False) -> None:
        with self._pause_lock:
            requested_epoch = self._pause_epoch
            if self._closing or self._start_allowed is not True:
                return
        snapshot = self.core.snapshot
        if snapshot.state is not FishingState.PAUSED:
            return
        bound = self._require_bound()
        if activate:
            self._activate_bound(bound)
        try:
            foreground = bool(self.window_service.is_foreground(bound))
        except Exception as error:
            detail = f"无法确认游戏窗口前台状态: {error}"
            foreground = False
        else:
            detail = "请在倒计时结束前切回已绑定的游戏窗口"
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
        self._runtime_event("automation.round_restart_requested")

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
            self._last_client_frame = None
            self._progress_frames.clear()
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
                    self._raise_if_runtime_log_failed()
                except RuntimeLogError as error:
                    self._pause(
                        "E_LOGGING",
                        str(error),
                        self._last_frame,
                        expected_epoch=operation_epoch,
                    )
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
                except OnScreenKeyboardActionError as error:
                    self._pause(
                        "E_OSK",
                        str(error),
                        packet.frame,
                        expected_epoch=frame_epoch,
                    )
                    self._shutdown_event.wait(0.005)
                    continue
                except ForegroundLostError as error:
                    self._pause_foreground_interruption(
                        error,
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

                if self.core.snapshot.state is FishingState.PAUSED:
                    with self._pause_lock:
                        restart_attempted = (
                            not self._closing
                            and resume_token is not None
                            and resume_token == self._resume_request
                            and resume_token == self._pause_epoch
                            and self.core.snapshot.state
                            is FishingState.PAUSED
                        )
                    if not restart_attempted:
                        self._shutdown_event.wait(0.005)
                        continue
                    try:
                        restarted = self.core.restart_round(now)
                    except InputActionError as error:
                        self._pause(
                            "E_INPUT",
                            str(error),
                            packet.frame,
                            expected_epoch=frame_epoch,
                        )
                        self._shutdown_event.wait(0.005)
                        continue
                    except VisionActionError as error:
                        self._pause(
                            "E_VISION",
                            str(error),
                            packet.frame,
                            expected_epoch=frame_epoch,
                        )
                        self._shutdown_event.wait(0.005)
                        continue
                    except Exception as error:
                        self._pause(
                            "E_AUTOMATION",
                            str(error),
                            packet.frame,
                            expected_epoch=frame_epoch,
                        )
                        self._shutdown_event.wait(0.005)
                        continue
                    with self._pause_lock:
                        restart_invalidated = (
                            self._closing
                            or resume_token != self._pause_epoch
                        )
                        if restarted and not restart_invalidated:
                            if self._resume_request == resume_token:
                                self._resume_request = None
                            self._diagnostic_recorded = False
                    if restarted and restart_invalidated:
                        self.core.pause(
                            "重开当前轮请求已失效",
                            now,
                            code="E_USER_PAUSE",
                        )
                    if restarted:
                        self._runtime_event("automation.round_restarted")
                    self._publish()
                    self._shutdown_event.wait(0.001)
                    continue

                try:
                    client_frame = self._crop_client(packet.frame, bound)
                except Exception as error:
                    self._pause(
                        "E_VISION",
                        str(error),
                        packet.frame,
                        expected_epoch=frame_epoch,
                    )
                    self._shutdown_event.wait(0.005)
                    continue
                self._last_client_frame = np.ascontiguousarray(client_frame).copy()

                try:
                    occlusion = self._client_occlusion(bound)
                except Exception as error:
                    self._pause(
                        "E_OSK",
                        str(error),
                        packet.frame,
                        expected_epoch=frame_epoch,
                    )
                    self._shutdown_event.wait(0.005)
                    continue

                try:
                    observation = self.scene_recognizer.observe(
                        client_frame,
                        packet.timestamp,
                        occlusion=occlusion,
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

                client_packet = FramePacket(
                    frame=client_frame,
                    timestamp=packet.timestamp,
                    fps=packet.fps,
                )
                state_before = self.core.snapshot.state
                self._remember_progress_frame(client_frame, state_before)
                try:
                    snapshot = self.core.process(
                        observation,
                        client_packet,
                        now,
                        bound.client_rect,
                    )
                    self._record_runtime_frame(
                        client_frame,
                        observation=observation,
                        state_before=state_before,
                        snapshot=snapshot,
                        frame_timestamp=packet.timestamp,
                        now_monotonic=now,
                    )
                    self._raise_if_runtime_log_failed()
                except RuntimeLogError as error:
                    self._pause(
                        "E_LOGGING",
                        str(error),
                        packet.frame,
                        expected_epoch=frame_epoch,
                    )
                    continue
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
                except ForegroundLostError as error:
                    self._pause_foreground_interruption(
                        error,
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
                        expected_epoch=frame_epoch,
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
        pre_pause_snapshot = self.core.snapshot
        held_keys_before_pause = _held_keys(self.core.input_service)
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
        self._runtime_event(
            "automation.paused",
            code=actual_code,
            detail=actual_detail,
        )

        save_diagnostic_now = False
        request_bundle_now = False
        with self._pause_lock:
            if pause_epoch == self._pause_epoch:
                self._latest_pause_reason = actual_detail
                self._latest_pause_code = actual_code
            if self.diagnostic_reporter is not None:
                if actual_code != "E_USER_PAUSE" and not self._diagnostic_recorded:
                    self._diagnostic_recorded = True
                    request_bundle_now = True
            else:
                should_save = save_diagnostic or actual_code == "E_INPUT"
                if (
                    should_save
                    and not self._diagnostic_recorded
                    and frame is not None
                ):
                    self._diagnostic_recorded = True
                    save_diagnostic_now = True
        if request_bundle_now:
            diagnostic_frame = (
                None
                if self._last_client_frame is None
                else np.ascontiguousarray(self._last_client_frame).copy()
            )
            try:
                self.diagnostic_reporter.request_report(
                    report_type="automatic",
                    code=actual_code,
                    detail=actual_detail,
                    state=self.core.snapshot.state.value,
                    frame=diagnostic_frame,
                    context=self._diagnostic_context(
                        pre_snapshot=pre_pause_snapshot,
                        held_keys=held_keys_before_pause,
                    ),
                )
            except Exception as error:
                self.logger.warning("生成诊断包失败: %s", error)
        elif save_diagnostic_now:
            try:
                self.diagnostics.save(
                    frame,
                    actual_code,
                    actual_detail,
                    progress_frames=(
                        tuple(self._progress_frames)
                        if actual_code == "E_PROGRESS_LOST"
                        else ()
                    ),
                )
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
            self._runtime_event("capture.stopped")
        except Exception as error:
            self.logger.warning("停止截屏失败: %s", error)

    def _start_capture(self, device_index: int, output_index: int) -> bool:
        with self._capture_lock:
            if self._stop_requested():
                return False
            self.frame_source.start(device_index, output_index)
            self._runtime_event(
                "capture.started",
                device_index=device_index,
                output_index=output_index,
            )
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
        status = (
            snapshot.state.value,
            snapshot.completed,
            snapshot.target,
            snapshot.error,
        )
        if status != self._last_logged_status:
            self._last_logged_status = status
            self._runtime_event(
                "automation.status",
                state=snapshot.state.value,
                completed=snapshot.completed,
                target=snapshot.target,
                fps=snapshot.fps,
                error=snapshot.error,
                pause_code=self.core.pause_code,
            )
        for callback in tuple(self._callbacks):
            try:
                callback(snapshot)
            except Exception as error:
                self.logger.warning("状态回调失败: %s", error)

    def _record_runtime_frame(
        self,
        frame: np.ndarray,
        *,
        observation: SceneObservation,
        state_before: FishingState,
        snapshot: RuntimeSnapshot,
        frame_timestamp: float,
        now_monotonic: float,
    ) -> None:
        if self.runtime_log is not None:
            self.runtime_log.record_frame(
                frame,
                observation=observation,
                state_before=state_before,
                snapshot=snapshot,
                frame_timestamp=frame_timestamp,
                now_monotonic=now_monotonic,
            )

    def _remember_progress_frame(
        self,
        client_frame: np.ndarray,
        state: FishingState,
    ) -> None:
        if state not in {FishingState.WAIT_BAR, FishingState.CONTROL}:
            self._progress_frames.clear()
            return
        top = crop_normalized(client_frame, TOP_ROI)
        height = top.shape[0]
        band = top[round(height * 0.40) : round(height * 0.52)]
        self._progress_frames.append(np.ascontiguousarray(band).copy())

    def _raise_if_runtime_log_failed(self) -> None:
        if self.runtime_log is not None:
            self.runtime_log.raise_if_failed()

    def _runtime_event(self, name: str, **fields: Any) -> None:
        if self.runtime_log is None:
            return
        try:
            self.runtime_log.event(name, **fields)
        except Exception as error:
            self.logger.warning("保存运行日志事件失败: %s", error)

    def _diagnostic_context(
        self,
        *,
        pre_snapshot: RuntimeSnapshot | None = None,
        held_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        context: dict[str, Any] = {
            "dpi_awareness": getattr(
                self.window_service,
                "dpi_awareness",
                "unknown",
            )
        }
        if pre_snapshot is not None:
            context.update(
                {
                    "pre_report_state": pre_snapshot.state.value,
                    "pre_report_completed": pre_snapshot.completed,
                    "pre_report_target": pre_snapshot.target,
                }
            )
        if held_keys is not None:
            context["held_keys_before_report"] = list(held_keys)
        bound = self._bound
        if bound is not None:
            context["monitor_rect"] = self._rect_values(bound.monitor_rect)
            context["client_rect"] = self._rect_values(bound.client_rect)
            context["game_hwnd"] = getattr(bound, "hwnd", None)
        try:
            osk_rect = self.core.input_service.occlusion_rect()
        except Exception as error:
            context["osk_rect_error"] = str(error)
        else:
            if osk_rect is not None:
                context["osk_rect"] = self._rect_values(osk_rect)
        return context

    @staticmethod
    def _rect_values(rect: Any) -> list[int]:
        return [rect.left, rect.top, rect.right, rect.bottom]

    def _activate_bound(self, bound: Any) -> None:
        self._runtime_event(
            "window.activation_requested",
            hwnd=getattr(bound, "hwnd", None),
        )
        try:
            activated = bool(self.window_service.activate(bound))
            foreground = bool(self.window_service.is_foreground(bound))
        except Exception as error:
            self._runtime_event(
                "window.activation_result",
                success=False,
                detail=str(error),
            )
            raise RuntimeError(
                "自动切换到游戏失败，请关闭自动切回或手动切到游戏后重试"
            ) from error

        success = activated and foreground
        self._runtime_event("window.activation_result", success=success)
        if not success:
            raise RuntimeError(
                "自动切换到游戏失败，请关闭自动切回或手动切到游戏后重试"
            )

    def _require_bound(self) -> Any:
        bound = self._bound
        if bound is None:
            raise RuntimeError("游戏窗口未绑定")
        return bound

    def _refresh_and_validate_window(
        self, bound: Any, now: float
    ) -> tuple[Any, bool]:
        if not self.window_service.is_foreground(bound):
            raise ForegroundLostError("游戏窗口已失去前台")
        if now - self._last_refresh < 0.5:
            return bound, False

        refreshed = self.window_service.refresh(bound)
        self._last_refresh = now
        if (
            refreshed.monitor_rect != bound.monitor_rect
            or refreshed.client_rect != bound.client_rect
        ):
            try:
                self.core.input_service.prepare(
                    refreshed.monitor_rect,
                    refreshed.client_rect,
                )
            except Exception as error:
                raise OnScreenKeyboardActionError(
                    f"屏幕键盘重新定位失败: {error}"
                ) from error
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

    def _input_target_is_foreground(self) -> bool:
        bound = self._bound
        return bound is not None and bool(
            self.window_service.is_foreground(bound)
        )

    def _pause_foreground_interruption(
        self,
        error: BaseException,
        frame: np.ndarray | None,
        *,
        expected_epoch: int | None = None,
    ) -> bool:
        try:
            control_foreground = bool(
                self.window_service.is_control_foreground()
            )
        except Exception:
            control_foreground = False
        if control_foreground:
            return self._pause(
                "E_USER_PAUSE",
                "控制窗口已取得前台，自动化已安全暂停",
                frame,
                save_diagnostic=False,
                expected_epoch=expected_epoch,
            )
        detail = (
            "检测到 Windows 系统弹窗或其他窗口抢占前台，"
            "自动化已安全暂停；关闭弹窗后点击继续"
        )
        if "无法确认" in str(error):
            detail = f"{detail}；{error}"
        return self._pause(
            "E_WINDOW",
            detail,
            frame,
            expected_epoch=expected_epoch,
        )

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

    def _client_occlusion(self, bound: Any) -> Rect | None:
        screen_rect = self.core.input_service.occlusion_rect()
        if screen_rect is None:
            return None
        client = bound.client_rect
        left = max(screen_rect.left, client.left)
        top = max(screen_rect.top, client.top)
        right = min(screen_rect.right, client.right)
        bottom = min(screen_rect.bottom, client.bottom)
        if left >= right or top >= bottom:
            return None
        return Rect(
            left - client.left,
            top - client.top,
            right - client.left,
            bottom - client.top,
        )
