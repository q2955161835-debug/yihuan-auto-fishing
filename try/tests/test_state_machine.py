import pytest

from auto_fishing.automation.state_machine import (
    TIMEOUTS,
    Event,
    FishingStateMachine,
)
from auto_fishing.model import FishingState, RuntimeSnapshot


ROUND_EVENTS = (
    Event.CAST_SENT,
    Event.REEL_SENT,
    Event.BAR_DETECTED,
    Event.BAR_GONE,
    Event.RESULT_DETECTED,
    Event.RESULT_CLICKED,
)


def advance_round_to_result_clicked(
    state_machine: FishingStateMachine,
    now: float,
) -> None:
    for event in ROUND_EVENTS:
        state_machine.handle(event, now)


def stateful_values(state_machine: FishingStateMachine) -> tuple[object, ...]:
    return (
        state_machine.state,
        state_machine.target,
        state_machine.completed,
        state_machine.entered_at,
        state_machine.pause_reason,
        state_machine.paused_from,
        state_machine.result_clicked,
    )


def reach_state(state: FishingState, now: float = 10.0) -> FishingStateMachine:
    state_machine = FishingStateMachine()
    state_machine.start(2, now)
    events_by_state = {
        FishingState.READY: (),
        FishingState.WAIT_BITE: (Event.CAST_SENT,),
        FishingState.WAIT_BAR: (Event.CAST_SENT, Event.REEL_SENT),
        FishingState.CONTROL: (
            Event.CAST_SENT,
            Event.REEL_SENT,
            Event.BAR_DETECTED,
        ),
        FishingState.WAIT_RESULT: (
            Event.CAST_SENT,
            Event.REEL_SENT,
            Event.BAR_DETECTED,
            Event.BAR_GONE,
        ),
        FishingState.DISMISS_RESULT: ROUND_EVENTS[:-1],
        FishingState.INTER_ROUND: (*ROUND_EVENTS, Event.READY_DETECTED),
    }
    for event in events_by_state[state]:
        state_machine.handle(event, now)
    assert state_machine.state is state
    return state_machine


def test_one_round_counts_only_after_result_clicked_and_ready_returns() -> None:
    state_machine = FishingStateMachine()
    state_machine.start(1, 0.0)
    for event, now in (
        (Event.CAST_SENT, 0.1),
        (Event.REEL_SENT, 2.0),
        (Event.BAR_DETECTED, 2.1),
        (Event.BAR_GONE, 4.0),
        (Event.RESULT_DETECTED, 4.1),
        (Event.RESULT_CLICKED, 4.2),
    ):
        state_machine.handle(event, now)

    assert state_machine.completed == 0
    assert state_machine.state is FishingState.DISMISS_RESULT

    state_machine.handle(Event.READY_DETECTED, 5.0)

    assert state_machine.completed == 1
    assert state_machine.state is FishingState.COMPLETE


def test_ready_before_result_click_is_illegal_and_does_not_count() -> None:
    state_machine = reach_state(FishingState.DISMISS_RESULT)
    before = stateful_values(state_machine)

    with pytest.raises(ValueError, match="READY_DETECTED"):
        state_machine.handle(Event.READY_DETECTED, 11.0)

    assert stateful_values(state_machine) == before
    assert state_machine.completed == 0


def test_duplicate_ready_does_not_repeat_success_count() -> None:
    state_machine = FishingStateMachine()
    state_machine.start(2, 0.0)
    advance_round_to_result_clicked(state_machine, 1.0)
    state_machine.handle(Event.READY_DETECTED, 1.1)
    before = stateful_values(state_machine)

    with pytest.raises(ValueError, match="READY_DETECTED"):
        state_machine.handle(Event.READY_DETECTED, 1.2)

    assert stateful_values(state_machine) == before
    assert state_machine.completed == 1


def test_inter_round_waits_one_second_before_returning_ready() -> None:
    state_machine = FishingStateMachine()
    state_machine.start(2, 0.0)
    advance_round_to_result_clicked(state_machine, 1.0)
    state_machine.handle(Event.READY_DETECTED, 2.0)

    assert state_machine.state is FishingState.INTER_ROUND
    assert state_machine.check_interval(2.999) is False
    assert state_machine.check_interval(3.0) is True
    assert state_machine.state is FishingState.INTER_ROUND

    state_machine.handle(Event.INTERVAL_ELAPSED, 3.0)

    assert state_machine.state is FishingState.READY
    assert state_machine.entered_at == 3.0


def test_early_inter_round_event_is_illegal_without_mutation() -> None:
    state_machine = FishingStateMachine()
    state_machine.start(2, 0.0)
    advance_round_to_result_clicked(state_machine, 1.0)
    state_machine.handle(Event.READY_DETECTED, 2.0)
    before = stateful_values(state_machine)

    with pytest.raises(ValueError, match="INTERVAL_ELAPSED"):
        state_machine.handle(Event.INTERVAL_ELAPSED, 2.999)

    assert stateful_values(state_machine) == before


@pytest.mark.parametrize("target", [1, 999])
def test_start_accepts_target_boundaries(target: int) -> None:
    state_machine = FishingStateMachine()

    state_machine.start(target, 12.5)

    assert state_machine.state is FishingState.READY
    assert state_machine.target == target
    assert state_machine.completed == 0
    assert state_machine.entered_at == 12.5


