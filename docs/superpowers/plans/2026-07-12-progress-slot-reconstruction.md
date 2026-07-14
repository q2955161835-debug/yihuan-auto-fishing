# Progress Slot Reconstruction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-connected-component progress recognizer with a fixed-slot, five-scanline reconstruction that remains valid when the yellow marker splits the green bar.

**Architecture:** `ProgressRecognizer` will analyze a fixed vertical band inside `TOP_ROI`, reconstruct logical green intervals around the yellow marker, combine five scanlines by median consensus, and use short temporal history only to reject jumps. `SceneObservation` will carry diagnostics without coupling them to the controller. `AutomationEngine` will retain a bounded in-memory progress-strip history and persist it only when progress recognition fails.

**Tech Stack:** Python 3.13, NumPy 2.4.1, OpenCV 4.13.0.92, pytest 9.1.0, Tk/Win32 integration unchanged.

## Global Constraints

- Keep screen-only automation: no game-memory reads, injection, or anti-cheat bypass.
- Keep `TOP_ROI`; scan only local vertical 40%–52%, with the outer 8% excluded on each horizontal side.
- Use five equally spaced scanlines and require at least three agreeing lines.
- Missing current-frame observation immediately releases A/D; six consecutive misses still pause with `E_PROGRESS_LOST`.
- Temporal history may select or reject candidates but must never drive A/D without a valid current frame.
- Do not modify bite, result, round counting, F delay, OSK coordinates, or window binding behavior.
- All test-only fixtures and utilities live under `try/`.
- Do not merge `main` until a real complete fishing round passes.

---

### Task 1: Reconstruct split green bars with five scanlines

**Files:**
- Modify: `src/auto_fishing/vision/progress.py:1-107`
- Modify: `try/tests/test_progress.py`
- Create: `try/fixtures/progress/progress_split_marker.png`
- Create: `try/tools/extract_progress_fixture.py`

**Interfaces:**
- Consumes: BGR `np.ndarray` cropped to `TOP_ROI`, plus a monotonic timestamp.
- Produces: `ProgressRecognizer.detect(image: np.ndarray, timestamp: float) -> ProgressObservation | None` with unchanged normalized-coordinate semantics.
- Internal helpers: `_runs()`, `_line_candidates()`, and `_consensus()`; `_consensus()` returns one `_LineCandidate` plus the number of agreeing scanlines, or `None`.

- [ ] **Step 1: Add the portable real-frame fixture extractor**

Create `try/tools/extract_progress_fixture.py` with an explicit source and output interface:

```python
from pathlib import Path
import sys

import cv2
import numpy as np


def main(source: Path, output: Path) -> None:
    frame = cv2.imdecode(np.fromfile(source, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise SystemExit(f"无法读取原始诊断帧: {source}")
    height, width = frame.shape[:2]
    top = frame[0 : round(height * 0.15), round(width * 0.24) : round(width * 0.76)]
    output.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".png", top)
    if not ok:
        raise SystemExit("测试夹具编码失败")
    encoded.tofile(output)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
```

Run:

```powershell
& 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe' try\tools\extract_progress_fixture.py `
  "$env:LOCALAPPDATA\异环自动钓鱼\diagnostics\20260712T094329800554Z_E_PROGRESS_LOST.png" `
  "try\fixtures\progress\progress_split_marker.png"
```

Expected: a top-strip PNG containing the real green bar split by the yellow marker, with no character, UID, or OSK content; the right-side 8% exclusion prevents the partial control window from becoming a candidate.

- [ ] **Step 2: Write failing split-bar and sweep tests**

Add tests that expose the current connected-component failure:

```python
from pathlib import Path


def test_real_split_marker_fixture_reconstructs_full_green_interval() -> None:
    fixture = Path("try/fixtures/progress/progress_split_marker.png")
    image = cv2.imdecode(np.fromfile(fixture, dtype=np.uint8), cv2.IMREAD_COLOR)
    observation = ProgressRecognizer().detect(image, 1.0)

    assert observation is not None
    assert observation.green_left < observation.yellow_x < observation.green_right
    assert observation.green_right - observation.green_left > 0.12


def test_marker_sweep_never_loses_bar_when_marker_splits_green() -> None:
    recognizer = ProgressRecognizer()
    for index, yellow in enumerate(range(75, 166, 3)):
        observation = recognizer.detect(frame(green=(70, 170), yellow=yellow), index / 30)
        assert observation is not None
        assert abs(observation.green_left - 70 / 300) < 0.02
        assert abs(observation.green_right - 171 / 300) < 0.02
```

Run:

```powershell
& 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe' -m pytest try/tests/test_progress.py -q -k "split_marker or marker_sweep"
```

