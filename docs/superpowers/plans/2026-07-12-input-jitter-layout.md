# Input Jitter and Window Spacing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a bounded cancellable pre-press delay to F taps while keeping A/D long presses immediate, and widen the control window so right-side text is not clipped.

**Architecture:** `SafeInput` owns the delay policy because every F tap passes through it. A monotonic cancellation generation is advanced by `release_all`; a pending F tap snapshots that generation, waits through an injectable function, and only presses F if the generation is unchanged. The Tk view owns its fixed minimum width and grid spacing, requiring no changes to settings persistence.

**Tech Stack:** Python 3.13, Tkinter/ttk, pytest, PyInstaller.

## Global Constraints

- F pre-press delay is uniformly sampled from 80 to 180 milliseconds.
- A/D press, switch and release receive no new delay.
- A pause, F8, shutdown or release during the F delay must prevent the delayed F press.
- Do not use the delay to evade game protection; do not change vision thresholds or A/D control decisions.
- Keep standard frame logs low-resolution; retain existing original-frame diagnostics only for failures.

---

### Task 1: Cancellable F pre-press delay

**Files:**
- Modify: `src/auto_fishing/platform/input.py:150-240`
- Test: `try/tests/test_safe_input.py`

**Interfaces:**
- Consumes: `InputBackend.key_down(key: str) -> None`, `InputBackend.key_up(key: str) -> None`.
- Produces: `SafeInput.tap_f() -> None`, `SafeInput.release_all() -> None`; constructor accepts `random_uniform: Callable[[float, float], float]`.

- [ ] **Step 1: Write failing tests for bounded F delay and unchanged A/D**

```python
def test_tap_f_waits_for_bounded_pre_press_delay() -> None:
    backend = FakeBackend()
    waits: list[float] = []
    safe = SafeInput(
        backend,
        sleep=waits.append,
        random_uniform=lambda lower, upper: upper,
    )

    safe.tap_f()

    assert waits == [0.18, 0.05]
    assert backend.events == [("down", "F"), ("up", "F")]


def test_direction_changes_do_not_wait_for_f_jitter() -> None:
    backend = FakeBackend()
    waits: list[float] = []
    safe = SafeInput(backend, sleep=waits.append)

    safe.set_direction(Direction.LEFT)
    safe.set_direction(Direction.RIGHT)

    assert waits == []
    assert backend.events == [("down", "A"), ("up", "A"), ("down", "D")]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe -m pytest try/tests/test_safe_input.py -q`

Expected: FAIL because `SafeInput.__init__` does not accept `random_uniform` and F only waits 50 milliseconds after pressing.

- [ ] **Step 3: Implement minimal delay policy**

```python
from random import uniform as real_uniform

class SafeInput:
    def __init__(self, backend, sleep=real_sleep, random_uniform=real_uniform, recorder=None):
        self.random_uniform = random_uniform
        self._cancel_generation = 0

    def tap_f(self) -> None:
        self._record("input.request", action="tap", key="F")
        generation = self._cancel_generation
        delay = self.random_uniform(0.08, 0.18)
        self._record("input.delay", key="F", seconds=delay)
        self.sleep(delay)
        if generation != self._cancel_generation:
            self._record("input.cancelled", action="tap", key="F")
            return
        self._down("F")
        try:
            self.sleep(0.05)
        finally:
            self._up("F")

    def release_all(self) -> None:
        self._cancel_generation += 1
        # retain the existing key and mouse release loop unchanged
```

Validate the sampled value is finite and clamp it to `[0.08, 0.18]` before waiting, so an injected bad random source cannot produce a negative or excessive delay.

- [ ] **Step 4: Add cancellation test and run focused tests**

```python
def test_release_all_cancels_f_before_its_delayed_press() -> None:
    backend = FakeBackend()
    waits: list[float] = []
    safe: SafeInput

    def interrupting_sleep(seconds: float) -> None:
        waits.append(seconds)
        safe.release_all()

    safe = SafeInput(
        backend,
        sleep=interrupting_sleep,
        random_uniform=lambda _lower, _upper: 0.10,
    )
    safe.tap_f()

    assert waits == [0.10]
    assert backend.events == []
```

