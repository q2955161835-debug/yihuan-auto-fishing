# 最新帧加权进度控制实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 最多使用最近 15 个连续有效进度观测，按识别置信度和新鲜度加权每帧“黄标相对当前绿区中心”的误差，只输出 A、D 或释放，避免旧绿区绝对位置继续驱动操作。

**Architecture:** 保留现有截图、识别器、状态机和输入后端；仅把 `ProgressController` 从单帧比较改为有界相对误差窗口。窗口只保存归一化相对误差，不保存或复用绿区绝对坐标；缺失帧或超过 0.20 秒的观测间隔立即清空。`AutomationCore` 继续负责安全释放，并为每个有效控制帧记录动作、样本数和加权误差。

**Tech Stack:** Python 3.13、`collections.deque`、pytest 9.1.0、现有 `RuntimeLogStore` 事件接口。

## Global Constraints

- 不新增机器学习模型、速度预测器、第二套状态机或新依赖。
- 控制窗口最多 15 帧；缺失帧不得使用历史观测发送 A/D。
- 每个样本只保存 `(relative_error, confidence, timestamp)`；`relative_error=(yellow_x-green_center)/green_width`。
- 权重固定为 `clamp(confidence, 0.05, 1.0) * 0.2 ** age`，最新样本 `age=0`。
- 加权误差大于 `0.10` 输出 `Direction.LEFT`（A），小于 `-0.10` 输出 `Direction.RIGHT`（D），其余输出 `Direction.RELEASE`。
- 只修改任务相关代码；测试文件放在 `try/`；完成后更新 `AGENTS.md`、`doc/验收标准.md` 和当天进展记录。

---

### Task 1: 有界加权控制器

**Files:**
- Modify: `src/auto_fishing/vision/progress.py`
- Test: `try/tests/test_progress.py`

**Interfaces:**
- Consumes: `ProgressObservation | None`。
- Produces: `ProgressController.decide(observation) -> Direction`、只读属性 `sample_count: int` 和 `weighted_error: float`。

- [ ] **Step 1: 写窗口上限、新鲜度、清晰度和清空行为的失败测试**

```python
def control_observation(
    error: float,
    confidence: float,
    timestamp: float,
) -> ProgressObservation:
    return ProgressObservation(0.4, 0.6, 0.5 + error * 0.2, confidence, timestamp)

def test_controller_uses_at_most_fifteen_recent_samples() -> None:
    controller = ProgressController()
    for index in range(20):
        controller.decide(control_observation(0.5, 1.0, index / 30))
    assert controller.sample_count == 15

def test_newer_equal_quality_sample_has_more_weight() -> None:
    controller = ProgressController()
    controller.decide(control_observation(-1.0, 1.0, 0.0))
    assert controller.decide(control_observation(1.0, 1.0, 1 / 30)) is Direction.LEFT
    assert controller.weighted_error > 0.10

def test_clearer_recent_sample_has_more_weight() -> None:
    controller = ProgressController()
    controller.decide(control_observation(-1.0, 1.0, 0.0))
    assert controller.decide(control_observation(1.0, 0.10, 1 / 30)) is Direction.RIGHT
    assert controller.weighted_error < -0.10

def test_missing_observation_clears_weighted_window() -> None:
    controller = ProgressController()
    controller.decide(control_observation(1.0, 1.0, 0.0))
    assert controller.decide(None) is Direction.RELEASE
    assert controller.sample_count == 0
    assert controller.weighted_error == 0.0
```

- [ ] **Step 2: 运行测试并确认旧单帧控制器失败**

Run: `py -3.13 -m pytest try/tests/test_progress.py -q`

Expected: 新增测试因缺少 `sample_count`、`weighted_error` 或加权行为而失败。

- [ ] **Step 3: 最小实现最近 15 帧加权相对误差**