Expected: FAIL because the current recognizer returns `None` for the real fixture and for marker positions that divide the bar below the 12% single-component threshold.

- [ ] **Step 3: Implement five-line reconstruction**

Replace the connected-component decision path with focused helpers in `progress.py`:

```python
from dataclasses import dataclass


_SCAN_FRACTIONS = (0.40, 0.43, 0.46, 0.49, 0.52)
_SIDE_EXCLUSION = 0.08


@dataclass(frozen=True)
class _LineCandidate:
    green_left: int
    green_right: int
    yellow_center: float


def _runs(mask_row: np.ndarray, offset: int) -> list[tuple[int, int]]:
    active = mask_row.astype(bool)
    padded = np.pad(active.astype(np.int8), (1, 1))
    changes = np.diff(padded)
    starts = np.flatnonzero(changes == 1)
    ends = np.flatnonzero(changes == -1)
    return [(int(left + offset), int(right + offset)) for left, right in zip(starts, ends)]


def _line_candidates(
    green_runs: list[tuple[int, int]],
    yellow_runs: list[tuple[int, int]],
    image_width: int,
) -> list[_LineCandidate]:
    candidates: list[_LineCandidate] = []
    minimum_width = image_width * 0.12
    for yellow_left, yellow_right in yellow_runs:
        yellow_center = (yellow_left + yellow_right) / 2
        before = [run for run in green_runs if run[1] <= yellow_right + 3]
        after = [run for run in green_runs if run[0] >= yellow_left - 3]
        if before and after:
            left_run = max(before, key=lambda run: run[1])
            right_run = min(after, key=lambda run: run[0])
            if yellow_left - left_run[1] <= 3 and right_run[0] - yellow_right <= 3:
                if right_run[1] - left_run[0] >= minimum_width:
                    candidates.append(_LineCandidate(left_run[0], right_run[1], yellow_center))
        for green_left, green_right in green_runs:
            green_width = green_right - green_left
            margin = max(12.0, green_width * 0.05)
            if green_width >= minimum_width and green_left - margin <= yellow_center <= green_right + margin:
                candidates.append(_LineCandidate(green_left, green_right, yellow_center))
    return candidates
```

`ProgressRecognizer.detect()` must build HSV masks, apply a horizontal 3-pixel close, scan the five fixed rows, require at least three mutually consistent candidates, and use medians for final coordinates. Confidence must be based on agreeing-line ratio plus normalized logical bar width, not on a single component height.

`_consensus()` groups candidates only when left and right edges each differ by no more than `image_width * 0.02`, takes at most one candidate from each scanline, selects the group with the most scanlines and then the widest logical bar, and returns median left/right/yellow coordinates. `detect()` rejects a consensus narrower than `image_width * 0.12` and computes confidence exactly as:

```python
agreement = agreeing_scanlines / len(_SCAN_FRACTIONS)
width_score = min(1.0, (green_right - green_left) / (image_width * 0.12))
confidence = (agreement + width_score) / 2
```

- [ ] **Step 4: Run focused and complete progress tests**

Run:

```powershell
& 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe' -m pytest try/tests/test_progress.py -q
```

Expected: all progress recognizer/controller tests PASS, including real split fixture, 30 FPS sweep, noise rejection, marker outside edge, and missing-frame behavior.

- [ ] **Step 5: Commit Task 1**

```powershell
git add src/auto_fishing/vision/progress.py try/tests/test_progress.py try/tools/extract_progress_fixture.py try/fixtures/progress/progress_split_marker.png
git commit -m "fix: reconstruct split progress bar from scanlines"
```

---

### Task 2: Add temporal candidate validation and structured diagnostics

**Files:**
- Modify: `src/auto_fishing/model.py:74-81`
- Modify: `src/auto_fishing/vision/progress.py`
- Modify: `src/auto_fishing/vision/scenes.py:60-130`
- Modify: `src/auto_fishing/storage/runtime_logging.py:85-128`
- Modify: `try/tests/test_progress.py`
- Modify: `try/tests/test_scenes.py`
- Modify: `try/tests/test_storage.py`

**Interfaces:**
- Produces: `ProgressScanResult(observation, valid_scanlines, candidate_count, rejection_reason)` from `ProgressRecognizer.analyze()`.
- Preserves: `ProgressRecognizer.detect()` as a compatibility wrapper returning only `observation`.
- Adds to `SceneObservation`: `progress_scanlines: int`, `progress_candidates: int`, `progress_rejection: str`.

- [ ] **Step 1: Write failing diagnostic and jump-confirmation tests**

