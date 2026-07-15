# Gold Settlement Cast Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent gold-quality settlement animation from consuming the next round's first F and recover safely when a cast is not accepted by the game.

**Architecture:** Keep result-click success as the completion boundary, extend only the post-result inter-round delay, and add a bounded cast-recovery guard inside `AutomationCore`. Reuse the existing three-frame `ready` observation only as a short-lived negative signal after a cast; do not add quality recognition or a second fishing state machine.

**Tech Stack:** Python 3.13, pytest 9.1.0, existing `FishingStateMachine`, `AutomationCore`, `SceneRecognizer`, and in-memory V2 diagnostic events.

## Global Constraints

- Result dismissal remains a single click scheduled 3.10–3.60 seconds after clean progress disappearance.
- A successful result click immediately increments the completed count; no result-card, quality, color, or ready-hook confirmation may become a completion dependency.
- Inter-round delay is exactly 3.5 seconds and applies only between completed rounds.
- Cast ready-guard window is inclusive from 1.5 through 4.0 seconds after each cast attempt.
- Recovery delay is exactly 0.8 seconds; allow two recovery casts and at most three total cast attempts per round.
- A fourth cast must never be sent; repeated failure pauses with `E_CAST` after releasing input.
- Existing foreground checks, F 80–180 ms input delay, progress entry gate, control algorithm, result click timing, and diagnostic retention remain unchanged.
- Diagnostic screenshots from the user's local ZIP must not be committed to the public repository.

---

### Task 1: Extend the inter-round guard

**Files:**
- Modify: `try/tests/test_state_machine.py:105-134`
- Modify: `try/tests/test_engine.py:1065-1079`
- Modify: `src/auto_fishing/automation/state_machine.py:33,78-83,116-120`

**Interfaces:**
- Consumes: `FishingStateMachine.check_interval(now: float) -> bool` and `Event.INTERVAL_ELAPSED`.
- Produces: an unchanged state-machine interface whose inter-round threshold is 3.5 seconds.

- [ ] **Step 1: Write the failing state-machine boundary test**

Replace the one-second test with:

```python
def test_inter_round_waits_three_point_five_seconds_before_returning_ready() -> None:
    state_machine = FishingStateMachine()
    state_machine.start(2, 0.0)
    advance_round_to_result_clicked(state_machine, 1.0)

    assert state_machine.state is FishingState.INTER_ROUND
    assert state_machine.check_interval(4.499) is False
    assert state_machine.check_interval(4.5) is True

    state_machine.handle(Event.INTERVAL_ELAPSED, 4.5)

    assert state_machine.state is FishingState.READY
    assert state_machine.entered_at == 4.5
```

Update the early-event test to call `INTERVAL_ELAPSED` at `4.499`.

- [ ] **Step 2: Write the failing engine timing test**

Update `test_inter_round_interval_precedes_generic_timeout` so processing at `4.499` remains `INTER_ROUND`, processing at `4.5` moves to `READY`, and a later fresh call sends exactly one F and reaches `WAIT_BITE`.

- [ ] **Step 3: Run the boundary tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest try/tests/test_state_machine.py::test_inter_round_waits_three_point_five_seconds_before_returning_ready try/tests/test_state_machine.py::test_early_inter_round_event_is_illegal_without_mutation try/tests/test_engine.py::test_inter_round_interval_precedes_generic_timeout -q
```

Expected: failures show the existing one-second interval reaches `READY` before 3.5 seconds.

- [ ] **Step 4: Implement the minimal interval change**

Change the centralized constant only:

```python
_INTER_ROUND_DELAY = 3.5
```

- [ ] **Step 5: Run the boundary tests and verify GREEN**

Run the command from Step 3. Expected: `3 passed`.

- [ ] **Step 6: Commit the isolated timing fix**

```powershell
git add -- src/auto_fishing/automation/state_machine.py try/tests/test_state_machine.py try/tests/test_engine.py
git commit -m "fix: wait for gold settlement before next cast"
```

### Task 2: Recover an unaccepted cast without advancing state

**Files:**
- Modify: `try/tests/test_engine.py` near the existing inter-round and second-F tests
- Modify: `src/auto_fishing/automation/engine.py:32-38,76-126,176-214,259-283,461-477`

**Interfaces:**
- Consumes: `SceneObservation.ready`, `SceneObservation.bite`, `FramePacket`, `input_service.tap_f()`, `scene_recognizer.set_bite_baseline(frame)`, and `AutomationCore._record(name, **fields)`.
- Produces: private helpers `_reset_cast_recovery()`, `_send_cast(packet, now, trigger)`, `_wait_for_bite(observation, packet, now)` and diagnostic events `cast.attempt`, `cast.recovery_scheduled`, `cast.recovery_exhausted`.

- [ ] **Step 1: Write the failing recovery sequence test**

Add a packet helper and a test that reproduces the diagnostic order:

```python
def frame_packet(timestamp: float) -> FramePacket:
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    return FramePacket(frame, timestamp, 30.0)


