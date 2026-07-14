# V2 30 秒诊断增强实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 V2 事件、全景缩略图、逐帧进度识别轨迹和无损进度条窄带统一扩展到最近 30 秒，并让错误包能够精确关联构建、识别、控制和输入证据。

**Architecture:** 识别器旁路生成不可参与控制的诊断结构；内存记录器按帧保存结构化轨迹并以 10 FPS 保存全景 JPEG 与原始横向分辨率进度 PNG；诊断服务冻结快照后异步生成 ZIP、补充构建哈希与覆盖统计，并合并主动报告紧邻的重复 `E_WINDOW`。正式识别输出、控制算法、状态机和输入节奏保持不变。

**Tech Stack:** Python 3.13、NumPy 2.4.1、OpenCV 4.13.0、pytest 9.1.0、PyInstaller 6.19.0、标准库 `dataclasses`/`deque`/`zipfile`/`hashlib`。

## Global Constraints

- 事件、进度轨迹、全景 JPEG 和进度 PNG 均只保留最近 30 秒。
- 全景 JPEG 与进度 PNG 最高 10 FPS；全景最长边 480、质量 50；进度窄带保持原始横向分辨率并使用无损 PNG。
- 正常运行不写磁盘；只在异常或主动报告时异步生成 ZIP，最多保留 5 份。
- 诊断失败不得改变识别输出、控制方向、输入节奏或暂停自动化。
- 不修改 HSV 阈值、候选排序、中心死区、15 帧加权控制窗口、0.2 秒帧间隔边界或状态机。
- 所有测试与临时产物位于 `try/`；不提交 `.env`、真实密钥、用户目录或私有地址。
- 当前环境没有项目 `.venv`，执行测试使用已安装固定依赖的 `py -3.13`；构建显式传入 `C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe`，只读取既有 C 盘解释器与 PyInstaller 默认缓存。
- 完成后合并 `main` 并删除 `codex/feat-v2-diagnostics-30s`；GitHub 推送另行询问用户。

---

## 文件结构

- Modify: `src/auto_fishing/model.py` — 不可变进度诊断数据模型及 `SceneObservation` 诊断字段。
- Modify: `src/auto_fishing/vision/progress.py` — 生成逐扫描线诊断证据，不改变正式观测。
- Modify: `src/auto_fishing/vision/scenes.py` — 将 `ProgressScanResult.diagnostics` 传入场景观测。
- Modify: `src/auto_fishing/storage/recording.py` — 无损 PNG 编码与诊断结构序列化助手。
- Modify: `src/auto_fishing/storage/memory_diagnostics.py` — 30 秒四类内存环、事件序号和分类丢弃计数。
- Modify: `src/auto_fishing/automation/engine.py` — 控制事件关联字段与报告前上下文。
- Modify: `src/auto_fishing/storage/diagnostic_bundles.py` — 新 ZIP 内容、覆盖统计、构建标识和重复报告合并。
- Modify: `src/auto_fishing/product.py` — 单一 V2 版本常量 `2.0.2`。
- Modify: `try/tests/test_progress.py` — 识别诊断轨迹测试。
- Modify: `try/tests/test_v2_diagnostics.py` — 30 秒内存环、PNG、ZIP、哈希和合并测试。
- Modify: `try/tests/test_engine.py` — 控制关联与报告前状态测试。
- Modify: `try/tests/test_ui_smoke.py` — V2 版本和装配回归。
- Modify: `AGENTS.md`、`doc/验收标准.md`、`doc/进展记录/2026-7-15.md` — 长期规则、验收证据和阶段记录。

### Task 1: 生成不参与控制的逐扫描线诊断结构

**Files:**
- Modify: `src/auto_fishing/model.py`
- Modify: `src/auto_fishing/vision/progress.py`
- Modify: `src/auto_fishing/vision/scenes.py`
- Test: `try/tests/test_progress.py`

