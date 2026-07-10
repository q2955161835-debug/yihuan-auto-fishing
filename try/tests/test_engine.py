from __future__ import annotations

import threading
import time
import json
from collections.abc import Callable

import numpy as np
import pytest

from auto_fishing.automation.engine import (
    AutomationCore,
    AutomationEngine,
    InputActionError,
)
from auto_fishing.automation.state_machine import Event, FishingStateMachine
from auto_fishing.model import (
    Direction,
    FramePacket,
    FishingState,
    ProgressObservation,
    Rect,
    SceneObservation,
)
from auto_fishing.platform.input import InputFailure
from auto_fishing.platform.windowing import BoundWindow
from auto_fishing.storage.diagnostics import DiagnosticsStore
from auto_fishing.vision.progress import ProgressController
from auto_fishing.vision.scenes import SceneRecognizer


CLIENT = Rect(0, 0, 1280, 720)
MONITOR = Rect(0, 0, 1920, 1080)
BOUND = BoundWindow(100, "异环", CLIENT, MONITOR, 0, 0)


class RecordingInput:
    def __init__(self) -> None:
        self.events: list[object] = []
        self.failure: Exception | None = None

    def _record(self, event: object) -> None:
        if self.failure is not None:
            error, self.failure = self.failure, None
            raise error
        self.events.append(event)

    def tap_f(self) -> None:
        self._record("F")

    def set_direction(self, direction: Direction) -> None:
        self._record(direction.value)

    def click(self, x: int, y: int) -> None:
        self._record(("click", x, y))

    def release_all(self) -> None:
        self.events.append("release")


class ReleaseFailingInput(RecordingInput):
    def __init__(self, *, fail_release: bool = True) -> None:
        super().__init__()
        self.fail_release = fail_release

    def release_all(self) -> None:
        if self.fail_release:
            self.fail_release = False
            raise InputFailure("key release failed")
        super().release_all()


class BarrierInput(RecordingInput):
    def __init__(self) -> None:
        super().__init__()
        self.tap_entered = threading.Event()
        self.allow_tap = threading.Event()

    def tap_f(self) -> None:
        self.tap_entered.set()
        assert self.allow_tap.wait(timeout=1)
        super().tap_f()


class ShutdownBlockingInput(RecordingInput):
    def __init__(self, *, block_tap: bool = False) -> None:
        super().__init__()
        self.block_tap = block_tap
        self.tap_entered = threading.Event()
        self.tap_returned = threading.Event()
        self.release_entered = threading.Event()
        self.release_returned = threading.Event()
        self.allow_tap = threading.Event()
        self.allow_release = threading.Event()

    def tap_f(self) -> None:
        if self.block_tap:
            self.tap_entered.set()
            self.allow_tap.wait(timeout=2.2)
            self.tap_returned.set()
        super().tap_f()

    def release_all(self) -> None:
        self.release_entered.set()
        self.allow_release.wait(timeout=2.2)
        self.release_returned.set()
        super().release_all()


class RecordingWindowService:
    def __init__(self) -> None:
        self.foreground = True
        self.activate_calls = 0
        self.refresh_calls = 0
        self.refreshed = BOUND
        self.refresh_error: Exception | None = None

    def activate(self, _bound: BoundWindow) -> bool:
        self.activate_calls += 1
        return self.foreground

    def is_foreground(self, _bound: BoundWindow) -> bool:
        return self.foreground

    def refresh(self, _bound: BoundWindow) -> BoundWindow:
        self.refresh_calls += 1
        if self.refresh_error is not None:
            raise self.refresh_error
        return self.refreshed


class ForegroundDropsAfterStartWindowService(RecordingWindowService):
    """Allow start's foreground confirmation, then model a game focus loss."""

    def __init__(self) -> None:
        super().__init__()
        self.foreground_checks = 0

    def is_foreground(self, bound: BoundWindow) -> bool:
        self.foreground_checks += 1
        return self.foreground_checks == 1 and super().is_foreground(bound)


class ActivatingWindowService(RecordingWindowService):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events
        self.foreground = False
        self.activate_succeeds = True

    def activate(self, _bound: BoundWindow) -> bool:
        self.activate_calls += 1
        self.events.append("activate")
        if self.activate_succeeds:
            self.foreground = True
        return self.activate_succeeds

    def is_foreground(self, _bound: BoundWindow) -> bool:
        self.events.append("foreground")
        return self.foreground


class BlockingWindowError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("迟到的窗口错误")
        self.stringify_entered = threading.Event()
        self.allow_stringify = threading.Event()

    def __str__(self) -> str:
        self.stringify_entered.set()
        assert self.allow_stringify.wait(timeout=1)
        return super().__str__()


class LateErrorWindowService(RecordingWindowService):
    def __init__(self, error: BlockingWindowError) -> None:
        super().__init__()
        self.error = error
        self.raise_worker_error = False

    def is_foreground(self, bound: BoundWindow) -> bool:
        if self.raise_worker_error:
            self.raise_worker_error = False
            raise self.error
        return super().is_foreground(bound)


class FreshFrameSource:
    def __init__(self, frame: np.ndarray | None = None) -> None:
        self.frame = (
            frame
            if frame is not None
            else np.zeros((1080, 1920, 3), dtype=np.uint8)
        )
        self.started: list[tuple[int, int]] = []
        self.stop_calls = 0

    def start(self, device_index: int, output_index: int) -> None:
        self.started.append((device_index, output_index))

    def latest(self) -> FramePacket:
        now = time.monotonic()
        return FramePacket(self.frame, now, 30.0)

    def stop(self) -> None:
        self.stop_calls += 1


class BlockingSecondLatestFailure(FreshFrameSource):
    def __init__(self) -> None:
        super().__init__()
        self.latest_calls = 0
        self.second_latest_entered = threading.Event()
        self.allow_second_latest = threading.Event()

    def latest(self) -> FramePacket:
        self.latest_calls += 1
        if self.latest_calls == 2:
            self.second_latest_entered.set()
            assert self.allow_second_latest.wait(timeout=1)
            raise RuntimeError("旧截图调用迟到失败")
        return super().latest()


class BlockingSecondStaleFrame(FreshFrameSource):
    def __init__(self) -> None:
        super().__init__()
        self.latest_calls = 0
        self.second_latest_entered = threading.Event()
        self.allow_second_latest = threading.Event()

    def latest(self) -> FramePacket:
        self.latest_calls += 1
        if self.latest_calls == 2:
            self.second_latest_entered.set()
            assert self.allow_second_latest.wait(timeout=1)
            return FramePacket(self.frame, time.monotonic() - 0.3, 30.0)
        return super().latest()


class FixedFrameSource(FreshFrameSource):
    def __init__(self, timestamp: float) -> None:
        super().__init__()
        self.timestamp = timestamp

    def latest(self) -> FramePacket:
        return FramePacket(self.frame, self.timestamp, 30.0)


class DelayedFrameSource(FreshFrameSource):
    def __init__(self) -> None:
        super().__init__()
        self.returned = threading.Event()

    def latest(self) -> FramePacket:
        timestamp = time.monotonic()
        time.sleep(0.25)
        self.returned.set()
        return FramePacket(self.frame, timestamp, 30.0)


class BlockingFrameSource(FreshFrameSource):
    def __init__(self, unblock_on_stop: bool) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.unblock = threading.Event()
        self.unblock_on_stop = unblock_on_stop

    def latest(self) -> FramePacket:
        self.entered.set()
        self.unblock.wait(timeout=5)
        return super().latest()

    def stop(self) -> None:
        super().stop()
        if self.unblock_on_stop:
            self.unblock.set()


class BlockingStopFrameSource(FreshFrameSource):
    def __init__(self) -> None:
        super().__init__()
        self.stop_entered = threading.Event()
        self.allow_stop = threading.Event()
        self.stop_returned = threading.Event()

    def stop(self) -> None:
        self.stop_calls += 1
        self.stop_entered.set()
        self.allow_stop.wait(timeout=5)
        self.stop_returned.set()