@pytest.mark.parametrize("target", [0, 1000])
def test_start_rejects_target_outside_boundaries_without_mutation(target: int) -> None:
    state_machine = FishingStateMachine()
    before = stateful_values(state_machine)

    with pytest.raises(ValueError, match="1.*999"):
        state_machine.start(target, 1.0)

    assert stateful_values(state_machine) == before


@pytest.mark.parametrize("target", [True, False, 1.0, 999.0])
def test_start_rejects_non_integer_targets_without_mutation(target: object) -> None:
    state_machine = FishingStateMachine()
    before = stateful_values(state_machine)

    with pytest.raises(ValueError, match="1.*999"):
        state_machine.start(target, 1.0)  # type: ignore[arg-type]

    assert stateful_values(state_machine) == before


def test_illegal_event_raises_without_changing_stateful_values() -> None:
    state_machine = FishingStateMachine()
    state_machine.start(1, 0.0)
    before = stateful_values(state_machine)

    with pytest.raises(ValueError, match="REEL_SENT"):
        state_machine.handle(Event.REEL_SENT, 1.0)

    assert stateful_values(state_machine) == before


@pytest.mark.parametrize("state", list(TIMEOUTS))
def test_each_timed_state_times_out_only_after_its_boundary(
    state: FishingState,
) -> None:
    state_machine = reach_state(state)
    timeout = TIMEOUTS[state]

    assert state_machine.check_timeout(10.0 + timeout) is False
    assert state_machine.state is state
    assert state_machine.check_timeout(10.0 + timeout + 0.001) is True
    assert state_machine.state is FishingState.PAUSED
    assert state_machine.pause_reason == f"{state.value}超时"
    assert state_machine.paused_from is state


@pytest.mark.parametrize(
    ("resume_event", "expected_state"),
    [
        (Event.RESUME_CONTROL, FishingState.CONTROL),
        (Event.RESUME_READY, FishingState.READY),
    ],
)
def test_pause_records_reason_and_original_state_then_resumes_by_classification(
    resume_event: Event,
    expected_state: FishingState,
) -> None:
    state_machine = reach_state(FishingState.CONTROL)

    state_machine.pause("F8", 12.0)

    assert state_machine.state is FishingState.PAUSED
    assert state_machine.pause_reason == "F8"
    assert state_machine.paused_from is FishingState.CONTROL
    assert state_machine.entered_at == 12.0

    state_machine.handle(resume_event, 13.0)

    assert state_machine.state is expected_state
    assert state_machine.entered_at == 13.0


@pytest.mark.parametrize("resume_event", [Event.RESUME_CONTROL, Event.RESUME_READY])
def test_pause_and_resume_classification_clear_result_click_marker(
    resume_event: Event,
) -> None:
    state_machine = FishingStateMachine()
    state_machine.start(1, 0.0)
    advance_round_to_result_clicked(state_machine, 1.0)
    assert state_machine.result_clicked is True

    state_machine.pause("F8", 2.0)

    assert state_machine.result_clicked is False

    state_machine.handle(resume_event, 3.0)

    assert state_machine.result_clicked is False


def test_old_result_click_cannot_count_a_later_unclicked_result() -> None:
    state_machine = FishingStateMachine()
    state_machine.start(1, 0.0)
    advance_round_to_result_clicked(state_machine, 1.0)
    state_machine.pause("F8", 2.0)
    state_machine.handle(Event.RESUME_CONTROL, 3.0)

    assert state_machine.state is FishingState.CONTROL
    assert state_machine.result_clicked is False

    state_machine.handle(Event.BAR_GONE, 4.0)
    assert state_machine.state is FishingState.WAIT_RESULT
    assert state_machine.result_clicked is False

    state_machine.handle(Event.RESULT_DETECTED, 5.0)
    before = stateful_values(state_machine)

    with pytest.raises(ValueError, match="READY_DETECTED"):
        state_machine.handle(Event.READY_DETECTED, 6.0)

    assert stateful_values(state_machine) == before
    assert state_machine.completed == 0


def test_timeout_check_is_inert_for_untimed_states() -> None:
    state_machine = FishingStateMachine()
    assert state_machine.check_timeout(1000.0) is False

    state_machine.start(1, 0.0)
    advance_round_to_result_clicked(state_machine, 1.0)
    state_machine.handle(Event.READY_DETECTED, 2.0)

    assert state_machine.state is FishingState.COMPLETE
    assert state_machine.check_timeout(1000.0) is False
    assert state_machine.state is FishingState.COMPLETE


def test_snapshot_contains_runtime_values_without_mutating_machine() -> None:
    state_machine = FishingStateMachine()
    state_machine.start(7, 2.0)
    before = stateful_values(state_machine)

    snapshot = state_machine.snapshot(fps=29.5, error="识别失败")

    assert snapshot == RuntimeSnapshot(
        state=FishingState.READY,
        completed=0,
        target=7,
        fps=29.5,
        error="识别失败",
    )
    assert stateful_values(state_machine) == before