**Interfaces:**
- Produces: `ProgressScanDiagnostics(image_width, image_height, scan_rows, minimum_green_width, yellow_runs, selected_yellow, green_runs_by_line, candidate_counts_by_line, reference, selected_green, agreeing_scanlines, truncated)`.
- Extends: `ProgressScanResult.diagnostics: ProgressScanDiagnostics`.
- Extends: `SceneObservation.progress_diagnostics: ProgressScanDiagnostics | None`.

- [ ] **Step 1: 写入失败测试**

在 `try/tests/test_progress.py` 新增：

```python
def test_analyze_exposes_scan_runs_and_selection_without_changing_result() -> None:
    recognizer = ProgressRecognizer()
    result = recognizer.analyze(frame(green=(70, 170), yellow=120), 1.0)

    assert result.observation is not None
    diagnostics = result.diagnostics
    assert diagnostics.image_width == 300
    assert diagnostics.image_height == 120
    assert len(diagnostics.scan_rows) == 5
    assert diagnostics.selected_yellow is not None
    assert len(diagnostics.yellow_runs) >= 1
    assert len(diagnostics.green_runs_by_line) == 5
    assert len(diagnostics.candidate_counts_by_line) == 5
    assert diagnostics.selected_green is not None
    assert diagnostics.agreeing_scanlines == result.valid_scanlines
    assert diagnostics.truncated is False
    assert result.observation.green_left == pytest.approx(
        diagnostics.selected_green[0] / diagnostics.image_width
    )


def test_yellow_missing_still_records_green_scan_runs() -> None:
    image = frame()
    image[:, :, :] = 0
    cv2.rectangle(image, (70, 40), (170, 70), GREEN_BGR, -1)

    result = ProgressRecognizer().analyze(image, 1.0)

    assert result.observation is None
    assert result.rejection_reason == "yellow_missing"
    assert result.diagnostics.yellow_runs == ()
    assert any(result.diagnostics.green_runs_by_line)
```

- [ ] **Step 2: 验证 RED**

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_progress.py -q`

Expected: FAIL，因为 `ProgressScanResult` 尚无 `diagnostics`。

- [ ] **Step 3: 最小实现**

在 `model.py` 定义只包含 JSON 安全标量/元组的 `ProgressScanDiagnostics`。在 `progress.py` 中先计算五行绿段和可用黄段，再执行既有选择逻辑；用 `_diagnostics(...)` 构造旁路证据。连续段诊断副本每行最多 128 项；超限时保留前 127 项和最终选中相关项，并设置 `truncated=True`。正式 `_consensus` 继续使用未截断列表。

`ProgressScanResult` 的所有返回分支必须携带诊断对象；`SceneRecognizer.observe` 只把该对象放入 `SceneObservation.progress_diagnostics`，不得读取它来改变 `progress`。

- [ ] **Step 4: 验证 GREEN 与正式输出不变**

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_progress.py try/tests/test_scenes.py -q`

Expected: PASS；既有坐标和方向断言全部保持。

- [ ] **Step 5: 提交**

```powershell
git add src/auto_fishing/model.py src/auto_fishing/vision/progress.py src/auto_fishing/vision/scenes.py try/tests/test_progress.py
git commit -m "feat: expose progress recognition diagnostics"
```

### Task 2: 建立统一 30 秒内存证据环

**Files:**
- Modify: `src/auto_fishing/storage/recording.py`
- Modify: `src/auto_fishing/storage/memory_diagnostics.py`
- Test: `try/tests/test_v2_diagnostics.py`

**Interfaces:**
- Produces: `encode_png(image: np.ndarray, compression: int = 3) -> bytes`.
- Produces: `BufferedProgressFrame(name, monotonic, png)`.
- Extends: `DiagnosticSnapshot(events, progress_traces, frames, progress_frames, drop_counts, captured_monotonic)`.
- `MemoryDiagnosticRecorder.record_frame(...) -> int` 保持调用签名不变。

- [ ] **Step 1: 写入 30 秒、序号和无损 PNG 失败测试**

将现有 20/10 秒测试改为 30 秒，并新增：