def test_ready_guard_recasts_without_treating_false_bite_as_reel() -> None:
    runtime_log = RecordingRuntimeLog()
    core, input_service, _state_machine = make_core(event_recorder=runtime_log)
    core.start(1, 0.0)

    core.process(SceneObservation(), frame_packet(0.1), 0.1, CLIENT)
    core.process(
        SceneObservation(ready=True, bite=True),
        frame_packet(1.6),
        1.6,
        CLIENT,
    )
    core.process(SceneObservation(bite=True), frame_packet(2.39), 2.39, CLIENT)

    assert input_service.events.count("F") == 1
    assert core.snapshot.state is FishingState.WAIT_BITE

    core.process(SceneObservation(), frame_packet(2.4), 2.4, CLIENT)

    assert input_service.events.count("F") == 2
    assert core.snapshot.state is FishingState.WAIT_BITE

    core.process(SceneObservation(bite=True), frame_packet(4.0), 4.0, CLIENT)

    assert input_service.events.count("F") == 3
    assert core.snapshot.state is FishingState.WAIT_BAR
    assert [event["event"] for event in runtime_log.events] == [
        "cast.attempt",
        "cast.recovery_scheduled",
        "cast.attempt",
    ]
```

- [ ] **Step 2: Run the recovery test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest try/tests/test_engine.py::test_ready_guard_recasts_without_treating_false_bite_as_reel -q
```

Expected: the second F is treated as a reel immediately and the state becomes `WAIT_BAR` before recovery.

- [ ] **Step 3: Implement the minimal recovery guard**

Add centralized constants:

```python
_CAST_READY_GUARD_MIN = 1.50
_CAST_READY_GUARD_MAX = 4.00
_CAST_RECOVERY_DELAY = 0.80
_CAST_MAX_ATTEMPTS = 3
```

Initialize `_cast_attempts`, `_cast_sent_at`, and `_cast_recovery_at`. Replace the READY and WAIT_BITE branches with `_send_cast(..., trigger="round_start")` and `_wait_for_bite(...)`. `_wait_for_bite` must prioritize an existing recovery plan, then `ready` inside the guard window, then the existing `bite` reel path. `_send_cast` must rebuild the bite baseline, increment attempts, clear the pending recovery, and emit:

```python
self._record(
    "cast.attempt",
    attempt=self._cast_attempts,
    trigger=trigger,
    sent_at=now,
    verification_starts_at=now + _CAST_READY_GUARD_MIN,
    verification_ends_at=now + _CAST_READY_GUARD_MAX,
)
```

When scheduling recovery, reset the current bite baseline from the packet, set `now + 0.8`, and emit `cast.recovery_scheduled` with the failed and next attempt numbers, delay, scheduled time, elapsed time, and `ambiguous_bite`.

- [ ] **Step 4: Run the recovery test and verify GREEN**

Run the command from Step 2. Expected: `1 passed`.

- [ ] **Step 5: Write the failing recovery exhaustion test**

Add a test that sends attempt 1 at 0.1, schedules/sends attempt 2 at 1.6/2.4, schedules/sends attempt 3 at 4.0/4.8, then provides `ready=True` at 6.4. Assert exactly three F inputs, `PAUSED`, `pause_code == "E_CAST"`, final input event `release`, and one `cast.recovery_exhausted` event.