```python
class ProgressController:
    def __init__(self, center_tolerance_ratio: float = 0.10) -> None:
        if not 0 < center_tolerance_ratio < 0.5:
            raise ValueError("center_tolerance_ratio 必须在 0 与 0.5 之间")
        self.center_tolerance_ratio = center_tolerance_ratio
        self._samples: deque[tuple[float, float, float]] = deque(maxlen=15)
        self._weighted_error = 0.0

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    @property
    def weighted_error(self) -> float:
        return self._weighted_error

    def decide(self, observation: ProgressObservation | None) -> Direction:
        if observation is None:
            self._samples.clear()
            self._weighted_error = 0.0
            return Direction.RELEASE
        if self._samples and (
            observation.timestamp <= self._samples[-1][2]
            or observation.timestamp - self._samples[-1][2] > 0.20
        ):
            self._samples.clear()
        green_width = observation.green_right - observation.green_left
        green_center = (observation.green_left + observation.green_right) / 2
        relative_error = (observation.yellow_x - green_center) / green_width
        confidence = min(1.0, max(0.05, observation.confidence))
        self._samples.append((relative_error, confidence, observation.timestamp))
        numerator = 0.0
        denominator = 0.0
        newest = len(self._samples) - 1
        for index, (error, quality, _timestamp) in enumerate(self._samples):
            weight = quality * 0.2 ** (newest - index)
            numerator += error * weight
            denominator += weight
        self._weighted_error = numerator / denominator
        if self._weighted_error > self.center_tolerance_ratio:
            return Direction.LEFT
        if self._weighted_error < -self.center_tolerance_ratio:
            return Direction.RIGHT
        return Direction.RELEASE
```

- [ ] **Step 4: 运行进度专项测试并确认通过**

Run: `py -3.13 -m pytest try/tests/test_progress.py -q`

Expected: 全部通过。

### Task 2: 缺失帧清空与控制诊断日志

**Files:**
- Modify: `src/auto_fishing/automation/engine.py`
- Test: `try/tests/test_engine.py`

**Interfaces:**
- Consumes: `ProgressController.decide(None)`、`sample_count`、`weighted_error`。
- Produces: `progress.control` 运行事件，字段为 `direction`、`sample_count`、`weighted_error`。

- [ ] **Step 1: 写失败测试**

```python
def test_control_records_weighted_direction_diagnostics() -> None:
    runtime_log = RecordingRuntimeLog()
    core, _input, _state_machine = make_core(
        state=FishingState.CONTROL,
        event_recorder=runtime_log,
    )
    progress = ProgressObservation(0.4, 0.6, 0.58, 0.8, 1.0)
    core.process(SceneObservation(progress=progress), None, 1.0, CLIENT)
    event = runtime_log.events[-1]
    assert event["event"] == "progress.control"
    assert event["direction"] == "left"
    assert event["sample_count"] == 1
    assert event["weighted_error"] == pytest.approx(0.4)

def test_missing_progress_clears_controller_window_and_releases() -> None:
    core, input_service, _state_machine = make_core(state=FishingState.CONTROL)
    progress = ProgressObservation(0.4, 0.6, 0.58, 0.8, 1.0)
    core.process(SceneObservation(progress=progress), None, 1.0, CLIENT)
    assert core.controller.sample_count == 1
    core.process(SceneObservation(), None, 1.1, CLIENT)
    assert core.controller.sample_count == 0
    assert input_service.events[-1] == "release"
```

- [ ] **Step 2: 运行测试并确认日志与清空行为缺失**

Run: `py -3.13 -m pytest try/tests/test_engine.py -q`

Expected: 新测试失败，原因是没有 `progress.control` 或缺失帧未调用控制器清空。

- [ ] **Step 3: 在现有 `_control` 边界接入最小逻辑**

```python
direction = self.controller.decide(observation.progress)
self._record(
    "progress.control",
    direction=direction.value,
    sample_count=self.controller.sample_count,
    weighted_error=self.controller.weighted_error,
)
```