```python
def test_events_have_strictly_increasing_sequence_and_keep_thirty_seconds() -> None:
    clock = [0.0]
    recorder = MemoryDiagnosticRecorder(clock=lambda: clock[0])
    recorder.event("first")
    clock[0] = 30.0
    recorder.event("boundary")
    snapshot = recorder.snapshot()
    assert [item["event"] for item in snapshot.events] == ["first", "boundary"]
    assert [item["sequence"] for item in snapshot.events] == [1, 2]
    clock[0] = 30.001
    recorder.event("after")
    assert [item["event"] for item in recorder.snapshot().events] == [
        "boundary", "after"
    ]


def test_progress_band_is_lossless_native_width_and_sampled_at_ten_fps() -> None:
    clock = [0.0]
    recorder = MemoryDiagnosticRecorder(clock=lambda: clock[0])
    source = np.zeros((720, 1280, 3), dtype=np.uint8)
    source[45:65, 400:900] = (17, 123, 231)
    observation = SceneObservation(
        progress_diagnostics=ProgressRecognizer().analyze(frame(), 0.0).diagnostics
    )
    for index in range(31):
        clock[0] = index / 30
        recorder.record_frame(
            source,
            observation=observation,
            state_before=FishingState.CONTROL,
            snapshot=RuntimeSnapshot(FishingState.CONTROL, 0, 1, 30.0),
            frame_timestamp=clock[0],
            now_monotonic=clock[0],
        )
    snapshot = recorder.snapshot()
    assert 10 <= len(snapshot.progress_frames) <= 11
    decoded = cv2.imdecode(
        np.frombuffer(snapshot.progress_frames[-1].png, dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    assert decoded.shape[1] == round(1280 * (0.76 - 0.24))
```

- [ ] **Step 2: 验证 RED**

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_v2_diagnostics.py -q`

Expected: FAIL，现有窗口仍为 20/10 秒且没有进度 PNG/事件序号。

- [ ] **Step 3: 最小实现四类环形数据**

`recording.py` 使用 `cv2.imencode('.png', np.ascontiguousarray(image), [cv2.IMWRITE_PNG_COMPRESSION, compression])` 实现无损编码。

`MemoryDiagnosticRecorder` 使用统一 `_WINDOW_SECONDS = 30.0`；事件在锁内分配 `sequence`，帧继续分配 `frame_index`。每帧把 `progress_diagnostics` 通过 `dataclasses.asdict` 写入 `progress_traces`；仅 `WAIT_BAR/CONTROL` 在 10 FPS 门内裁剪 `TOP_ROI` 的 40%～52% 窄带并编码 PNG。JPEG 与 PNG 分别捕获异常并更新：

```python
{
    "context_frames": int,
    "progress_frames": int,
    "progress_traces": int,
}
```

`dropped_items` 保持为三类之和以兼容旧元数据。

- [ ] **Step 4: 增加编码失败不外抛测试并验证 GREEN**

用 `monkeypatch` 让 `encode_png` 抛出 `OSError("PNG 编码失败")`，断言 `record_frame` 正常返回、事件和轨迹仍存在、`drop_counts["progress_frames"] == 1`。

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_v2_diagnostics.py try/tests/test_storage.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/auto_fishing/storage/recording.py src/auto_fishing/storage/memory_diagnostics.py try/tests/test_v2_diagnostics.py try/tests/test_storage.py
git commit -m "feat: retain thirty seconds of diagnostic evidence"
```

### Task 3: 直接关联控制决策与报告前状态

**Files:**
- Modify: `src/auto_fishing/automation/engine.py`
- Test: `try/tests/test_engine.py`

**Interfaces:**
- Extends event: `progress.control(frame_timestamp, green_left, green_right, yellow_x, instantaneous_error, requested_key, held_keys_before, ...)`.
- Extends report context: `pre_report_state`, `pre_report_completed`, `pre_report_target`, `held_keys_before_report`.

- [ ] **Step 1: 扩充现有失败测试**

在 `test_control_records_weighted_recent_frame_decision` 增加：

```python
assert event["frame_timestamp"] == 0.1
assert event["green_left"] == 0.3
assert event["green_right"] == 0.7
assert event["yellow_x"] == 0.7
assert event["instantaneous_error"] == pytest.approx(0.5)
assert event["requested_key"] == "A"
assert event["held_keys_before"] == []
```