Run: `C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe -m pytest try/tests/test_safe_input.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/auto_fishing/platform/input.py try/tests/test_safe_input.py
git commit -m "feat: add cancellable F input jitter"
```

### Task 2: Widen and space the control window

**Files:**
- Modify: `src/auto_fishing/ui/main_window.py:25-122`
- Test: `try/tests/test_ui_smoke.py`

**Interfaces:**
- Consumes: `MainWindow(root, controller, settings_store)` and persisted `(window_x, window_y)` settings.
- Produces: a 400×240 initial/minimum control window, with existing controls and callbacks unchanged.

- [ ] **Step 1: Write failing geometry test**

```python
def test_window_geometry_leaves_room_for_right_side_status(root) -> None:
    controller = FakeController()
    requested: list[str] = []
    original_geometry = root.geometry
    root.geometry = lambda value: requested.append(value)  # type: ignore[method-assign]
    try:
        MainWindow(root, controller, FakeSettings())
    finally:
        root.geometry = original_geometry  # type: ignore[method-assign]

    assert requested == ["400x240+12+34"]
```

- [ ] **Step 2: Run test to verify failure**

Run: `C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe -m pytest try/tests/test_ui_smoke.py::test_window_geometry_leaves_room_for_right_side_status -q`

Expected: FAIL because the current width is 320 pixels.

- [ ] **Step 3: Implement layout constants and right-side spacing**

```python
_WINDOW_WIDTH = 400
_WINDOW_HEIGHT = 240

root.geometry(f"{_WINDOW_WIDTH}x{_WINDOW_HEIGHT}{x:+d}{y:+d}")
root.minsize(_WINDOW_WIDTH, _WINDOW_HEIGHT)
content.columnconfigure(2, minsize=52)
ttk.Label(content, text="阶段：").grid(..., padx=(12, 4))
ttk.Label(content, text="帧率：").grid(..., padx=(12, 4))
```

Keep the existing four-column structure, button rows, state variables and saved position behavior unchanged.

- [ ] **Step 4: Run UI tests**

Run: `C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe -m pytest try/tests/test_ui_smoke.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/auto_fishing/ui/main_window.py try/tests/test_ui_smoke.py
git commit -m "fix: widen control window status layout"
```

### Task 3: Verify release and record result

**Files:**
- Modify: `AGENTS.md`
- Modify: `doc/验收标准.md`
- Modify: `doc/进展记录/2026-7-12.md`

**Interfaces:**
- Consumes: completed Tasks 1 and 2.
- Produces: documented timing bounds, UI width and release evidence.

- [ ] **Step 1: Run the full automated suite**

Run: `C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe -m pytest try/tests -q`

Expected: PASS with all tests green.

- [ ] **Step 2: Back up and build the single-file release**

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = "D:\0文件夹\备份\异环自动钓鱼-input-jitter-prebuild-$stamp"
New-Item -ItemType Directory -Path $backup -Force | Out-Null
Copy-Item dist\异环自动钓鱼.exe (Join-Path $backup '异环自动钓鱼.exe')
powershell -ExecutionPolicy Bypass -File scripts/build.ps1 -PythonPath 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe'
```

Expected: build gate reports all tests passed and `RELEASE_MANIFEST_OK requireAdministrator uiAccess=false`.

- [ ] **Step 3: Record test/build evidence and manual verification checklist**

Document the F delay range, F8 cancellation test, A/D no-delay test, 400-pixel window width, backup path, release hash, and the remaining manual checks: F visibly delays briefly, A/D remains continuous, and the right-side stage/FPS text is fully visible.

- [ ] **Step 4: Commit documentation**

```powershell
git add AGENTS.md doc/验收标准.md doc/进展记录/2026-7-12.md
git commit -m "docs: record input jitter release evidence"
```