缺失观测分支先调用 `self.controller.decide(None)`，再执行现有 `release_all()`。

- [ ] **Step 4: 运行引擎专项测试并确认通过**

Run: `py -3.13 -m pytest try/tests/test_engine.py -q`

Expected: 全部通过，原有安全释放和结算切换行为不变。

### Task 3: 回放、文档、提交与发布物

**Files:**
- Modify: `AGENTS.md`
- Modify: `doc/验收标准.md`
- Modify: `doc/进展记录/2026-7-13.md`

**Interfaces:**
- Consumes: 最新真实运行 `run-20260713T124537041921Z` 中的观测序列。
- Produces: 回放统计、全量测试证据、阶段提交和 `dist/异环自动钓鱼.exe`。

- [ ] **Step 1: 用真实观测序列离线调用新控制器**

Run:

```powershell
$script = @'
from pathlib import Path
import json
from auto_fishing.model import ProgressObservation
from auto_fishing.vision.progress import ProgressController
path = Path(r"C:\Users\29551\AppData\Local\异环自动钓鱼\runs\run-20260713T124537041921Z\events.jsonl")
controller = ProgressController()
directions = set()
maximum = 0
for line in path.read_text(encoding="utf-8").splitlines():
    event = json.loads(line)
    if event.get("event") != "frame.processed" or event.get("state_before") != "控制进度条":
        continue
    if "green_left" not in event:
        direction = controller.decide(None)
    else:
        direction = controller.decide(ProgressObservation(
            event["green_left"], event["green_right"], event["yellow_x"],
            event["confidence"], event["frame_timestamp"],
        ))
    directions.add(direction.value)
    maximum = max(maximum, controller.sample_count)
assert maximum <= 15
assert controller.sample_count == 0
assert directions <= {"left", "right", "release"}
print("REAL_CONTROL_REPLAY_OK", maximum, sorted(directions))
'@
$env:PYTHONPATH=(Resolve-Path src).Path
$script | py -3.13 -
```

Expected: 每次决策只使用最多 15 个样本；缺失观测后样本数归零；输出仅为 `left/right/release`。

- [ ] **Step 2: 运行全量验证**

Run: `py -3.13 -m pytest try/tests -q`

Run: `git diff --check`

Expected: 全部测试通过且无空白错误。

- [ ] **Step 3: 更新长期规则、验收和进展文档**

记录真实问题、单帧控制原因、最多 15 帧的相对误差加权方案、修改文件、测试命令、人工验收项、技术债和回退方案。

- [ ] **Step 4: 提交实现和文档**

```powershell
git add AGENTS.md doc src try/tests docs/superpowers/plans
git commit -m "fix: weight recent progress control frames"
```

- [ ] **Step 5: 构建前备份旧发布物并限制同类备份最多两份**

在 `D:\0文件夹\备份\异环自动钓鱼-weighted-control-prebuild-YYYYMMDD-HHmm\` 保存旧 EXE，并核对源与备份 SHA256；删除更早的同类 `异环自动钓鱼-weighted-control-prebuild-*`，只保留最新两份。

- [ ] **Step 6: 构建并验证发布物**

Run:

```powershell
$env:PYINSTALLER_CONFIG_DIR=(Resolve-Path 'try/output').Path
$env:TEMP=(Resolve-Path 'try/output').Path
$env:TMP=$env:TEMP
powershell -ExecutionPolicy Bypass -File scripts/build.ps1 -PythonPath C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe
```

Run: `powershell -ExecutionPolicy Bypass -File try/smoke_exe.ps1`

Expected: 测试门通过、发布清单校验通过、输出 SHA256、烟雾输出 `SMOKE_OK`。

- [ ] **Step 7: 最终核对**

Run: `git status --short --branch`

Expected: 功能分支工作区干净；由于真实游戏人工验收尚未执行，不合并 `main`，并在报告中说明保留分支的原因。