class StartStopRaceFrameSource(FreshFrameSource):
    def __init__(self) -> None:
        super().__init__()
        self.start_entered = threading.Event()
        self.allow_start = threading.Event()
        self.stop_entered = threading.Event()
        self.active = False

    def start(self, device_index: int, output_index: int) -> None:
        self.started.append((device_index, output_index))
        self.start_entered.set()
        assert self.allow_start.wait(timeout=1)
        self.active = True

    def stop(self) -> None:
        self.stop_calls += 1
        self.stop_entered.set()
        self.active = False


class SwitchingFrameSource(FreshFrameSource):
    def __init__(self, *, fail_restart: bool = False) -> None:
        super().__init__()
        self.fail_restart = fail_restart
        self.restart_entered = threading.Event()
        self.allow_restart = threading.Event()
        self.new_latest_entered = threading.Event()
        self.allow_new_frame = threading.Event()
        self.latest_calls = 0

    def start(self, device_index: int, output_index: int) -> None:
        self.started.append((device_index, output_index))
        if len(self.started) > 1:
            self.restart_entered.set()
            if self.fail_restart:
                raise RuntimeError("restart capture failed")
            assert self.allow_restart.wait(timeout=1)

    def latest(self) -> FramePacket:
        self.latest_calls += 1
        if self.latest_calls > 1:
            self.new_latest_entered.set()
            assert self.allow_new_frame.wait(timeout=1)
        return super().latest()


