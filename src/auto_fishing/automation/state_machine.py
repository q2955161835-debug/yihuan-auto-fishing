from __future__ import annotations

from enum import Enum, auto

from auto_fishing.model import FishingState, RuntimeSnapshot


class Event(Enum):
    CAST_SENT = auto()
    REEL_SENT = auto()
    BAR_DETECTED = auto()
    BAR_GONE = auto()
    RESULT_DETECTED = auto()
    RESULT_CLICKED = auto()
    READY_DETECTED = auto()
    INTERVAL_ELAPSED = auto()
    RESUME_CONTROL = auto()
    RESUME_READY = auto()


TRANSITIONS = {
    (FishingState.READY, Event.CAST_SENT): FishingState.WAIT_BITE,
    (FishingState.WAIT_BITE, Event.REEL_SENT): FishingState.WAIT_BAR,
    (FishingState.WAIT_BAR, Event.BAR_DETECTED): FishingState.CONTROL,
    (FishingState.CONTROL, Event.BAR_GONE): FishingState.WAIT_RESULT,
    (FishingState.WAIT_RESULT, Event.RESULT_DETECTED): FishingState.DISMISS_RESULT,
    (FishingState.DISMISS_RESULT, Event.RESULT_CLICKED): FishingState.DISMISS_RESULT,
    (FishingState.INTER_ROUND, Event.INTERVAL_ELAPSED): FishingState.READY,
    (FishingState.PAUSED, Event.RESUME_CONTROL): FishingState.CONTROL,
    (FishingState.PAUSED, Event.RESUME_READY): FishingState.READY,
}

TIMEOUTS = {
    FishingState.READY: 3.0,
    FishingState.WAIT_BITE: 120.0,
    FishingState.WAIT_BAR: 8.0,
    FishingState.CONTROL: 120.0,
    FishingState.WAIT_RESULT: 10.0,
    FishingState.DISMISS_RESULT: 8.0,
    FishingState.INTER_ROUND: 1.0,
}


class FishingStateMachine:
    def __init__(self) -> None:
        self.state = FishingState.UNBOUND
        self.target = 0
        self.completed = 0
        self.entered_at = 0.0
        self.pause_reason = ""
        self.paused_from: FishingState | None = None
        self.result_clicked = False

    def start(self, target: int, now: float) -> None:
        if not 1 <= target <= 999:
            raise ValueError("target must be between 1 and 999")

        self.state = FishingState.READY
        self.target = target
        self.completed = 0
        self.entered_at = now
        self.pause_reason = ""
        self.paused_from = None
        self.result_clicked = False

    def handle(self, event: Event, now: float) -> None:
        if (
            self.state is FishingState.DISMISS_RESULT
            and event is Event.READY_DETECTED
            and self.result_clicked
        ):
            self.completed += 1
            self.result_clicked = False
            self.state = (
                FishingState.COMPLETE
                if self.completed >= self.target
                else FishingState.INTER_ROUND
            )
            self.entered_at = now
            return

        next_state = TRANSITIONS.get((self.state, event))
        if next_state is None:
            raise ValueError(f"illegal event {event.name} in state {self.state.name}")

        if event is Event.RESULT_CLICKED:
            self.result_clicked = True
        elif event is Event.INTERVAL_ELAPSED or event is Event.RESUME_READY:
            self.result_clicked = False

        self.state = next_state
        self.entered_at = now

    def pause(self, reason: str, now: float) -> None:
        if self.state is not FishingState.PAUSED:
            self.paused_from = self.state
        self.state = FishingState.PAUSED
        self.pause_reason = reason
        self.entered_at = now

    def check_timeout(self, now: float) -> bool:
        timeout = TIMEOUTS.get(self.state)
        if timeout is None or now - self.entered_at <= timeout:
            return False

        self.pause(f"{self.state.value}超时", now)
        return True

    def check_interval(self, now: float) -> bool:
        return (
            self.state is FishingState.INTER_ROUND
            and now - self.entered_at >= TIMEOUTS[FishingState.INTER_ROUND]
        )

    def snapshot(self, fps: float = 0.0, error: str = "") -> RuntimeSnapshot:
        return RuntimeSnapshot(
            state=self.state,
            completed=self.completed,
            target=self.target,
            fps=fps,
            error=error,
        )
