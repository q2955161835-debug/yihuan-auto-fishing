from __future__ import annotations

from enum import Enum, auto

from auto_fishing.model import FishingState, RuntimeSnapshot


class Event(Enum):
    CAST_SENT = auto()
    REEL_SENT = auto()
    BAR_DETECTED = auto()
    BAR_GONE = auto()
    RESULT_CLICKED = auto()
    INTERVAL_ELAPSED = auto()


TRANSITIONS = {
    (FishingState.READY, Event.CAST_SENT): FishingState.WAIT_BITE,
    (FishingState.WAIT_BITE, Event.REEL_SENT): FishingState.WAIT_BAR,
    (FishingState.WAIT_BAR, Event.BAR_DETECTED): FishingState.CONTROL,
    (FishingState.CONTROL, Event.BAR_GONE): FishingState.WAIT_RESULT,
    (FishingState.INTER_ROUND, Event.INTERVAL_ELAPSED): FishingState.READY,
}

TIMEOUTS = {
    FishingState.READY: 3.0,
    FishingState.WAIT_BITE: 120.0,
    FishingState.WAIT_BAR: 8.0,
    FishingState.CONTROL: 120.0,
    FishingState.WAIT_RESULT: 10.0,
}

_INTER_ROUND_DELAY = 3.5


class FishingStateMachine:
    def __init__(self) -> None:
        self.state = FishingState.UNBOUND
        self.target = 0
        self.completed = 0
        self.entered_at = 0.0
        self.pause_reason = ""
        self.paused_from: FishingState | None = None

    def start(self, target: int, now: float) -> None:
        if type(target) is not int or not 1 <= target <= 999:
            raise ValueError("target must be an integer between 1 and 999")

        self.state = FishingState.READY
        self.target = target
        self.completed = 0
        self.entered_at = now
        self.pause_reason = ""
        self.paused_from = None

    def cancel_current(self, now: float) -> None:
        self.state = FishingState.UNBOUND
        self.target = 0
        self.completed = 0
        self.entered_at = now
        self.pause_reason = ""
        self.paused_from = None

    def handle(self, event: Event, now: float) -> None:
        if (
            self.state is FishingState.WAIT_RESULT
            and event is Event.RESULT_CLICKED
        ):
            self.completed += 1
            self.state = (
                FishingState.COMPLETE
                if self.completed >= self.target
                else FishingState.INTER_ROUND
            )
            self.entered_at = now
            return

        if (
            self.state is FishingState.INTER_ROUND
            and event is Event.INTERVAL_ELAPSED
            and now - self.entered_at < _INTER_ROUND_DELAY
        ):
            raise ValueError(f"illegal event {event.name} before interval elapsed")

        next_state = TRANSITIONS.get((self.state, event))
        if next_state is None:
            raise ValueError(f"illegal event {event.name} in state {self.state.name}")

        self.state = next_state
        self.entered_at = now

    def pause(self, reason: str, now: float) -> None:
        if self.state is not FishingState.PAUSED:
            self.paused_from = self.state
        self.state = FishingState.PAUSED
        self.pause_reason = reason
        self.entered_at = now

    def restart_round(self, now: float) -> bool:
        if self.state is not FishingState.PAUSED:
            return False
        self.state = FishingState.READY
        self.entered_at = now
        self.pause_reason = ""
        self.paused_from = None
        return True

    def check_timeout(self, now: float) -> bool:
        timeout = TIMEOUTS.get(self.state)
        if timeout is None or now - self.entered_at <= timeout:
            return False

        self.pause(f"{self.state.value}超时", now)
        return True

    def check_interval(self, now: float) -> bool:
        return (
            self.state is FishingState.INTER_ROUND
            and now - self.entered_at >= _INTER_ROUND_DELAY
        )

    def snapshot(self, fps: float = 0.0, error: str = "") -> RuntimeSnapshot:
        return RuntimeSnapshot(
            state=self.state,
            completed=self.completed,
            target=self.target,
            fps=fps,
            error=error,
        )