新增主动报告测试，将测试输入对象设置 `held = {"A"}`，调用 `report_error()` 后断言请求 context 中保留 `held_keys_before_report == ["A"]` 和暂停前状态。

- [ ] **Step 2: 验证 RED**

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_engine.py -q`

Expected: FAIL，缺少新增事件/上下文字段。

- [ ] **Step 3: 最小实现**

`AutomationCore._control` 直接从当前 `ProgressObservation` 计算即时相对误差，读取 `input_service.held` 的副本（不存在时为空），先记录完整事件再调用既有 `set_direction`。键映射固定为 `left -> A`、`right -> D`、`release -> None`。

`AutomationEngine.report_error` 和 `_pause` 在释放输入前冻结 `RuntimeSnapshot` 与 held 集合，再通过 `_diagnostic_context(pre_snapshot=..., held_keys=...)` 传给报告服务。不得改变现有先释放输入再生成报告的安全顺序。

- [ ] **Step 4: 验证 GREEN**

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_engine.py try/tests/test_safe_input.py -q`

Expected: PASS；输入方向与现有事件顺序不变。

- [ ] **Step 5: 提交**

```powershell
git add src/auto_fishing/automation/engine.py try/tests/test_engine.py
git commit -m "feat: correlate progress decisions with frames"
```

### Task 4: 生成可识别构建且不重复的诊断 ZIP

**Files:**
- Modify: `src/auto_fishing/storage/diagnostic_bundles.py`
- Test: `try/tests/test_v2_diagnostics.py`

**Interfaces:**
- Extends constructor: `clock: Callable[[], float] = time.monotonic`, `executable_info: Callable[[], Mapping[str, Any]] | None = None`.
- Produces ZIP members: `progress/trace.jsonl`, `progress/frames/*.png`.
- Produces metadata: schema 2、coverage、分类丢弃、最近控制、冻结 EXE 标识。

- [ ] **Step 1: 写入 ZIP 契约和覆盖统计失败测试**

扩充现有包测试：

```python
assert {
    "error.json", "events.jsonl", "progress/trace.jsonl", "error.jpg"
} <= names
assert any(name.startswith("progress/frames/") for name in names)
assert metadata["diagnostic_schema_version"] == 2
assert metadata["coverage"]["events"]["count"] >= 1
assert metadata["diagnostic_drop_counts"] == {
    "context_frames": 0,
    "progress_frames": 0,
    "progress_traces": 0,
}
assert metadata["frozen"] is True
assert metadata["executable_sha256"] == "abc123"
```

测试服务注入 `executable_info=lambda: {"frozen": True, "executable_name": "app.exe", "executable_size": 1, "executable_sha256": "abc123"}`。

- [ ] **Step 2: 写入哈希失败与相邻报告合并失败测试**

新增三个测试：

1. `executable_info` 抛错时 ZIP 仍成功且 `error.json` 含 `executable_hash_error`；
2. 自动 `E_WINDOW` 成功后 1.0 秒内主动报告成功，只剩主动报告 ZIP；
3. 主动报告以空尺寸错误帧触发写入失败时，先前自动 `E_WINDOW` ZIP 仍存在。

- [ ] **Step 3: 验证 RED**

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_v2_diagnostics.py -q`

Expected: FAIL，缺少新成员、构建标识与合并行为。

- [ ] **Step 4: 最小实现 ZIP 与元数据**

请求对象保存 `requested_monotonic`。后台写包时先准备不会抛出到顶层的构建信息，再写 `error.json`、事件、轨迹、两类图片和 `error.jpg`。`coverage` 对每类记录输出：

```python
{"count": count, "first_monotonic": first, "last_monotonic": last,
 "duration_seconds": max(0.0, last - first)}
