# Progress Completion Transition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transition from progress control to result waiting when a well-established progress slot disappears cleanly for three consecutive frames.

**Architecture:** `AutomationCore` will count reliable progress frames and interpret only a fully empty slot after at least 15 valid frames as completion. Existing `BAR_GONE`, `WAIT_RESULT`, result recognition, release behavior, and failure timeout remain unchanged.

**Tech Stack:** Python 3.13, pytest 9.1.0, existing state machine and structured `SceneObservation` diagnostics.

## Global Constraints

- Completion requires at least 15 reliable progress frames.
- Completion requires three consecutive frames with no observation, zero scanlines, zero candidates, and `yellow_missing`.
- One or two missing frames release A/D and remain in control.
- Early loss or ambiguous candidates still pause after six missing frames.
- Completion sends no F and no click; it only releases A/D and emits `BAR_GONE`.
- Do not merge `main` before the real result, dismissal, and count increment pass.

---

### Task 1: Add stable-control completion semantics

**Files:**
- Modify: `src/auto_fishing/automation/engine.py:48-290`
- Modify: `try/tests/test_engine.py:780-880`

**Interfaces:**
- Consumes: `SceneObservation.progress`, `progress_scanlines`, `progress_candidates`, and `progress_rejection`.
- Produces: existing state transition `FishingState.CONTROL -> Event.BAR_GONE -> FishingState.WAIT_RESULT`.
- Adds internal counter: `AutomationCore.bar_valid_frames: int`.

- [ ] **Step 1: Write failing transition tests**

```python
def clean_progress_disappearance() -> SceneObservation:
    return SceneObservation(
        progress_scanlines=0,
        progress_candidates=0,
        progress_rejection="yellow_missing",
    )


def test_stable_control_then_three_clean_missing_frames_waits_result() -> None:
    core, input_service, _state_machine = make_core(state=FishingState.CONTROL)
    progress = ProgressObservation(0.3, 0.7, 0.5, 1.0, 0.0)
    for index in range(15):
        core.process(SceneObservation(progress=progress), None, 0.1 + index / 30, CLIENT)
    for index in range(3):
        core.process(clean_progress_disappearance(), None, 1.0 + index / 30, CLIENT)
    assert core.snapshot.state is FishingState.WAIT_RESULT
    assert input_service.events[-1] == "release"
    assert "F" not in input_service.events


def test_two_clean_missing_frames_then_recovery_stays_control() -> None:
    core, _input_service, _state_machine = make_core(state=FishingState.CONTROL)
    progress = ProgressObservation(0.3, 0.7, 0.5, 1.0, 0.0)
    for index in range(15):
        core.process(SceneObservation(progress=progress), None, 0.1 + index / 30, CLIENT)
    core.process(clean_progress_disappearance(), None, 1.0, CLIENT)
    core.process(clean_progress_disappearance(), None, 1.1, CLIENT)
    core.process(SceneObservation(progress=progress), None, 1.2, CLIENT)
    assert core.snapshot.state is FishingState.CONTROL


@pytest.mark.parametrize(
    "missing",
    [
        clean_progress_disappearance(),
        SceneObservation(
            progress_scanlines=2,
            progress_candidates=3,
            progress_rejection="no_consensus",
        ),
    ],
)
def test_early_or_ambiguous_loss_still_pauses(missing: SceneObservation) -> None:
    core, _input_service, _state_machine = make_core(state=FishingState.CONTROL)
    progress = ProgressObservation(0.3, 0.7, 0.5, 1.0, 0.0)
    for index in range(14):
        core.process(SceneObservation(progress=progress), None, 0.1 + index / 30, CLIENT)
    for index in range(6):
        core.process(missing, None, 1.0 + index / 30, CLIENT)
    assert core.snapshot.state is FishingState.PAUSED
    assert core.pause_code == "E_PROGRESS_LOST"
```

- [ ] **Step 2: Verify the tests fail for the intended reason**

Run:

```powershell
& 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe' -m pytest try/tests/test_engine.py -q -k "stable_control or clean_missing or ambiguous_loss"
```

Expected: the stable completion test reaches `PAUSED` rather than `WAIT_RESULT`.

- [ ] **Step 3: Implement the minimum core change**

Initialize and reset `bar_valid_frames` beside `bar_missing_frames` in `__init__`, `start`, `resume`, and `cancel_current`. Increment it only for a reliable progress observation. After the existing result-candidate branch and before the six-frame pause, add:

```python
clean_disappearance = (
    observation.progress_scanlines == 0
    and observation.progress_candidates == 0
    and observation.progress_rejection == "yellow_missing"
)
if self.bar_valid_frames >= 15 and self.bar_missing_frames >= 3 and clean_disappearance:
    self.bar_valid_frames = 0
    self.state_machine.handle(Event.BAR_GONE, now)
    return
```

The existing missing-frame `release_all()` must run before this transition. Reset the counter when the result-candidate path also emits `BAR_GONE`.

- [ ] **Step 4: Run focused and full tests**

```powershell
& 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe' -m pytest try/tests/test_engine.py -q
& 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe' -m pytest try/tests -q
```

Expected: all tests PASS and no completion path records a third F.

- [ ] **Step 5: Commit Task 1**

```powershell
git add src/auto_fishing/automation/engine.py try/tests/test_engine.py
git commit -m "fix: transition after stable progress disappears"
```

---

### Task 2: Document, rebuild, and resume real acceptance

**Files:**
- Modify: `AGENTS.md`
- Modify: `doc/验收标准.md`
- Modify: `doc/进展记录/2026-7-12.md`
- Generated/ignored: `dist/异环自动钓鱼.exe`

- [ ] **Step 1: Record evidence and the long-term rule**

Document the 15-valid/3-clean-missing transition, absence of an extra F, exact test count, and remaining result/dismiss/count real gate.

- [ ] **Step 2: Back up and rebuild**

Create a backup directory under `D:\0文件夹\备份` with prefix `异环自动钓鱼-progress-completion-prebuild-` and `Get-Date -Format 'yyyyMMdd-HHmmss'`, copy the current executable, record SHA256, then run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1 -PythonPath 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe'
```

Expected: full tests PASS and `RELEASE_MANIFEST_OK requireAdministrator uiAccess=false`.

- [ ] **Step 3: Start and inspect the real run**

```powershell
Start-Process -FilePath (Resolve-Path 'dist\异环自动钓鱼.exe').Path
```

Expected: `CONTROL -> WAIT_RESULT` after the clean transition, with no third F. Continue inspecting result recognition, dismissal, and completion count; do not merge `main` until all three pass.