- [ ] **Step 6: Run the exhaustion test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest try/tests/test_engine.py::test_ready_guard_pauses_after_three_unaccepted_casts -q
```

Expected: current recovery implementation schedules or sends a fourth cast instead of pausing.

- [ ] **Step 7: Implement bounded exhaustion handling**

Before scheduling recovery, if `_cast_attempts >= _CAST_MAX_ATTEMPTS`, emit:

```python
self._record(
    "cast.recovery_exhausted",
    attempts=self._cast_attempts,
    elapsed_since_cast=elapsed,
    ready=observation.ready,
    bite=observation.bite,
)
self.pause(
    "连续三次抛竿均未被游戏接受",
    now,
    code="E_CAST",
)
```

Return without sending another F.

- [ ] **Step 8: Run both recovery tests and verify GREEN**

Run both Task 2 tests. Expected: `2 passed`.

- [ ] **Step 9: Add lifecycle and upper-window regression tests**

Add tests proving a paused pending recovery is discarded by `restart_round`, and `ready=True` after 4.0 seconds does not schedule recovery or block a simultaneous genuine `bite`. Run each new test before implementation to verify the expected failure, then add `_reset_cast_recovery()` calls to start/restart/cancel/result-click boundaries as required.

- [ ] **Step 10: Run engine and state-machine suites**

```powershell
.\.venv\Scripts\python.exe -m pytest try/tests/test_state_machine.py try/tests/test_engine.py -q
```

Expected: all tests pass with no warnings or errors.

- [ ] **Step 11: Commit the recovery guard**

```powershell
git add -- src/auto_fishing/automation/engine.py try/tests/test_engine.py
git commit -m "fix: recover casts ignored after settlement"
```

### Task 3: Synchronize project rules and acceptance evidence

**Files:**
- Modify: `AGENTS.md`
- Modify: `doc/验收标准.md`
- Modify: `doc/进展记录/2026-7-15.md`

**Interfaces:**
- Consumes: final constants, event names, test counts, and command output from Tasks 1–2.
- Produces: current long-term project rules, reproducible acceptance steps, and a timestamped implementation/error report.

- [ ] **Step 1: Update long-term rules**

Change the main data flow from a 1-second to a 3.5-second inter-round wait. Document the 1.5–4.0 second ready guard, 0.8-second recovery delay, maximum three cast attempts, `E_CAST`, and the rule that ready remains a cast-safety veto rather than a completion dependency.

- [ ] **Step 2: Add acceptance steps and evidence fields**

Record commands for the state-machine/engine suite and full suite. Add artificial sequence acceptance for ignored cast recovery and mark ordinary/gold-quality real-game runs as manual confirmation until performed.

- [ ] **Step 3: Update today's progress record**

Append one local-time range with the diagnostic ZIP name, problem-cause-solution, modified file list, test commands/results, manual items, and technical-debt review. State explicitly that local diagnostic files under `%LOCALAPPDATA%` were read only and no screenshots were committed.

- [ ] **Step 4: Run documentation consistency checks**

```powershell
rg -n "轮间等待 1 秒|轮间等待1秒|_INTER_ROUND_DELAY = 1\.0" AGENTS.md doc docs src try/tests
git diff --check
```

Expected: no stale production rule remains outside historical records; `git diff --check` exits 0.

### Task 4: Full verification and branch integration

**Files:**
- Verify all files changed by Tasks 1–3.

**Interfaces:**
- Consumes: complete implementation and documentation.
- Produces: tested commits merged into local `main`, with the task branch removed and no dirty workspace.

- [ ] **Step 1: Run focused regression tests fresh**

```powershell
.\.venv\Scripts\python.exe -m pytest try/tests/test_state_machine.py try/tests/test_engine.py -q
```

Expected: zero failures.

- [ ] **Step 2: Run the complete test suite fresh**

```powershell
.\.venv\Scripts\python.exe -m pytest try/tests -q
```

Expected: zero failures and a new baseline count recorded in `AGENTS.md` and `doc/验收标准.md`.

- [ ] **Step 3: Inspect the final diff and duplication risk**

```powershell
git diff --check
git status --short
git diff main...HEAD --stat
git diff main...HEAD -- src/auto_fishing/automation/state_machine.py src/auto_fishing/automation/engine.py try/tests/test_state_machine.py try/tests/test_engine.py AGENTS.md doc/验收标准.md doc/进展记录/2026-7-15.md
```

Confirm no quality-specific branch, result-ready completion dependency, duplicate state machine, unrelated refactor, temporary patch, secret, or diagnostic screenshot was introduced.

- [ ] **Step 4: Commit final documentation and verified baseline**

```powershell
git add -- AGENTS.md doc/验收标准.md doc/进展记录/2026-7-15.md docs/superpowers/plans/2026-07-15-gold-settlement-cast-guard.md
git commit -m "docs: record gold settlement cast guard"
```

- [ ] **Step 5: Merge into main and remove the task branch**

Switch to `main`, merge `codex/fix-gold-settlement-cast-guard` with a non-fast-forward merge, rerun the focused suite from `main`, then delete the task branch. Do not push; ask the user whether to push the local main branch to the public GitHub repository.