```

默认构建信息仅在 `sys.frozen` 为真时读取 `sys.executable`，以 1 MiB 块计算 SHA256；只写文件名、大小和摘要，不写完整路径。

单线程后台任务维护本服务实例已成功写入的报告记录。主动报告成功后删除请求时间相差 `0.0～1.0` 秒且 code 为 `E_WINDOW`、type 为 `automatic` 的前一报告，再执行最多 5 份清理。主动报告失败不调用合并。

- [ ] **Step 5: 验证 GREEN**

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_v2_diagnostics.py -q`

Expected: PASS；ZIP 临时文件清理断言继续通过。

- [ ] **Step 6: 提交**

```powershell
git add src/auto_fishing/storage/diagnostic_bundles.py try/tests/test_v2_diagnostics.py
git commit -m "feat: enrich and coalesce V2 diagnostic bundles"
```

### Task 5: 更新 V2 版本与装配回归

**Files:**
- Modify: `src/auto_fishing/product.py`
- Modify: `try/tests/test_ui_smoke.py`
- Test: `try/tests/test_v2_diagnostics.py`

**Interfaces:**
- Produces: `V2_VERSION = "2.0.2"`.
- `v2_profile().version` 只引用该常量。

- [ ] **Step 1: 先把版本测试改为 2.0.2 并验证 RED**

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_ui_smoke.py::test_v2_profile_uses_local_app_data_and_explicit_version -q`

Expected: FAIL，实际仍是 `2.0.0`。

- [ ] **Step 2: 定义单一版本常量并验证 GREEN**

在 `product.py` 顶层定义 `V2_VERSION = "2.0.2"`，`v2_profile` 引用它。不要改 V1 版本。

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_ui_smoke.py try/tests/test_v2_diagnostics.py -q`

Expected: PASS；V1 标题、路径与日志装配不变。

- [ ] **Step 3: 提交**

```powershell
git add src/auto_fishing/product.py try/tests/test_ui_smoke.py try/tests/test_v2_diagnostics.py
git commit -m "chore: identify V2 diagnostic build as 2.0.2"
```

### Task 6: 回归、构建、文档与主线合并

**Files:**
- Modify: `AGENTS.md`
- Modify: `doc/验收标准.md`
- Create: `doc/进展记录/2026-7-15.md`
- Build output ignored: `dist/异环自动钓鱼V2.exe`, root `异环自动钓鱼V2.exe`

- [ ] **Step 1: 运行源码和完整测试验证**

```powershell
$env:PYTHONPATH='src'
py -3.13 -m compileall -q src try/tests scripts
py -3.13 -m pytest try/tests -q
git diff --check
```

Expected: 全部 PASS，测试数至少 410，`git diff --check` 无错误。

- [ ] **Step 2: 运行保留的 412 帧真实控制回放**

Run:

```powershell
$script = @'
from pathlib import Path
import json
from auto_fishing.model import ProgressObservation
from auto_fishing.vision.progress import ProgressController

path = Path(r"D:\29551\异环自动钓鱼数据\runs\run-20260714T095204572440Z\events.jsonl")
controller = ProgressController()
directions = set()
maximum = 0
processed = 0
for line in path.read_text(encoding="utf-8").splitlines():
    event = json.loads(line)
    if event.get("event") != "frame.processed" or event.get("state_before") != "控制进度条":
        continue
    if processed == 412:
        break
    if "green_left" not in event:
        direction = controller.decide(None)
    else:
        direction = controller.decide(ProgressObservation(
            event["green_left"], event["green_right"], event["yellow_x"],
            event["confidence"], event["frame_timestamp"],
        ))
    processed += 1
    directions.add(direction.value)
    maximum = max(maximum, controller.sample_count)
assert processed == 412
assert maximum <= 15
assert directions <= {"left", "right", "release"}
controller.decide(None)
assert controller.sample_count == 0
print("REAL_CONTROL_REPLAY_OK", processed, maximum, sorted(directions))
'@
$env:PYTHONPATH=(Resolve-Path src).Path
$script | py -3.13 -
```

Expected: `REAL_CONTROL_REPLAY_OK 412 15 ['left', 'release', 'right']`。