```python
def test_analyze_reports_scanline_consensus() -> None:
    result = ProgressRecognizer().analyze(frame(), 1.0)
    assert result.observation is not None
    assert result.valid_scanlines >= 3
    assert result.candidate_count >= 1
    assert result.rejection_reason == ""


def test_large_single_frame_jump_is_released_until_confirmed() -> None:
    recognizer = ProgressRecognizer()
    assert recognizer.detect(frame((20, 120), 70), 0.0) is not None
    assert recognizer.detect(frame((170, 270), 220), 1 / 30) is None
    confirmed = recognizer.detect(frame((170, 270), 220), 2 / 30)
    assert confirmed is not None
    assert confirmed.green_left > 0.55
```

Add a runtime-log assertion that a `SceneObservation(progress_rejection="jump_pending")` writes `progress_scanlines`, `progress_candidates`, and `progress_rejection` to `events.jsonl` even when `progress is None`.

Update the existing fast-jump test so that a large location change is pending for one frame and accepted only after the same new location appears again. Keep the separate missing-frame test proving that an empty current frame never returns the last reliable observation.

Run the three focused test files and verify they fail because the new result type and fields do not exist.

- [ ] **Step 2: Implement result and scene diagnostic types**

Add in `progress.py`:

```python
@dataclass(frozen=True)
class ProgressScanResult:
    observation: ProgressObservation | None
    valid_scanlines: int = 0
    candidate_count: int = 0
    rejection_reason: str = ""
```

Add in `model.py` after `progress`:

```python
    progress_scanlines: int = 0
    progress_candidates: int = 0
    progress_rejection: str = ""
```

`SceneRecognizer.observe()` must call `analyze()`, pass `result.observation` as `progress`, and copy the three diagnostic fields into `SceneObservation`.

- [ ] **Step 3: Implement bounded temporal confirmation**

`ProgressRecognizer` stores `deque[ProgressObservation](maxlen=5)` and one pending jump. A candidate whose green center differs from the latest reliable center by more than `image_width * 0.20` returns `ProgressScanResult(None, ..., "jump_pending")`. The next adjacent candidate confirms the jump; absence or a different candidate clears it. Empty masks return `"no_consensus"` and never reuse history.

- [ ] **Step 4: Log diagnostics for every frame**

Add these fields unconditionally in `RuntimeLogStore.record_frame()`:

```python
"progress_scanlines": observation.progress_scanlines,
"progress_candidates": observation.progress_candidates,
"progress_rejection": observation.progress_rejection,
```

Run:

```powershell
& 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe' -m pytest try/tests/test_progress.py try/tests/test_scenes.py try/tests/test_storage.py -q
```

Expected: all focused tests PASS and a missing current frame still returns `None`.

- [ ] **Step 5: Commit Task 2**

```powershell
git add src/auto_fishing/model.py src/auto_fishing/vision/progress.py src/auto_fishing/vision/scenes.py src/auto_fishing/storage/runtime_logging.py try/tests/test_progress.py try/tests/test_scenes.py try/tests/test_storage.py
git commit -m "feat: log progress scan consensus and jumps"
```

---

### Task 3: Preserve the last twelve progress strips on recognition failure

**Files:**
- Modify: `src/auto_fishing/storage/diagnostics.py:10-75`
- Modify: `src/auto_fishing/automation/engine.py:300-1180`
- Modify: `try/tests/test_storage.py`
- Modify: `try/tests/test_engine.py`

**Interfaces:**
- Extends: `DiagnosticsStore.save(frame, code, detail, now=None, *, progress_frames=()) -> str`.
- Engine field: `_progress_frames: deque[np.ndarray]` with `maxlen=12`.
- Diagnostic artifact: `{incident_stem}_progress.jpg`, a 3×4 contact sheet ordered oldest to newest.

- [ ] **Step 1: Write failing storage and engine tests**

```python
def test_diagnostics_save_twelve_progress_frames_as_contact_sheet(tmp_path) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics")
    frames = [np.full((24, 120, 3), index, np.uint8) for index in range(12)]
    stem = store.save(np.zeros((60, 80, 3), np.uint8), "E_PROGRESS_LOST", "lost", progress_frames=frames)
    sheet = cv2.imdecode(np.fromfile(tmp_path / "diagnostics" / f"{stem}_progress.jpg", np.uint8), cv2.IMREAD_COLOR)
    assert sheet.shape[0] == 24 * 3
    assert sheet.shape[1] == 120 * 4
```

Add an engine test that feeds 15 control frames followed by six missing observations and asserts the diagnostic contact sheet contains exactly the newest 12 progress strips. Also assert non-progress failures do not create `_progress.jpg`.