class ScriptedRecognizer:
    def __init__(
        self,
        observations: list[SceneObservation] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.observations = list(observations or [])
        self.error = error
        self.frames: list[np.ndarray] = []
        self.observed = threading.Event()

    def set_bite_baseline(self, _frame: np.ndarray) -> None:
        return None

    def observe(self, frame: np.ndarray, _timestamp: float) -> SceneObservation:
        self.frames.append(frame)
        self.observed.set()
        if self.error is not None:
            raise self.error
        if self.observations:
            return self.observations.pop(0)
        return SceneObservation()


class OrderedResumeRecognizer(ScriptedRecognizer):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events
        self.resume_frames = 0
        self.resume_mode = False

    def observe(self, frame: np.ndarray, timestamp: float) -> SceneObservation:
        if not self.resume_mode:
            return super().observe(frame, timestamp)
        self.resume_frames += 1
        self.events.append(f"frame-{self.resume_frames}")
        if self.resume_frames < 3:
            return SceneObservation()
        return SceneObservation(ready=True)


class BarrierRecognizer(ScriptedRecognizer):
    def __init__(self, observations: list[SceneObservation]) -> None:
        super().__init__(observations)
        self.block = False
        self.entered = threading.Event()
        self.allow = threading.Event()
        self.returned = threading.Event()

    def observe(self, frame: np.ndarray, timestamp: float) -> SceneObservation:
        if self.block:
            self.entered.set()
            assert self.allow.wait(timeout=1)
        result = super().observe(frame, timestamp)
        self.returned.set()
        return result


class CapturedBarrierRecognizer(ScriptedRecognizer):
    def __init__(self, observations: list[SceneObservation]) -> None:
        super().__init__(observations)
        self.entered = threading.Event()
        self.allow = threading.Event()

    def observe(self, frame: np.ndarray, timestamp: float) -> SceneObservation:
        result = super().observe(frame, timestamp)
        self.entered.set()
        assert self.allow.wait(timeout=1)
        return result


class AbaRecognizer(ScriptedRecognizer):
    def __init__(self) -> None:
        super().__init__([SceneObservation()])
        self.block_a = False
        self.a_entered = threading.Event()
        self.allow_a = threading.Event()
        self.a_returned = threading.Event()
        self.b_entered = threading.Event()
        self.allow_b = threading.Event()
        self.b_returned = threading.Event()

    def observe(self, frame: np.ndarray, timestamp: float) -> SceneObservation:
        if self.block_a:
            self.block_a = False
            result = SceneObservation(ready=True)
            self.a_entered.set()
            assert self.allow_a.wait(timeout=1)
            self.a_returned.set()
            return result
        if self.a_returned.is_set() and not self.b_returned.is_set():
            self.b_entered.set()
            assert self.allow_b.wait(timeout=1)
            self.b_returned.set()
            return SceneObservation(result=True)
        return super().observe(frame, timestamp)


class SnapshotBarrierCore:
    def __init__(self, core: AutomationCore) -> None:
        object.__setattr__(self, "core", core)
        object.__setattr__(self, "armed", False)
        object.__setattr__(self, "snapshot_entered", threading.Event())
        object.__setattr__(self, "allow_snapshot", threading.Event())

    def __getattr__(self, name: str):
        return getattr(self.core, name)

    def __setattr__(self, name: str, value) -> None:
        if name in {"core", "armed", "snapshot_entered", "allow_snapshot"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self.core, name, value)

    @property
    def snapshot(self):
        snapshot = self.core.snapshot
        if self.armed:
            self.armed = False
            self.snapshot_entered.set()
            assert self.allow_snapshot.wait(timeout=1)
        return snapshot


class StartBarrierCore:
    def __init__(self, core: AutomationCore) -> None:
        object.__setattr__(self, "core", core)
        object.__setattr__(self, "start_entered", threading.Event())
        object.__setattr__(self, "allow_start", threading.Event())

    def __getattr__(self, name: str):
        return getattr(self.core, name)

    def __setattr__(self, name: str, value) -> None:
        if name in {"core", "start_entered", "allow_start"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self.core, name, value)

    def start(self, target: int, now: float) -> None:
        self.start_entered.set()
        assert self.allow_start.wait(timeout=1)
        self.core.start(target, now)


class BlockingInputActionError(InputActionError):
    def __init__(self) -> None:
        super().__init__("迟到的释放输入错误")
        self.stringify_entered = threading.Event()
        self.allow_stringify = threading.Event()

    def __str__(self) -> str:
        self.stringify_entered.set()
        assert self.allow_stringify.wait(timeout=1)
        return super().__str__()


class LateReleaseCoreProxy:
    def __init__(
        self,
        core: AutomationCore,
        late_error: BlockingInputActionError,
    ) -> None:
        self.core = core
        self.late_error = late_error

    def __getattr__(self, name: str):
        return getattr(self.core, name)

    def release_inputs(self) -> None:
        try:
            self.core.release_inputs()
        except InputActionError as error:
            raise self.late_error from error


def single_round_observations() -> list[SceneObservation]:
    progress = ProgressObservation(0.3, 0.7, 0.2, 1.0, 2.0)
    return [
        SceneObservation(),
        SceneObservation(bite=True),
        SceneObservation(progress=progress),
        SceneObservation(progress=progress),
        SceneObservation(result_candidate=True),
        SceneObservation(result_candidate=True),
        SceneObservation(result=True),
        SceneObservation(result=True),
        SceneObservation(ready=True),
    ]


def wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not met before timeout")


def make_core(
    *,
    state: FishingState = FishingState.UNBOUND,
) -> tuple[AutomationCore, RecordingInput, FishingStateMachine]:
    input_service = RecordingInput()
    state_machine = FishingStateMachine()
    core = AutomationCore(
        state_machine=state_machine,
        controller=ProgressController(),
        input_service=input_service,
        scene_recognizer=SceneRecognizer(),
    )
    if state is not FishingState.UNBOUND:
        core.start(1, 0.0)
    if state is FishingState.CONTROL:
        state_machine.handle(Event.CAST_SENT, 0.01)
        state_machine.handle(Event.REEL_SENT, 0.02)
        state_machine.handle(Event.BAR_DETECTED, 0.03)
    return core, input_service, state_machine


def make_engine(
    tmp_path,
    *,
    frame_source: FreshFrameSource | None = None,
    recognizer: ScriptedRecognizer | None = None,
    window_service: RecordingWindowService | None = None,
    input_service: RecordingInput | None = None,
) -> tuple[
    AutomationEngine,
    AutomationCore,
    RecordingInput,
    RecordingWindowService,
    FreshFrameSource,
]:
    input_service = input_service or RecordingInput()
    window_service = window_service or RecordingWindowService()
    frame_source = frame_source or FreshFrameSource()
    recognizer = recognizer or ScriptedRecognizer()
    state_machine = FishingStateMachine()
    core = AutomationCore(
        state_machine=state_machine,
        controller=ProgressController(),
        input_service=input_service,
        scene_recognizer=recognizer,
    )
    engine = AutomationEngine(
        core=core,
        window_service=window_service,
        frame_source=frame_source,
        scene_recognizer=recognizer,
        diagnostics=DiagnosticsStore(tmp_path / "diagnostics"),
    )
    engine.bind(BOUND)
    return engine, core, input_service, window_service, frame_source


def test_core_drives_single_round_and_counts_only_after_ready() -> None:
    core, input_service, _state_machine = make_core()
    progress = ProgressObservation(0.3, 0.7, 0.2, 1.0, 2.0)
    sequence = [
        SceneObservation(),
        SceneObservation(bite=True),
        SceneObservation(progress=progress),
        SceneObservation(progress=progress),
        SceneObservation(result_candidate=True),
        SceneObservation(result_candidate=True),
        SceneObservation(result=True),
        SceneObservation(result=True),
    ]
    core.start(1, 0.0)
    for index, observation in enumerate(sequence, 1):
        core.process(observation, None, float(index), CLIENT)

    assert core.snapshot.state is FishingState.DISMISS_RESULT
    assert core.snapshot.completed == 0
    core.process(SceneObservation(ready=True), None, 9.0, CLIENT)

    assert core.snapshot.state is FishingState.COMPLETE
    assert core.snapshot.completed == 1
    assert input_service.events.count("F") == 2
    assert ("click", 192, 396) in input_service.events


def test_core_releases_on_first_missing_bar_and_pauses_at_six_frames() -> None:
    core, input_service, _state_machine = make_core(state=FishingState.CONTROL)

    for index in range(6):
        core.process(SceneObservation(), None, 0.04 + index / 30, None)

    assert input_service.events[0] == "release"
    assert core.snapshot.state is FishingState.PAUSED
    assert core.pause_code == "E_PROGRESS_LOST"
    assert "连续六帧" in core.snapshot.error


def test_core_requires_two_result_candidates_before_leaving_control() -> None:
    core, input_service, _state_machine = make_core(state=FishingState.CONTROL)

    core.process(SceneObservation(result_candidate=True), None, 0.1, CLIENT)
    assert core.snapshot.state is FishingState.CONTROL
    core.process(SceneObservation(result_candidate=True), None, 0.2, CLIENT)
    assert core.snapshot.state is FishingState.WAIT_RESULT
    core.process(SceneObservation(), None, 0.3, CLIENT)

    assert core.snapshot.state is FishingState.WAIT_RESULT
    assert not any(isinstance(event, tuple) for event in input_service.events)


def test_result_candidates_on_sixth_and_seventh_missing_frames_win_over_loss() -> None:
    core, _input_service, _state_machine = make_core(
        state=FishingState.CONTROL
    )
    for index in range(5):
        core.process(SceneObservation(), None, index / 30, CLIENT)

    core.process(
        SceneObservation(result_candidate=True),
        None,
        5 / 30,
        CLIENT,
    )
    assert core.snapshot.state is FishingState.CONTROL

    core.process(
        SceneObservation(result_candidate=True),
        None,
        6 / 30,
        CLIENT,
    )

    assert core.snapshot.state is FishingState.WAIT_RESULT


def test_single_sixth_frame_candidate_then_interruption_is_progress_loss() -> None:
    core, _input_service, _state_machine = make_core(
        state=FishingState.CONTROL
    )
    for index in range(5):
        core.process(SceneObservation(), None, index / 30, CLIENT)

    core.process(
        SceneObservation(result_candidate=True),
        None,
        5 / 30,
        CLIENT,
    )
    assert core.snapshot.state is FishingState.CONTROL

    core.process(SceneObservation(), None, 6 / 30, CLIENT)

    assert core.snapshot.state is FishingState.PAUSED
    assert core.pause_code == "E_PROGRESS_LOST"


def test_engine_complete_exits_worker_and_allows_restart(tmp_path) -> None:
    recognizer = ScriptedRecognizer(single_round_observations())
    engine, core, input_service, _window, source = make_engine(
        tmp_path,
        recognizer=recognizer,
    )

    engine.start(1)
    try:
        wait_until(lambda: core.snapshot.state is FishingState.COMPLETE)
        wait_until(lambda: engine.is_running is False)
        completed_event_count = len(input_service.events)
        time.sleep(0.02)

        assert core.snapshot.state is FishingState.COMPLETE
        assert core.snapshot.completed == 1
        assert core.input_blocked is True
        assert input_service.events[-1] == "release"
        assert len(input_service.events) == completed_event_count
        assert source.stop_calls == 1
        assert engine._thread is None

        engine.bind(BOUND)
        engine.start(2)
        wait_until(lambda: core.snapshot.state is FishingState.WAIT_BITE)

        assert core.snapshot.target == 2
        assert engine.is_running is True
        assert source.started == [(0, 0), (0, 0)]
    finally:
        engine.shutdown()


def test_complete_publish_racing_shutdown_preserves_terminal_state(
    tmp_path,
) -> None:
    recognizer = ScriptedRecognizer(single_round_observations())
    engine, core, input_service, _window, _source = make_engine(
        tmp_path,
        recognizer=recognizer,
    )
    complete_publish_entered = threading.Event()
    allow_complete_publish = threading.Event()

    def subscriber(snapshot) -> None:
        if (
            snapshot.state is FishingState.COMPLETE
            and not complete_publish_entered.is_set()
        ):
            complete_publish_entered.set()
            assert allow_complete_publish.wait(timeout=2.2)

    engine.subscribe(subscriber)
    engine.start(1)
    assert complete_publish_entered.wait(timeout=1)
    releases_before_shutdown = input_service.events.count("release")
    shutdown_thread = threading.Thread(target=engine.shutdown)
    shutdown_thread.start()

    try:
        wait_until(
            lambda: input_service.events.count("release")
            > releases_before_shutdown
        )
        allow_complete_publish.set()
        shutdown_thread.join(timeout=1)

        assert shutdown_thread.is_alive() is False
        assert core.snapshot.state is FishingState.COMPLETE
        assert core.snapshot.completed == 1
        assert core.input_blocked is True
    finally:
        allow_complete_publish.set()
        shutdown_thread.join(timeout=1)
        engine.shutdown()


def test_pause_and_f8_after_worker_exit_preserve_complete(tmp_path) -> None:
    recognizer = ScriptedRecognizer(single_round_observations())
    engine, core, input_service, _window, _source = make_engine(
        tmp_path,
        recognizer=recognizer,
    )

    engine.start(1)
    try:
        wait_until(lambda: engine.is_running is False)
        releases_before_pause = input_service.events.count("release")

        engine.pause("按钮暂停")
        assert core.snapshot.state is FishingState.COMPLETE
        engine.pause("F8 紧急暂停")

        assert core.snapshot.state is FishingState.COMPLETE
        assert core.snapshot.completed == 1
        assert core.input_blocked is True
        assert input_service.events.count("release") == (
            releases_before_pause + 2
        )
    finally:
        engine.shutdown()


def test_complete_pause_records_release_failure_without_losing_terminal_state(
) -> None:
    input_service = ReleaseFailingInput(fail_release=False)
    state_machine = FishingStateMachine()
    core = AutomationCore(
        state_machine=state_machine,
        controller=ProgressController(),
        input_service=input_service,
        scene_recognizer=SceneRecognizer(),
    )
    core.start(1, 0.0)
    for index, observation in enumerate(single_round_observations(), 1):
        core.process(observation, None, float(index), CLIENT)
    assert core.snapshot.state is FishingState.COMPLETE
    input_service.fail_release = True

    core.pause("F8 terminal release", 10.0)

    assert core.snapshot.state is FishingState.COMPLETE
    assert core.snapshot.completed == 1
    assert core.input_blocked is True
    assert core.pause_code == "E_INPUT"
    assert "F8 terminal release" in core.snapshot.error
    assert "release_all failed: key release failed" in core.snapshot.error


def test_core_pause_still_transitions_non_terminal_state_to_paused() -> None:
    core, input_service, _state_machine = make_core(
        state=FishingState.CONTROL
    )

    core.pause("普通暂停", 1.0, code="E_USER_PAUSE")

    assert core.snapshot.state is FishingState.PAUSED
    assert core.snapshot.error == "普通暂停"
    assert core.pause_code == "E_USER_PAUSE"
    assert core.input_blocked is True
    assert input_service.events[-1] == "release"


def test_core_stale_frame_releases_after_point_two_and_pauses_after_point_five() -> None:
    core, input_service, _state_machine = make_core(state=FishingState.CONTROL)
    frame = np.zeros((10, 10, 3), dtype=np.uint8)

    core.process(
        SceneObservation(), FramePacket(frame, 1.0, 30.0), 1.3, None
    )
    assert core.snapshot.state is FishingState.CONTROL
    assert input_service.events[-1] == "release"
    assert core.bar_missing_frames == 0

    core.process(
        SceneObservation(), FramePacket(frame, 1.0, 30.0), 1.6, None
    )
    assert core.snapshot.state is FishingState.PAUSED
    assert input_service.events[-1] == "release"


def test_core_old_ready_frame_releases_without_casting_or_advancing() -> None:
    core, input_service, _state_machine = make_core()
    core.start(1, 0.0)

    core.process(
        SceneObservation(),
        FramePacket(np.zeros((10, 10, 3), dtype=np.uint8), 1.0, 30.0),
        1.3,
        CLIENT,
    )

    assert input_service.events == ["release"]
    assert core.snapshot.state is FishingState.READY


def test_pause_serializes_with_inflight_process_and_finishes_with_release() -> None:
    input_service = BarrierInput()
    state_machine = FishingStateMachine()
    core = AutomationCore(
        state_machine=state_machine,
        controller=ProgressController(),
        input_service=input_service,
        scene_recognizer=SceneRecognizer(),
    )
    core.start(1, 0.0)
    process_errors: list[BaseException] = []
    pause_returned = threading.Event()

    def process_frame() -> None:
        try:
            core.process(SceneObservation(), None, 0.1, CLIENT)
        except BaseException as error:
            process_errors.append(error)

    process_thread = threading.Thread(target=process_frame)
    process_thread.start()
    assert input_service.tap_entered.wait(timeout=1)
    pause_thread = threading.Thread(
        target=lambda: (core.pause("race pause", 0.2), pause_returned.set())
    )
    pause_thread.start()
    assert pause_returned.wait(timeout=0.05) is False

    input_service.allow_tap.set()
    process_thread.join(timeout=1)
    pause_thread.join(timeout=1)
    core.process(SceneObservation(bite=True), None, 0.3, CLIENT)

    assert process_errors == []
    assert pause_returned.is_set()
    assert core.snapshot.state is FishingState.PAUSED
    assert input_service.events == ["F", "release"]


def test_core_timeout_and_resume_classify_current_scene() -> None:
    core, _input_service, state_machine = make_core()
    core.start(1, 0.0)
    core.process(SceneObservation(), None, 3.1, CLIENT)

    assert core.snapshot.state is FishingState.PAUSED
    assert core.pause_code == "E_TIMEOUT"

    assert core.resume(SceneObservation(result=True), 4.0) is True
    assert state_machine.state is FishingState.WAIT_RESULT
    core.pause("用户暂停", 4.1)
    assert core.resume(SceneObservation(), 4.2) is False
    assert state_machine.state is FishingState.PAUSED


@pytest.mark.parametrize(
    ("failure_kind", "expected_code"),
    [("window", "E_WINDOW"), ("input", "E_INPUT"), ("vision", "E_VISION")],
)
def test_engine_classifies_failures_and_saves_one_diagnostic(
    tmp_path, failure_kind: str, expected_code: str
) -> None:
    input_service = RecordingInput()
    window_service = (
        ForegroundDropsAfterStartWindowService()
        if failure_kind == "window"
        else RecordingWindowService()
    )
    recognizer = ScriptedRecognizer()
    if failure_kind == "input":
        input_service.failure = InputFailure("SendInput failed")
    elif failure_kind == "vision":
        recognizer.error = RuntimeError("recognizer failed")
    engine, core, input_service, _window, _source = make_engine(
        tmp_path,
        recognizer=recognizer,
        window_service=window_service,
        input_service=input_service,
    )
    snapshots = []
    engine.subscribe(snapshots.append)

    engine.start(1)
    wait_until(lambda: core.snapshot.state is FishingState.PAUSED)
    engine.pause("duplicate pause")
    engine.shutdown()

    assert core.pause_code == expected_code
    assert input_service.events[-1] == "release"
    assert len(list((tmp_path / "diagnostics").glob("*.json"))) == 1
    assert snapshots[-1].state is FishingState.PAUSED


def test_engine_pauses_stale_frame_and_user_pause_paths(tmp_path) -> None:
    stale_source = FixedFrameSource(time.monotonic() - 0.6)
    engine, core, input_service, _window, _source = make_engine(
        tmp_path / "stale", frame_source=stale_source
    )
    engine.start(1)
    wait_until(lambda: core.snapshot.state is FishingState.PAUSED)
    engine.shutdown()

    assert core.pause_code == "E_STALE_FRAME"
    assert input_service.events[-1] == "release"

    engine, core, input_service, _window, _source = make_engine(
        tmp_path / "user"
    )
    engine.start(1)
    wait_until(lambda: core.snapshot.state is FishingState.WAIT_BITE)
    engine.pause("按钮暂停")
    engine.pause("F8")
    engine.shutdown()

    assert core.pause_code == "E_USER_PAUSE"
    assert input_service.events[-1] == "release"
    assert len(list((tmp_path / "user" / "diagnostics").glob("*.json"))) <= 1


def test_frame_age_is_measured_after_latest_frame_returns(tmp_path) -> None:
    source = DelayedFrameSource()
    recognizer = ScriptedRecognizer()
    engine, core, input_service, _window, _source = make_engine(
        tmp_path, frame_source=source, recognizer=recognizer
    )
    engine.start(1)
    assert source.returned.wait(timeout=1)
    wait_until(lambda: "release" in input_service.events)

    assert "F" not in input_service.events
    assert recognizer.frames == []
    assert core.snapshot.state is FishingState.READY
    engine.shutdown()


def test_engine_classifies_release_failure_as_input_error(tmp_path) -> None:
    progress = ProgressObservation(0.3, 0.7, 0.2, 1.0, 0.0)
    recognizer = ScriptedRecognizer(
        [
            SceneObservation(),
            SceneObservation(bite=True),
            SceneObservation(progress=progress),
            SceneObservation(),
        ]
    )
    input_service = ReleaseFailingInput()
    engine, core, input_service, _window, _source = make_engine(
        tmp_path,
        recognizer=recognizer,
        input_service=input_service,
    )

    engine.start(1)
    wait_until(lambda: core.snapshot.state is FishingState.PAUSED)
    engine.shutdown()

    assert core.pause_code == "E_INPUT"
    assert input_service.events[-1] == "release"


@pytest.mark.parametrize("failure_kind", ["window", "vision", "stale"])
def test_engine_safety_release_failure_overrides_cause_with_input_error(
    tmp_path, failure_kind: str
) -> None:
    input_service = ReleaseFailingInput()
    window_service = (
        ForegroundDropsAfterStartWindowService()
        if failure_kind == "window"
        else RecordingWindowService()
    )
    recognizer = ScriptedRecognizer()
    source: FreshFrameSource = FreshFrameSource()
    if failure_kind == "vision":
        recognizer.error = RuntimeError("vision root cause")
    elif failure_kind == "stale":
        source = FixedFrameSource(time.monotonic() - 0.6)
    engine, core, _input, _window, _source = make_engine(
        tmp_path,
        frame_source=source,
        recognizer=recognizer,
        window_service=window_service,
        input_service=input_service,
    )

    engine.start(1)
    wait_until(lambda: core.snapshot.state is FishingState.PAUSED)
    engine.shutdown()

    metadata = json.loads(next((tmp_path / "diagnostics").glob("*.json")).read_text("utf-8"))
    assert core.pause_code == "E_INPUT"
    assert "key release failed" in core.snapshot.error
    assert metadata["code"] == "E_INPUT"
    assert "key release failed" in metadata["detail"]


def test_core_timeout_release_failure_still_pauses_as_input_error() -> None:
    input_service = ReleaseFailingInput()
    state_machine = FishingStateMachine()
    core = AutomationCore(
        state_machine=state_machine,
        controller=ProgressController(),
        input_service=input_service,
        scene_recognizer=SceneRecognizer(),
    )
    core.start(1, 0.0)

    core.process(SceneObservation(), None, 3.1, CLIENT)

    assert core.snapshot.state is FishingState.PAUSED
    assert core.pause_code == "E_INPUT"
    assert "key release failed" in core.snapshot.error


def test_engine_refreshes_client_rect_and_crops_monitor_frame(tmp_path) -> None:
    window_service = RecordingWindowService()
    window_service.refreshed = BoundWindow(
        100,
        "异环",
        Rect(100, 50, 1060, 590),
        MONITOR,
        0,
        0,
    )
    recognizer = ScriptedRecognizer()
    engine, _core, _input, _window, _source = make_engine(
        tmp_path, recognizer=recognizer, window_service=window_service
    )

    engine.start(1)
    wait_until(lambda: window_service.refresh_calls >= 1)
    wait_until(
        lambda: any(frame.shape[:2] == (540, 960) for frame in recognizer.frames)
    )
    engine.shutdown()

    assert window_service.refresh_calls >= 1
    assert any(frame.shape[:2] == (540, 960) for frame in recognizer.frames)


def test_output_restart_discards_packet_from_previous_capture_source(tmp_path) -> None:
    source = SwitchingFrameSource()
    window_service = RecordingWindowService()
    window_service.refreshed = BoundWindow(
        100, "异环", CLIENT, MONITOR, 1, 0
    )
    recognizer = ScriptedRecognizer()
    engine, core, input_service, _window, _source = make_engine(
        tmp_path,
        frame_source=source,
        recognizer=recognizer,
        window_service=window_service,
    )
    engine.start(1)
    assert source.restart_entered.wait(timeout=1)
    source.allow_restart.set()
    assert source.new_latest_entered.wait(timeout=1)

    assert recognizer.observed.is_set() is False
    assert "F" not in input_service.events

    source.allow_new_frame.set()
    wait_until(lambda: core.snapshot.state is FishingState.WAIT_BITE)
    engine.shutdown()


def test_output_restart_failure_is_capture_error(tmp_path) -> None:
    source = SwitchingFrameSource(fail_restart=True)
    window_service = RecordingWindowService()
    window_service.refreshed = BoundWindow(
        100, "异环", CLIENT, MONITOR, 1, 0
    )
    engine, core, _input, _window, _source = make_engine(
        tmp_path, frame_source=source, window_service=window_service
    )
    engine.start(1)
    wait_until(lambda: core.snapshot.state is FishingState.PAUSED)
    engine.shutdown()

    assert core.pause_code == "E_CAPTURE"


def test_engine_resume_reclassifies_result_before_allowing_click(tmp_path) -> None:
    recognizer = ScriptedRecognizer([SceneObservation()])
    engine, core, input_service, _window, _source = make_engine(
        tmp_path, recognizer=recognizer
    )
    engine.start(1)
    wait_until(lambda: core.snapshot.state is FishingState.WAIT_BITE)
    engine.pause("用户暂停")
    recognizer.observations.extend(
        [SceneObservation(result=True), SceneObservation(result=True)]
    )
    engine.resume()
    try:
        wait_until(lambda: core.snapshot.state is FishingState.DISMISS_RESULT)
    except AssertionError as error:
        raise AssertionError(
            f"snapshot={core.snapshot!r}, events={input_service.events!r}, "
            f"remaining={recognizer.observations!r}, "
            f"resume_request={engine._resume_request!r}, "
            f"pause_epoch={engine._pause_epoch}, "
            f"running={engine.is_running}"
        ) from error

    assert core.snapshot.state is FishingState.DISMISS_RESULT
    assert not any(isinstance(event, tuple) for event in input_service.events)
    engine.shutdown()


def test_engine_start_checks_foreground_without_forcing_activation(
    tmp_path,
) -> None:
    window_service = RecordingWindowService()
    engine, core, _input, _window, _source = make_engine(
        tmp_path,
        window_service=window_service,
    )

    engine.start(1)
    try:
        wait_until(lambda: core.snapshot.state is FishingState.WAIT_BITE)

        assert window_service.activate_calls == 0
    finally:
        engine.shutdown()


def test_engine_start_rejects_background_game_without_worker(tmp_path) -> None:
    window_service = RecordingWindowService()
    window_service.foreground = False
    engine, core, _input, _window, source = make_engine(
        tmp_path,
        window_service=window_service,
    )

    with pytest.raises(
        RuntimeError,
        match="请在倒计时结束前切回已绑定的游戏窗口",
    ):
        engine.start(1)

    assert window_service.activate_calls == 0
    assert source.started == []
    assert engine.is_running is False
    assert core.snapshot.state is FishingState.UNBOUND


def test_resume_after_manual_foreground_keeps_request_until_third_stable_frame(
    tmp_path,
) -> None:
    events: list[str] = []
    window_service = ActivatingWindowService(events)
    window_service.foreground = True
    recognizer = OrderedResumeRecognizer(events)
    engine, core, input_service, _window, _source = make_engine(
        tmp_path,
        recognizer=recognizer,
        window_service=window_service,
    )
    engine.start(1)
    wait_until(lambda: core.snapshot.state is FishingState.WAIT_BITE)
    engine.pause("用户暂停")
    events.clear()
    events.append("ui-continue")
    window_service.foreground = False
    recognizer.resume_mode = True

    # The UI countdown gives the player time to restore game focus.
    window_service.foreground = True

    engine.resume()
    wait_until(lambda: core.snapshot.state is FishingState.WAIT_BITE)

    try:
        assert events[:2] == ["ui-continue", "foreground"]
        assert "activate" not in events
        assert [event for event in events if event.startswith("frame-")][:3] == [
            "frame-1",
            "frame-2",
            "frame-3",
        ]
        assert recognizer.resume_frames >= 3
        assert engine._resume_request is None
        assert input_service.events.count("F") == 2
    finally:
        engine.shutdown()


def test_late_window_error_cannot_invalidate_new_resume_request(tmp_path) -> None:
    error = BlockingWindowError()
    window_service = LateErrorWindowService(error)
    recognizer = OrderedResumeRecognizer([])
    engine, core, input_service, _window, _source = make_engine(
        tmp_path,
        recognizer=recognizer,
        window_service=window_service,
    )
    engine.start(1)
    wait_until(lambda: core.snapshot.state is FishingState.WAIT_BITE)
    window_service.raise_worker_error = True
    assert error.stringify_entered.wait(timeout=1)

    engine.pause("用户暂停")
    recognizer.resume_mode = True
    engine.resume()
    resume_epoch = engine._pause_epoch
    assert engine._resume_request == resume_epoch
    error.allow_stringify.set()

    try:
        wait_until(lambda: core.snapshot.state is FishingState.WAIT_BITE)
        assert engine._pause_epoch == resume_epoch
        assert engine._resume_request is None
        assert recognizer.resume_frames >= 3
        assert input_service.events.count("F") == 2
    finally:
        error.allow_stringify.set()
        engine.shutdown()


def test_late_latest_error_cannot_clear_resume_token_or_exit_worker(tmp_path) -> None:
    source = BlockingSecondLatestFailure()
    recognizer = OrderedResumeRecognizer([])
    engine, core, input_service, _window, _source = make_engine(
        tmp_path,
        frame_source=source,
        recognizer=recognizer,
    )
    engine.start(1)
    assert source.second_latest_entered.wait(timeout=1)
    assert core.snapshot.state is FishingState.WAIT_BITE

    engine.pause("用户暂停")
    recognizer.resume_mode = True
    engine.resume()
    resume_epoch = engine._pause_epoch
    assert engine._resume_request == resume_epoch
    source.allow_second_latest.set()

    try:
        wait_until(lambda: core.snapshot.state is FishingState.WAIT_BITE)
        assert engine._pause_epoch == resume_epoch
        assert engine._resume_request is None
        assert engine.is_running is True
        assert recognizer.resume_frames >= 3
        assert input_service.events.count("F") == 2
    finally:
        source.allow_second_latest.set()
        engine.shutdown()


def test_late_stale_frame_release_error_cannot_clear_resume_token(tmp_path) -> None:
    source = BlockingSecondStaleFrame()
    input_service = ReleaseFailingInput(fail_release=False)
    recognizer = OrderedResumeRecognizer([])
    engine, core, _input, _window, _source = make_engine(
        tmp_path,
        frame_source=source,
        input_service=input_service,
        recognizer=recognizer,
    )
    engine.start(1)
    assert source.second_latest_entered.wait(timeout=1)
    assert core.snapshot.state is FishingState.WAIT_BITE
    late_error = BlockingInputActionError()
    engine.core = LateReleaseCoreProxy(core, late_error)
    input_service.fail_release = True
    source.allow_second_latest.set()
    assert late_error.stringify_entered.wait(timeout=1)

    engine.pause("用户暂停")
    recognizer.resume_mode = True
    engine.resume()
    resume_epoch = engine._pause_epoch
    assert engine._resume_request == resume_epoch
    late_error.allow_stringify.set()

    try:
        wait_until(lambda: core.snapshot.state is FishingState.WAIT_BITE)
        assert engine._pause_epoch == resume_epoch
        assert engine._resume_request is None
        assert engine.is_running is True
        assert recognizer.resume_frames >= 3
        assert input_service.events.count("F") == 2
    finally:
        late_error.allow_stringify.set()
        source.allow_second_latest.set()
        engine.shutdown()


def test_resume_requires_manual_foreground_without_activation(tmp_path) -> None:
    window_service = RecordingWindowService()
    engine, core, _input, _window, _source = make_engine(
        tmp_path,
        window_service=window_service,
    )
    engine.start(1)
    wait_until(lambda: core.snapshot.state is FishingState.WAIT_BITE)
    activate_calls_before_resume = window_service.activate_calls
    engine.pause("用户暂停")
    window_service.foreground = False

    engine.resume()

    try:
        assert core.snapshot.state is FishingState.PAUSED
        assert core.pause_code == "E_WINDOW"
        assert core.snapshot.error == "请在倒计时结束前切回已绑定的游戏窗口"
        assert engine._resume_request is None
        assert window_service.activate_calls == activate_calls_before_resume
    finally:
        engine.shutdown()


def test_window_invalid_pause_can_cancel_rebind_and_start_again(tmp_path) -> None:
    window_service = RecordingWindowService()
    engine, core, input_service, _window, source = make_engine(
        tmp_path,
        window_service=window_service,
    )
    engine.start(1)
    wait_until(lambda: core.snapshot.state is FishingState.WAIT_BITE)
    window_service.foreground = False
    wait_until(lambda: core.snapshot.state is FishingState.PAUSED)

    engine.cancel_current()

    assert core.snapshot.state is FishingState.UNBOUND
    assert engine.is_running is False
    assert engine._thread is None
    assert source.stop_calls >= 1
    assert input_service.events[-1] == "release"

    window_service.foreground = True
    new_bound = BoundWindow(200, "异环-新窗口", CLIENT, MONITOR, 0, 0)
    engine.bind(new_bound)
    engine.start(1)
    try:
        wait_until(lambda: core.snapshot.state is FishingState.WAIT_BITE)
        assert source.started == [(0, 0), (0, 0)]
    finally:
        engine.shutdown()


def test_resume_does_not_consume_observation_captured_before_request(tmp_path) -> None:
    recognizer = CapturedBarrierRecognizer(
        [
            SceneObservation(),
            SceneObservation(result=True),
            SceneObservation(result=True),
        ]
    )
    engine, core, _input, _window, _source = make_engine(
        tmp_path, recognizer=recognizer
    )
    engine.start(1)
    assert recognizer.entered.wait(timeout=1)

    engine.pause("pause during recognition")
    engine.resume()
    recognizer.allow.set()
    wait_until(lambda: core.snapshot.state is FishingState.DISMISS_RESULT)

    assert core.snapshot.state is FishingState.DISMISS_RESULT
    engine.shutdown()


def test_successful_resume_starts_a_new_single_diagnostic_incident(tmp_path) -> None:
    recognizer = ScriptedRecognizer([SceneObservation()])
    engine, core, input_service, _window, _source = make_engine(
        tmp_path, recognizer=recognizer
    )
    engine.start(1)
    wait_until(lambda: core.snapshot.state is FishingState.WAIT_BITE)
    engine.pause("first pause")
    wait_until(
        lambda: len(list((tmp_path / "diagnostics").glob("*.json"))) == 1
    )

    recognizer.observations.append(SceneObservation(ready=True))
    engine.resume()
    wait_until(lambda: input_service.events.count("F") == 2)
    engine.pause("second pause")
    engine.shutdown()

    assert len(list((tmp_path / "diagnostics").glob("*.json"))) == 2


def test_pause_cancels_inflight_resume_request_before_returning(tmp_path) -> None:
    recognizer = BarrierRecognizer([SceneObservation()])
    engine, core, input_service, _window, _source = make_engine(
        tmp_path, recognizer=recognizer
    )
    engine.start(1)
    wait_until(lambda: core.snapshot.state is FishingState.WAIT_BITE)
    engine.pause("first pause")
    recognizer.observations.append(SceneObservation(ready=True))
    recognizer.block = True

    engine.resume()
    assert recognizer.entered.wait(timeout=1)
    engine.pause("second pause")
    event_count = len(input_service.events)
    recognizer.allow.set()
    assert recognizer.returned.wait(timeout=1)
    time.sleep(0.02)

    assert core.snapshot.state is FishingState.PAUSED
    assert input_service.events[-1] == "release"
    assert len(input_service.events) == event_count
    engine.shutdown()


def test_resume_aba_does_not_let_request_a_consume_request_b(tmp_path) -> None:
    recognizer = AbaRecognizer()
    engine, core, input_service, _window, _source = make_engine(
        tmp_path, recognizer=recognizer
    )
    engine.start(1)
    wait_until(lambda: core.snapshot.state is FishingState.WAIT_BITE)
    engine.pause("pause for request A")
    recognizer.block_a = True
    engine.resume()
    assert recognizer.a_entered.wait(timeout=1)

    engine.pause("invalidate request A")
    engine.resume()
    recognizer.allow_a.set()
    assert recognizer.a_returned.wait(timeout=1)
    assert recognizer.b_entered.wait(timeout=1)

    assert core.snapshot.state is FishingState.PAUSED
    assert input_service.events.count("F") == 1

    recognizer.allow_b.set()
    assert recognizer.b_returned.wait(timeout=1)
    wait_until(lambda: core.snapshot.state is FishingState.WAIT_RESULT)
    engine.shutdown()


def test_old_resume_cannot_publish_request_after_new_pause_returns(tmp_path) -> None:
    source = BlockingFrameSource(unblock_on_stop=False)
    recognizer = ScriptedRecognizer([SceneObservation(ready=True)])
    engine, core, _input, _window, _source = make_engine(
        tmp_path, frame_source=source, recognizer=recognizer
    )
    engine.start(1)
    assert source.entered.wait(timeout=1)
    core.pause("initial pause", time.monotonic())
    proxy = SnapshotBarrierCore(core)
    proxy.armed = True
    engine.core = proxy
    resume_thread = threading.Thread(target=engine.resume)
    resume_thread.start()
    assert proxy.snapshot_entered.wait(timeout=1)

    pause_returned = threading.Event()
    pause_thread = threading.Thread(
        target=lambda: (engine.pause("new pause"), pause_returned.set())
    )
    pause_thread.start()
    pause_returned.wait(timeout=0.05)
    proxy.allow_snapshot.set()
    resume_thread.join(timeout=1)
    pause_thread.join(timeout=1)
    source.unblock.set()
    time.sleep(0.05)

    assert core.snapshot.state is FishingState.PAUSED
    engine.shutdown()


def test_unexpected_core_failure_still_blocks_and_releases_input(tmp_path) -> None:
    progress = ProgressObservation(0.3, 0.7, 0.2, 1.0, 0.0)
    recognizer = ScriptedRecognizer(
        [
            SceneObservation(),
            SceneObservation(bite=True),
            SceneObservation(progress=progress),
            SceneObservation(progress=progress),
        ]
    )
    engine, core, input_service, _window, _source = make_engine(
        tmp_path, recognizer=recognizer
    )

    class ExplodingController:
        def decide(self, _observation: ProgressObservation) -> Direction:
            raise RuntimeError("controller failed")

    core.controller = ExplodingController()
    engine.start(1)
    wait_until(lambda: core.snapshot.state is FishingState.PAUSED)
    engine.shutdown()

    assert core.input_blocked is True
    assert input_service.events[-1] == "release"
    assert core.pause_code == "E_AUTOMATION"


def test_shutdown_is_idempotent_and_unblocks_worker_within_two_seconds(tmp_path) -> None:
    source = BlockingFrameSource(unblock_on_stop=True)
    engine, _core, input_service, _window, _source = make_engine(
        tmp_path, frame_source=source
    )
    engine.start(1)
    assert source.entered.wait(timeout=1)

    started = time.monotonic()
    engine.shutdown()
    engine.shutdown()
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert engine.is_running is False
    assert source.stop_calls >= 1
    assert input_service.events[-1] == "release"


def test_shutdown_total_budget_includes_blocking_stop_and_is_reused(tmp_path) -> None:
    source = BlockingStopFrameSource()
    engine, _core, _input, _window, _source = make_engine(
        tmp_path, frame_source=source
    )
    engine.start(1)
    wait_until(lambda: engine.is_running)

    started = time.monotonic()
    engine.shutdown()
    first_elapsed = time.monotonic() - started
    repeated = time.monotonic()
    engine.shutdown()
    repeated_elapsed = time.monotonic() - repeated
    source.allow_stop.set()

    assert source.stop_entered.is_set()
    assert first_elapsed < 2.1
    assert repeated_elapsed < 0.1
    assert source.stop_returned.wait(timeout=1)
    assert engine._cleanup_done.wait(timeout=1)


def test_shutdown_cannot_stop_before_inflight_capture_start_finishes(tmp_path) -> None:
    source = StartStopRaceFrameSource()
    engine, _core, _input, _window, _source = make_engine(
        tmp_path, frame_source=source
    )
    engine.start(1)
    assert source.start_entered.wait(timeout=1)
    shutdown_thread = threading.Thread(target=engine.shutdown)
    shutdown_thread.start()
    source.stop_entered.wait(timeout=0.1)
    source.allow_start.set()
    shutdown_thread.join(timeout=2.2)
    assert source.stop_entered.wait(timeout=1)
    wait_until(lambda: engine.is_running is False)

    assert source.active is False


def test_shutdown_release_failure_finishes_paused_with_input_error(tmp_path) -> None:
    input_service = ReleaseFailingInput(fail_release=False)
    engine, core, _input, _window, _source = make_engine(
        tmp_path, input_service=input_service
    )
    engine.start(1)
    wait_until(lambda: core.snapshot.state is FishingState.WAIT_BITE)
    input_service.fail_release = True

    engine.shutdown()

    assert core.snapshot.state is FishingState.PAUSED
    assert core.pause_code == "E_INPUT"
    assert "key release failed" in core.snapshot.error


def test_shutdown_budget_includes_blocking_release_all(tmp_path) -> None:
    input_service = ShutdownBlockingInput()
    input_service.allow_release.clear()
    engine, _core, _input, _window, _source = make_engine(
        tmp_path, input_service=input_service
    )
    engine.start(1)
    wait_until(lambda: "F" in input_service.events)

    started = time.monotonic()
    engine.shutdown()
    elapsed = time.monotonic() - started
    input_service.allow_release.set()

    assert elapsed < 2.1
    assert input_service.release_returned.wait(timeout=1)
    assert engine._cleanup_done.wait(timeout=1)


def test_shutdown_budget_includes_blocking_subscriber_callback(tmp_path) -> None:
    callback_entered = threading.Event()
    allow_callback = threading.Event()
    callback_returned = threading.Event()
    engine, _core, _input, _window, _source = make_engine(tmp_path)

    def subscriber(snapshot) -> None:
        if snapshot.state is FishingState.PAUSED:
            callback_entered.set()
            allow_callback.wait(timeout=2.2)
            callback_returned.set()

    engine.subscribe(subscriber)
    engine.start(1)

    started = time.monotonic()
    engine.shutdown()
    elapsed = time.monotonic() - started
    allow_callback.set()

    assert callback_entered.is_set()
    assert elapsed < 2.1
    assert callback_returned.wait(timeout=1)
    assert engine._cleanup_done.wait(timeout=1)


def test_start_ready_callback_does_not_hold_lifecycle_lock_from_shutdown(
    tmp_path,
) -> None:
    callback_entered = threading.Event()
    allow_callback = threading.Event()
    callback_returned = threading.Event()
    start_errors: list[BaseException] = []
    engine, _core, _input, _window, _source = make_engine(tmp_path)

    def subscriber(snapshot) -> None:
        if snapshot.state is FishingState.READY:
            callback_entered.set()
            allow_callback.wait(timeout=2.2)
            callback_returned.set()

    def start_engine() -> None:
        try:
            engine.start(1)
        except BaseException as error:
            start_errors.append(error)

    engine.subscribe(subscriber)
    start_thread = threading.Thread(target=start_engine)
    start_thread.start()
    assert callback_entered.wait(timeout=1)

    started = time.monotonic()
    engine.shutdown()
    elapsed = time.monotonic() - started
    repeated = time.monotonic()
    engine.shutdown()
    repeated_elapsed = time.monotonic() - repeated
    allow_callback.set()
    start_thread.join(timeout=1)

    assert elapsed < 2.1
    assert repeated_elapsed < 0.1
    assert callback_returned.wait(timeout=1)
    assert engine._cleanup_done.wait(timeout=1)
    assert start_thread.is_alive() is False
    assert len(start_errors) == 1
    assert "cancel" in str(start_errors[0]).lower()


def test_pause_during_ready_publish_explicitly_cancels_start_and_resume(
    tmp_path,
) -> None:
    callback_entered = threading.Event()
    allow_callback = threading.Event()
    start_errors: list[BaseException] = []
    engine, core, input_service, _window, source = make_engine(tmp_path)

    def subscriber(snapshot) -> None:
        if snapshot.state is FishingState.READY:
            callback_entered.set()
            assert allow_callback.wait(timeout=1)

    def start_engine() -> None:
        try:
            engine.start(1)
        except BaseException as error:
            start_errors.append(error)

    engine.subscribe(subscriber)
    start_thread = threading.Thread(target=start_engine)
    start_thread.start()
    assert callback_entered.wait(timeout=1)

    engine.pause("F8 pause during READY publish")
    allow_callback.set()
    start_thread.join(timeout=1)
    engine.resume()

    try:
        assert start_thread.is_alive() is False
        assert len(start_errors) == 1
        assert "cancel" in str(start_errors[0]).lower()
        assert core.snapshot.state is FishingState.PAUSED
        assert core.input_blocked is True
        assert engine._resume_request is None
        assert engine.is_running is False
        assert "F" not in input_service.events
        assert source.started == []
    finally:
        engine.shutdown()


def test_pause_after_start_allowed_latch_keeps_worker_resumable(tmp_path) -> None:
    source = StartStopRaceFrameSource()
    recognizer = ScriptedRecognizer()
    engine, core, input_service, _window, _source = make_engine(
        tmp_path,
        frame_source=source,
        recognizer=recognizer,
    )
    start_errors: list[BaseException] = []

    def start_engine() -> None:
        try:
            engine.start(1)
        except BaseException as error:
            start_errors.append(error)

    start_thread = threading.Thread(target=start_engine)
    start_thread.start()
    assert engine._start_decided.wait(timeout=1)
    assert engine._start_allowed is True
    assert source.start_entered.wait(timeout=1)
    start_thread.join(timeout=1)

    engine.pause("pause after allowed latch")
    recognizer.observations.append(SceneObservation(ready=True))
    engine.resume()
    source.allow_start.set()
    wait_until(lambda: input_service.events.count("F") == 1)

    try:
        assert start_errors == []
        assert core.snapshot.state is FishingState.WAIT_BITE
        assert engine.is_running is True
    finally:
        engine.shutdown()


def test_start_thread_construction_failure_rolls_back_ready_core(
    tmp_path, monkeypatch
) -> None:
    original_thread = threading.Thread
    engine, core, input_service, _window, _source = make_engine(tmp_path)

    def thread_factory(*args, **kwargs):
        if kwargs.get("name") == "auto-fishing-worker":
            raise RuntimeError("thread construction failed")
        return original_thread(*args, **kwargs)

    monkeypatch.setattr(threading, "Thread", thread_factory)

    with pytest.raises(RuntimeError, match="thread construction failed"):
        engine.start(1)

    assert core.snapshot.state is FishingState.PAUSED
    assert core.input_blocked is True
    assert input_service.events[-1] == "release"
    assert engine.is_running is False


def test_worker_start_failure_releases_both_startup_gates(
    tmp_path, monkeypatch
) -> None:
    original_thread = threading.Thread
    gates: dict[str, threading.Event] = {}
    engine, core, input_service, _window, source = make_engine(tmp_path)

    class FailingStartThread(original_thread):
        def start(self) -> None:
            raise RuntimeError("worker start failed")

    def thread_factory(*args, **kwargs):
        if kwargs.get("name") == "auto-fishing-worker":
            publish_done, start_decided = kwargs["args"]
            gates["publish_done"] = publish_done
            gates["start_decided"] = start_decided
            return FailingStartThread(*args, **kwargs)
        return original_thread(*args, **kwargs)

    monkeypatch.setattr(threading, "Thread", thread_factory)

    with pytest.raises(RuntimeError, match="worker start failed"):
        engine.start(1)

    assert gates["publish_done"].is_set()
    assert gates["start_decided"].is_set()
    assert engine._start_allowed is False
    assert engine._thread is None
    assert engine.is_running is False
    assert core.snapshot.state is FishingState.PAUSED
    assert core.input_blocked is True
    assert input_service.events[-1] == "release"
    assert source.started == []


def test_initial_publish_failure_releases_gates_and_rolls_back_worker(
    tmp_path, monkeypatch
) -> None:
    original_thread = threading.Thread
    gates: dict[str, threading.Event] = {}
    engine, core, input_service, _window, source = make_engine(tmp_path)
    original_publish = engine._publish
    publish_calls = 0

    def thread_factory(*args, **kwargs):
        if kwargs.get("name") == "auto-fishing-worker":
            publish_done, start_decided = kwargs["args"]
            gates["publish_done"] = publish_done
            gates["start_decided"] = start_decided
        return original_thread(*args, **kwargs)

    def failing_initial_publish() -> None:
        nonlocal publish_calls
        publish_calls += 1
        if publish_calls == 1:
            raise RuntimeError("initial publish failed")
        original_publish()

    monkeypatch.setattr(threading, "Thread", thread_factory)
    monkeypatch.setattr(engine, "_publish", failing_initial_publish)

    with pytest.raises(RuntimeError, match="initial publish failed"):
        engine.start(1)

    worker = engine._thread
    try:
        assert gates["publish_done"].is_set()
        assert gates["start_decided"].is_set()
        assert engine._start_allowed is False
        assert engine._thread is None
        assert engine.is_running is False
        assert core.snapshot.state is FishingState.PAUSED
        assert core.input_blocked is True
        assert input_service.events[-1] == "release"
        assert source.started == []
    finally:
        engine._start_allowed = False
        engine._start_decided.set()
        if worker is not None:
            worker.join(timeout=1)
        engine.shutdown()


def test_pause_during_pending_start_cancels_worker_with_latest_reason(
    tmp_path,
) -> None:
    engine, core, input_service, _window, source = make_engine(tmp_path)
    barrier_core = StartBarrierCore(core)
    engine.core = barrier_core
    start_errors: list[BaseException] = []

    def start_engine() -> None:
        try:
            engine.start(1)
        except BaseException as error:
            start_errors.append(error)

    start_thread = threading.Thread(target=start_engine)
    start_thread.start()
    assert barrier_core.start_entered.wait(timeout=1)

    engine.pause("first pending-start pause")
    engine.pause("F8 latest pending-start pause")
    barrier_core.allow_start.set()
    start_thread.join(timeout=1)
    time.sleep(0.05)

    try:
        assert start_thread.is_alive() is False
        assert len(start_errors) == 1
        assert "pause" in str(start_errors[0]).lower()
        assert core.snapshot.state is FishingState.PAUSED
        assert core.snapshot.error == "F8 latest pending-start pause"
        assert core.input_blocked is True
        assert input_service.events[-1] == "release"
        assert "F" not in input_service.events
        assert source.started == []
        assert engine.is_running is False
    finally:
        engine.shutdown()


def test_shutdown_budget_includes_core_process_holding_lock(tmp_path) -> None:
    input_service = ShutdownBlockingInput(block_tap=True)
    input_service.allow_release.set()
    engine, _core, _input, _window, _source = make_engine(
        tmp_path, input_service=input_service
    )
    engine.start(1)
    assert input_service.tap_entered.wait(timeout=1)

    started = time.monotonic()
    engine.shutdown()
    elapsed = time.monotonic() - started
    input_service.allow_tap.set()

    assert elapsed < 2.1
    assert input_service.tap_returned.wait(timeout=1)
    assert engine._cleanup_done.wait(timeout=1)


def test_shutdown_does_not_block_past_two_seconds_for_stuck_worker(tmp_path) -> None:
    source = BlockingFrameSource(unblock_on_stop=False)
    engine, _core, _input, _window, _source = make_engine(
        tmp_path, frame_source=source
    )
    engine.start(1)
    assert source.entered.wait(timeout=1)

    started = time.monotonic()
    engine.shutdown()
    elapsed = time.monotonic() - started
    source.unblock.set()
    wait_until(lambda: engine.is_running is False)

    assert 1.8 <= elapsed < 2.2