- [ ] **Step 3: 构建并验证单文件发布物**

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_v2.ps1 -PythonPath C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe
py -3.13 scripts/verify_release.py dist/异环自动钓鱼V2.exe
Get-FileHash -Algorithm SHA256 -LiteralPath dist/异环自动钓鱼V2.exe
```

Expected: 构建成功，最终内嵌清单含 `requireAdministrator`、`uiAccess=false`、`PerMonitorV2` 和 `true/pm`。

- [ ] **Step 4: 尝试烟雾并如实记录权限边界**

Run: `& .\try\smoke_exe.ps1 -TargetPath .\dist\异环自动钓鱼V2.exe`

Expected: 当前非提升 PowerShell 应明确拒绝；不得把该结果记为提升烟雾通过。Windows 10 真机继续标记人工确认。

- [ ] **Step 5: 更新长期文档和高风险前阶段记录**

先生成备份时间戳：

```powershell
$BackupStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$BackupPath = "D:\0文件夹\备份\异环自动钓鱼-v2.0.2-before-$BackupStamp"
$BackupPath
```

`AGENTS.md` 记录 30 秒四类证据、ZIP 新结构、版本 `2.0.2`、新测试基线和待人工项。`doc/验收标准.md` 增加 ZIP 内容/时长/重复合并/构建标识的可验证步骤和结果。`doc/进展记录/2026-7-15.md` 使用本地分钟级时间段记录审计结论、问题-原因-解决方案、修改文件、命令结果、上述精确 `$BackupPath`、即将执行的根目录 EXE 覆盖、回退文件、C 盘既有解释器/缓存使用和回退提交。该记录必须在覆盖根目录 EXE 前完成。

- [ ] **Step 6: 提交文档与发布候选记录**

```powershell
git add AGENTS.md doc/验收标准.md doc/进展记录/2026-7-15.md
git commit -m "docs: record 30 second diagnostic validation"
```

- [ ] **Step 7: 备份并替换根目录候选，核对哈希**

使用 Step 5 已记录的精确 `$BackupPath`：

```powershell
$BackupRoot = (Resolve-Path -LiteralPath 'D:\0文件夹\备份').Path
New-Item -ItemType Directory -Path $BackupPath -Force | Out-Null
Copy-Item -LiteralPath '.\异环自动钓鱼V2.exe' -Destination $BackupPath

$matching = @(Get-ChildItem -LiteralPath $BackupRoot -Directory -Filter '异环自动钓鱼-v2.0.2-before-*' | Sort-Object LastWriteTime -Descending)
foreach ($old in $matching | Select-Object -Skip 2) {
    $resolved = $old.FullName
    if ([IO.Path]::GetDirectoryName($resolved) -ne $BackupRoot) {
        throw "拒绝删除备份根目录之外的路径：$resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

Copy-Item -LiteralPath '.\dist\异环自动钓鱼V2.exe' -Destination '.\异环自动钓鱼V2.exe' -Force
$distHash = (Get-FileHash -Algorithm SHA256 -LiteralPath '.\dist\异环自动钓鱼V2.exe').Hash
$rootHash = (Get-FileHash -Algorithm SHA256 -LiteralPath '.\异环自动钓鱼V2.exe').Hash
if ($distHash -ne $rootHash) { throw '根目录候选与 dist 构建哈希不一致' }
```

Expected: 备份根目录解析为 `D:\0文件夹\备份`，同类备份最多 2 份，根目录与 `dist` SHA256 完全一致。若复制或哈希失败，立即从 `$BackupPath\异环自动钓鱼V2.exe` 恢复。

- [ ] **Step 8: 完成分支、自检后合并主线**

重新运行完整 pytest、412 帧回放和 `git diff --check`。确认工作区干净后切到 `main`，执行非快进合并：

```powershell
git switch main
git merge --no-ff codex/feat-v2-diagnostics-30s -m "merge: add 30 second V2 diagnostics"
git branch -d codex/feat-v2-diagnostics-30s
git status --short --branch
```

Expected: `main` 干净、功能提交可追溯、任务分支已删除。不要推送 GitHub；最终询问用户是否推送私有仓库。