- [ ] **Step 2: Implement contact-sheet persistence and cleanup grouping**

`DiagnosticsStore.save()` accepts `progress_frames`. Use `np.hstack`/`np.vstack` to create a 3×4 sheet, encode JPEG quality 50, and write with `tofile()` for Unicode paths. Cleanup must treat `.png`, `.json`, and `_progress.jpg` as one incident group by removing the `_progress` suffix before grouping; the 7-day and latest-20-incident limits remain unchanged.

- [ ] **Step 3: Maintain a bounded raw progress-strip buffer**

Initialize in `AutomationEngine.__init__`:

```python
self._progress_frames: deque[np.ndarray] = deque(maxlen=12)
```

During `WAIT_BAR` and `CONTROL`, crop `TOP_ROI`, then its local 40%–52% vertical band, and append a contiguous copy. Clear the deque when starting/cancelling a round and after leaving progress-related states. When `_pause()` saves `E_PROGRESS_LOST`, pass `tuple(self._progress_frames)` to `DiagnosticsStore.save()`; all other errors pass an empty tuple.

- [ ] **Step 4: Run storage and engine tests**

```powershell
& 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe' -m pytest try/tests/test_storage.py try/tests/test_engine.py -q
```

Expected: PASS; Unicode paths, cleanup boundaries, error classification, immediate release, and progress contact sheets all remain correct.

- [ ] **Step 5: Commit Task 3**

```powershell
git add src/auto_fishing/storage/diagnostics.py src/auto_fishing/automation/engine.py try/tests/test_storage.py try/tests/test_engine.py
git commit -m "feat: save progress failure frame sequence"
```

---

### Task 4: Integrate, document, build, and run the real acceptance gate

**Files:**
- Modify: `AGENTS.md`
- Modify: `doc/验收标准.md`
- Modify: `doc/进展记录/2026-7-12.md`
- Generated/ignored: `dist/异环自动钓鱼.exe`
- External backup prefix: `D:\0文件夹\备份\异环自动钓鱼-progress-slot-prebuild-` followed by `Get-Date -Format 'yyyyMMdd-HHmmss'`.

**Interfaces:**
- Build command remains `scripts/build.ps1 -PythonPath 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe'`.
- Release manifest must remain `requireAdministrator uiAccess=false` for the current OSK deployment baseline.

- [ ] **Step 1: Run all automated tests**

```powershell
& 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe' -m pytest try/tests -q
```

Expected: all tests PASS with no errors or warnings; record the exact count and duration.

- [ ] **Step 2: Update long-term architecture and acceptance docs**

Document the fixed-slot scanline reconstruction, current-frame-only input safety, structured scan diagnostics, and progress contact-sheet artifact in `AGENTS.md`. Update `doc/验收标准.md` with exact commands, real fixture evidence, synthetic sweep result, and the pending real single-round gate. Record problem → cause → solution, file list, test evidence, external backup path, and rollback hash in the dated progress record.

- [ ] **Step 3: Commit the verified source and documentation state**

```powershell
git add AGENTS.md doc/验收标准.md doc/进展记录/2026-7-12.md
git commit -m "docs: record progress slot reconstruction acceptance"
```

- [ ] **Step 4: Back up the current executable and rebuild**

Create the timestamped backup under `D:\0文件夹\备份`, verify its SHA256, then run:

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = "D:\0文件夹\备份\异环自动钓鱼-progress-slot-prebuild-$stamp"
New-Item -ItemType Directory -Path $backup -Force | Out-Null
Copy-Item -LiteralPath 'dist\异环自动钓鱼.exe' -Destination (Join-Path $backup '异环自动钓鱼.exe')
Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $backup '异环自动钓鱼.exe')
```

Then run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1 -PythonPath 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe'
```

Expected: full tests PASS, PyInstaller succeeds, and `RELEASE_MANIFEST_OK requireAdministrator uiAccess=false` is printed. Record the new SHA256.

- [ ] **Step 5: Start the new executable for real-game acceptance**

```powershell
Start-Process -FilePath (Resolve-Path 'dist\异环自动钓鱼.exe').Path
```

Expected real evidence in the newest `%LOCALAPPDATA%\异环自动钓鱼\runs\run-*\events.jsonl`:

- two F taps succeed;
- control remains active while yellow crosses inside green;
- `progress_scanlines >= 3` on valid frames;
- A/D direction tracks center error and releases in the center deadband;
- the state reaches result, dismisses it, and increments completed count to 1.

If `E_PROGRESS_LOST` occurs, inspect the generated `_progress.jpg` contact sheet before any further code change. Do not merge `main` or delete the branch until the user confirms the complete real round.
