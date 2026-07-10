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
        self._input_blocked = threading.Event()
        self._input_blocked.set()

    @property
    def snapshot(self) -> RuntimeSnapshot:
        return self.state_machine.snapshot(self._fps, self._error)

    @property
    def input_blocked(self) -> bool:
        return self._input_blocked.is_set()

    def start(self, target: int, now: float) -> None:
        self.state_machine.start(target, now)
        self.bar_missing_frames = 0
        self.result_candidate_frames = 0
        self.pause_code = ""
        self._error = ""
        self._fps = 0.0
        self._input_blocked.clear()

    def block_input(self) -> None:
        """Prevent subsequent decisions from creating new input actions."""
        self._input_blocked.set()

    def release_inputs(self) -> None:
        """Best-effort release used by every safety and shutdown path."""
        try:
            self.input_service.release_all()
        except Exception:
            # A failed release must not prevent the state from becoming paused.
            return

    def pause(self, reason: str, now: float, *, code: str = "E_USER_PAUSE") -> None:
        self.block_input()
        self.release_inputs()
        self.state_machine.pause(reason, now)
        self.pause_code = code
        self._error = reason

    def resume(self, observation: SceneObservation, now: float) -> bool:
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
        if packet is not None:
            self._fps = packet.fps
            age = now - packet.timestamp
            if age > 0.2:
                self._input(self.input_service.release_all)
            if age > 0.5:
                self.pause("截图帧超过 0.5 秒未更新", now, code="E_STALE_FRAME")
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
        self._shutdown_event = threading.Event()
        self._resume_requested = threading.Event()
        self._callbacks: list[Callable[[RuntimeSnapshot], None]] = []
        self._lifecycle_lock = threading.RLock()
        self._pause_lock = threading.Lock()
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
            if self.is_running:
                raise RuntimeError("自动化运行中不能更换绑定窗口")
            self._bound = bound

    def start(self, target: int) -> None:
        with self._lifecycle_lock:
            if self._bound is None:
                raise RuntimeError("请先绑定游戏窗口")
            if self.is_running:
                raise RuntimeError("自动化已在运行")
            self._shutdown_event.clear()
            self._resume_requested.clear()
            self._diagnostic_recorded = False
            self._last_frame = None
            self._last_refresh = float("-inf")
            self.core.start(target, self.clock())
            self._publish()
            self._thread = threading.Thread(
                target=self._run,
                name="auto-fishing-worker",
                daemon=True,
            )
            self._thread.start()

    def pause(self, reason: str) -> None:
        frame = (
            None
            if reason.strip().upper().startswith("F8")
            else self._last_frame
        )
        self._pause("E_USER_PAUSE", reason, frame)

    def resume(self) -> None:
        if self.core.snapshot.state is FishingState.PAUSED:
            self._resume_requested.set()

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            self._shutdown_event.set()
            self._resume_requested.clear()
            self.core.block_input()
            try:
                self.frame_source.stop()
            except Exception as error:
                self.logger.warning("停止截屏失败: %s", error)
            self.core.release_inputs()
            thread = self._thread

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
            if thread.is_alive():
                self.logger.warning("自动化工作线程在 2 秒内未退出")
            else:
                with self._lifecycle_lock:
                    if self._thread is thread:
                        self._thread = None

    def _run(self) -> None:
        try:
            bound = self._require_bound()
            try:
                self.frame_source.start(bound.device_index, bound.output_index)
            except Exception as error:
                self._pause("E_CAPTURE", str(error), None)
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

                if now - packet.timestamp > 0.2:
                    self.core.release_inputs()
                if now - packet.timestamp > 0.5:
                    self._pause(
                        "E_STALE_FRAME",
                        "截图帧超过 0.5 秒未更新",
                        packet.frame,
                    )
                    self._shutdown_event.wait(0.005)
                    continue

                try:
                    bound = self._refresh_and_validate_window(bound, now)
                except Exception as error:
                    self._pause("E_WINDOW", str(error), packet.frame)
                    self._shutdown_event.wait(0.005)
                    continue

                if (
                    self.core.snapshot.state is FishingState.PAUSED
                    and not self._resume_requested.is_set()
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
                    self._resume_requested.clear()
                    resumed = self.core.resume(observation, now)
                    if resumed:
                        with self._pause_lock:
                            self._diagnostic_recorded = False
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
            try:
                self.frame_source.stop()
            except Exception as error:
                self.logger.warning("停止截屏失败: %s", error)

    def _pause(
        self,
        code: str,
        detail: str,
        frame: np.ndarray | None,
    ) -> None:
        with self._pause_lock:
            self.core.block_input()
            already_paused = self.core.snapshot.state is FishingState.PAUSED
            if not already_paused:
                self.core.pause(detail, self.clock(), code=code)
            else:
                self.core.release_inputs()

            if not self._diagnostic_recorded and frame is not None:
                self._diagnostic_recorded = True
                try:
                    self.diagnostics.save(frame, code, detail)
                except Exception as error:
                    self.logger.warning("保存诊断失败: %s", error)
        self._publish()

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

    def _refresh_and_validate_window(self, bound: Any, now: float) -> Any:
        if not self.window_service.is_foreground(bound):
            raise WindowActionError("游戏窗口已失去前台")
        if now - self._last_refresh < 0.5:
            return bound

        refreshed = self.window_service.refresh(bound)
        self._last_refresh = now
        if (
            refreshed.device_index != bound.device_index
            or refreshed.output_index != bound.output_index
        ):
            self.frame_source.start(
                refreshed.device_index,
                refreshed.output_index,
            )
        self._bound = refreshed
        return refreshed

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
