# 进度条启用门与继续重开当前轮实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 阻止第二次 F 后的动画和场景颜色被误当成顶部进度条，并把“继续”改为保留计数、放弃未完成鱼、重新抛竿。

**Architecture:** 在 `AutomationCore`（自动化核心）的 `WAIT_BAR`（等待进度条）入口增加按单调时间启用的 0.60 秒门和连续 3 帧确认；启用前及确认中断时清空视觉历史，进入控制后复用现有 15 帧中心控制。状态机提供不依赖画面的 `restart_round()`（重开当前轮），工作线程在确认新鲜前台帧后消费现有恢复令牌并调用它。

**Tech Stack:** Python 3.13、pytest 9.1、Tkinter/ttk、NumPy、OpenCV、DXcam、Win32 ctypes、PyInstaller 6.19

## Global Constraints

- 不改变高品质鱼短绿色区域的现有最小宽度 `max(4 像素, TOP_ROI 宽度 * 0.012)`。
- 不增加黄标与绿区相邻、重叠或相对距离约束。
- 不增加速度预测、旧绝对位置驱动或第二套 A/D 控制状态机。
- 第二次 F 后等待 0.60 秒，再要求连续 3 张有效且时间戳递增的新帧。
- 任一非递增时间戳或超过 0.20 秒的帧间隔都清空本次确认并释放 A/D。
- “继续”保留绑定、目标次数和已完成次数，只丢弃当前未完成轮并重新抛竿。
- V2 正常运行不持续落盘；诊断 ZIP 上限仍为最近 5 份。
- Windows 11 自动验收，Windows 10 真机继续标记人工确认。

---

## 文件结构与职责

- `src/auto_fishing/automation/state_machine.py`：新增原子化 `restart_round(now)` 状态操作并删除基于视觉分类的恢复事件。
- `src/auto_fishing/automation/engine.py`：实现核心重开、进度启用门、连续帧确认、诊断事件和工作线程令牌消费。
- `src/auto_fishing/ui/main_window.py`：在继续动作期间显示“放弃当前轮并开始新一轮”的明确提示。
- `try/tests/test_state_machine.py`：验证计数保留和旧恢复事件删除。
- `try/tests/test_engine.py`：用诊断时序回归启用门、历史清理、窄绿条、重开与并发安全。
- `try/tests/test_ui_smoke.py`：验证继续按钮和自动/手动切回路径的新提示及单次请求。
- `AGENTS.md`、`doc/验收标准.md`、`doc/进展记录/2026-7-14.md`：同步长期规则、可验证验收项和问题—原因—方案。

### Task 1: 状态机原子重开当前轮

**Files:**
- Modify: `src/auto_fishing/automation/state_machine.py`
- Test: `try/tests/test_state_machine.py`

**Interfaces:**
- Consumes: `FishingStateMachine.state/target/completed/paused_from`
- Produces: `FishingStateMachine.restart_round(now: float) -> bool`

- [ ] **Step 1: 写失败测试**

在 `try/tests/test_state_machine.py` 增加参数化测试：从 `WAIT_BITE`、`WAIT_BAR`、`CONTROL`、`WAIT_RESULT` 暂停后调用 `restart_round(20.0)`，断言返回 `True`，状态为 `READY`，`target == 2`、`completed` 不变、`entered_at == 20.0`、暂停原因和 `paused_from` 清空；非 `PAUSED` 调用返回 `False` 且状态不变。

```python
@pytest.mark.parametrize(
    "paused_from",
    [FishingState.WAIT_BITE, FishingState.WAIT_BAR,
     FishingState.CONTROL, FishingState.WAIT_RESULT],
)
def test_restart_round_preserves_counts_and_returns_ready(paused_from):
    machine = reach_state(paused_from)
    machine.completed = 1
    machine.pause("用户暂停", 12.0)
    assert machine.restart_round(20.0) is True
    assert (machine.state, machine.completed, machine.target) == (
        FishingState.READY, 1, 2
    )
    assert machine.paused_from is None
    assert machine.pause_reason == ""
```

- [ ] **Step 2: 运行测试确认失败**

Run: `./.venv/Scripts/python.exe -m pytest try/tests/test_state_machine.py -q`

Expected: FAIL，提示 `FishingStateMachine` 没有 `restart_round`。

- [ ] **Step 3: 写最小实现**

在 `FishingStateMachine` 增加：

```python
def restart_round(self, now: float) -> bool:
    if self.state is not FishingState.PAUSED:
        return False
    self.state = FishingState.READY
    self.entered_at = now
    self.pause_reason = ""
    self.paused_from = None
    return True
```

删除 `RESUME_CONTROL/RESUME_RESULT/RESUME_READY` 事件和对应迁移，更新旧状态机恢复测试为重开测试。

- [ ] **Step 4: 运行状态机测试确认通过**

Run: `./.venv/Scripts/python.exe -m pytest try/tests/test_state_machine.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/auto_fishing/automation/state_machine.py try/tests/test_state_machine.py
git commit -m "fix: restart paused fishing from a fresh round"
```

### Task 2: 第二次 F 后的进度启用门

**Files:**
- Modify: `src/auto_fishing/automation/engine.py`
- Test: `try/tests/test_engine.py`

**Interfaces:**
- Consumes: `SceneObservation.progress`、`FramePacket.timestamp`、`SceneRecognizer.reset_progress_tracking()`
- Produces: `AutomationCore._await_progress_entry(observation, packet, now) -> None`

- [ ] **Step 1: 写诊断时序失败测试**

用 `ScriptedRecognizer` 和 `RecordingInput` 建立核心，第二次 F 后传入诊断包对应的连续错误观测；首张 `WAIT_BAR` 帧负责设定 `now + 0.60` 的启用时刻。断言 0.60 秒内状态始终为 `WAIT_BAR`、没有 `left/right`，视觉历史每帧被重置；启用后前两张有效帧仍等待，第三张才进入 `CONTROL`。

```python
false_progress = ProgressObservation(0.100, 0.278, 0.537, 0.8, 1.0)
for index in range(19):
    now = 0.03 + index / 30
    core.process(
        SceneObservation(progress=false_progress),
        packet(now), now, CLIENT,
    )
assert core.snapshot.state is FishingState.WAIT_BAR
assert not ({"left", "right"} & set(input_service.events))
```

再增加：缺失帧清零；非递增时间戳不计数；间隔超过 0.20 秒后当前帧成为新序列首帧；达到现有最小宽度的窄绿区以及黄标位于绿区两侧时都能完成确认。

- [ ] **Step 2: 运行定向测试确认失败**

Run: `./.venv/Scripts/python.exe -m pytest try/tests/test_engine.py -q -k "progress_entry or wait_bar"`

Expected: FAIL，现有代码在第一张错误观测立即进入 `CONTROL`。

- [ ] **Step 3: 实现启用门和确认状态**

在 `engine.py` 增加常量和字段：

```python
_PROGRESS_ENTRY_DELAY = 0.60
_PROGRESS_ENTRY_CONFIRM_FRAMES = 3
_PROGRESS_ENTRY_MAX_FRAME_GAP = 0.20

self.progress_entry_armed_at: float | None = None
self.progress_entry_confirm_frames = 0
self.progress_entry_last_timestamp: float | None = None
```

第二次 F 成功后把启用字段恢复为空，并重置视觉历史。`WAIT_BAR` 首张后续帧设置
`progress_entry_armed_at = now + 0.60`；启用前每帧调用
`scene_recognizer.reset_progress_tracking()`。启用后只统计完整进度观测：缺失、非递增和超长间隔按规格清零；第三张连续有效帧触发 `Event.BAR_DETECTED`。所有重置路径都调用现有控制器空观测/输入释放边界，不发 A/D。

诊断事件使用以下固定名称：

```python
self._record("progress.entry_armed", delay_seconds=0.60,
             required_frames=3)
self._record("progress.entry_ignored", reason="arming")
self._record("progress.entry_confirming", count=count,
             required_frames=3)
self._record("progress.entry_confirmed", count=3)
```

- [ ] **Step 4: 运行定向测试确认通过**

Run: `./.venv/Scripts/python.exe -m pytest try/tests/test_engine.py -q -k "progress_entry or second_f or wait_bar"`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/auto_fishing/automation/engine.py try/tests/test_engine.py
git commit -m "fix: arm progress detection after reel animation"
```

### Task 3: 工作线程把继续请求改成重开请求

**Files:**
- Modify: `src/auto_fishing/automation/engine.py`
- Test: `try/tests/test_engine.py`

**Interfaces:**
- Consumes: `FishingStateMachine.restart_round(now)`、现有 `_resume_request` epoch（代次）令牌和前台复核
- Produces: `AutomationCore.restart_round(now: float) -> bool`；`AutomationEngine.resume()` 对外名称不变但语义为重开

- [ ] **Step 1: 写失败测试**

替换基于 `SceneObservation(ready/progress/result)` 分类恢复的旧测试，断言：

```python
core.pause("用户暂停", 2.0)
completed_before = core.snapshot.completed
assert core.restart_round(3.0) is True
assert core.snapshot.state is FishingState.READY
assert core.snapshot.completed == completed_before
assert input_service.events[-1] == "release"
```

引擎级测试让恢复后的第一张画面返回错误 `progress`，仍必须先进入 `READY`，下一张新鲜前台帧只发送第一 F 并进入 `WAIT_BITE`，绝不产生 A/D。保留并更新现有激活失败、ABA（A-B-A 竞态）、迟到窗口/截图错误、暂停取消请求测试。

- [ ] **Step 2: 运行恢复与竞态测试确认失败**

Run: `./.venv/Scripts/python.exe -m pytest try/tests/test_engine.py -q -k "resume or restart_round or late_window or aba"`

Expected: FAIL，现有 `resume(observation, now)` 仍依赖单帧视觉分类。

- [ ] **Step 3: 实现核心和工作线程重开**

把核心方法替换为：

```python
def restart_round(self, now: float) -> bool:
    with self._lock:
        if self.state_machine.state is not FishingState.PAUSED:
            return False
        self.input_service.release_all()
        if not self.state_machine.restart_round(now):
            return False
        self._reset_progress_tracking()
        self._reset_progress_entry()
        self.bar_valid_frames = 0
        self._reset_result_dismissal()
        self.pause_code = ""
        self._error = ""
        self._input_blocked.clear()
        return True
```

工作线程在完成窗口刷新和前台复核、但调用视觉识别之前消费有效令牌并调用
`core.restart_round(now)`；成功后清空令牌、诊断冻结标志并发布 `automation.round_restarted`，然后
`continue` 到下一循环。`AutomationEngine.resume()` 保留 UI 接口，但运行事件改为
`automation.round_restart_requested`，不得再把 `observation` 传给核心。

- [ ] **Step 4: 运行引擎恢复与并发测试确认通过**

Run: `./.venv/Scripts/python.exe -m pytest try/tests/test_engine.py -q -k "resume or restart_round or late_window or aba or pause_cancels"`

Expected: PASS，错误进度画面零 A/D，计数保持不变。

- [ ] **Step 5: 提交**

```powershell
git add src/auto_fishing/automation/engine.py try/tests/test_engine.py
git commit -m "fix: make continue restart the incomplete round"
```

### Task 4: UI 明示新一轮语义

**Files:**
- Modify: `src/auto_fishing/ui/main_window.py`
- Test: `try/tests/test_ui_smoke.py`

**Interfaces:**
- Consumes: 现有 `controller.resume(activate=...)` 和 `resume_after_countdown(...)`
- Produces: 用户可见的“已放弃当前轮，正在开始新一轮”状态提示

- [ ] **Step 1: 写失败测试**

自动激活与手动三秒倒计时两条路径分别点击“继续”，断言只调用一次控制器恢复接口，并在请求成功后显示新一轮语义；错误返回仍显示具体错误而不是成功提示。

- [ ] **Step 2: 运行 UI 定向测试确认失败**

Run: `./.venv/Scripts/python.exe -m pytest try/tests/test_ui_smoke.py -q -k "resume or pause_or_resume"`

Expected: FAIL，当前成功提示为“无”。

- [ ] **Step 3: 修改最小 UI 文案**

在 `on_pause_or_resume()` 发起继续时设置“正在放弃当前轮并开始新一轮”；
`_on_resume_done(None)` 设置“已放弃当前轮，开始新一轮”。按钮文字仍为“继续”，布局尺寸不变。

- [ ] **Step 4: 运行 UI 测试确认通过**

Run: `./.venv/Scripts/python.exe -m pytest try/tests/test_ui_smoke.py -q -k "resume or pause_or_resume"`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/auto_fishing/ui/main_window.py try/tests/test_ui_smoke.py
git commit -m "fix: clarify continue starts a new round"
```

### Task 5: 回归、文档、构建和发布物

**Files:**
- Modify: `AGENTS.md`
- Modify: `doc/验收标准.md`
- Modify: `doc/进展记录/2026-7-14.md`
- Replace after backup: `异环自动钓鱼V2.exe`

**Interfaces:**
- Consumes: Tasks 1–4 的全部代码和测试
- Produces: 可直接分发的 V2 管理员单文件 EXE

- [ ] **Step 1: 运行定向和全量回归**

```powershell
./.venv/Scripts/python.exe -m pytest try/tests/test_state_machine.py try/tests/test_engine.py try/tests/test_ui_smoke.py -q
./.venv/Scripts/python.exe -m pytest try/tests -q
```

Expected: 全部 PASS；测试总数高于现有 395 项基线。

- [ ] **Step 2: 运行真实控制回放和静态检查**

运行固定的 412 帧控制回放：

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
$script | ./.venv/Scripts/python.exe -
```

Expected: 输出 `REAL_CONTROL_REPLAY_OK 412`，最大样本数不超过 15，动作只包含 `left/right/release`。

再运行：

```powershell
git diff --check
./.venv/Scripts/python.exe -m compileall -q src
```

Expected: 回放 PASS、无空白错误、源码可编译。

- [ ] **Step 3: 同步项目长期文档与验收记录**

`AGENTS.md` 增加 0.60 秒启用门、3 帧确认和继续重开语义；`doc/验收标准.md` 增加自动测试命令、诊断包时序证据和人工实机项；`doc/进展记录/2026-7-14.md` 记录精确到分钟的时间段、外部 C 盘诊断包只读路径、问题—原因—解决方案、文件清单和测试结果。

- [ ] **Step 4: 提交源码和文档检查点**

```powershell
git add AGENTS.md doc/验收标准.md doc/进展记录/2026-7-14.md
git commit -m "docs: record progress gate regression fix"
```

- [ ] **Step 5: 高风险替换前备份并构建**

把当前根目录 `异环自动钓鱼V2.exe` 复制到
`D:\0文件夹\备份\异环自动钓鱼-v2-progress-fix-<yyyyMMdd-HHmmss>\`，同类备份只保留最近 2 份；然后运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_v2.ps1
```

Expected: 全量测试再次通过，`dist/异环自动钓鱼V2.exe` 构建成功，清单验证包含
`requireAdministrator`、`uiAccess=false`、`PerMonitorV2` 和 `true/pm`。

- [ ] **Step 6: 提升会话烟雾与根目录替换**

```powershell
& ./try/smoke_exe.ps1 -TargetPath ./dist/异环自动钓鱼V2.exe
Copy-Item -LiteralPath ./dist/异环自动钓鱼V2.exe -Destination ./异环自动钓鱼V2.exe -Force
Get-FileHash ./dist/异环自动钓鱼V2.exe, ./异环自动钓鱼V2.exe -Algorithm SHA256
```

Expected: 烟雾 PASS；两个 SHA256 完全一致。若当前 PowerShell 未提升，烟雾明确记为阻塞并由用户人工运行，不得冒充通过。

- [ ] **Step 7: 完成验证、合并并清理分支**

按 `superpowers:verification-before-completion` 完成证据复核；提交必要发布记录后切换 `main`，使用
`git merge --ff-only codex/fix-progress-resume`，确认 `main` 干净，再删除本地任务分支。仓库为私有 GitHub，合并完成后询问用户是否推送，不擅自推送。

## 自检结果

- 规格覆盖：启用期、历史重置、连续三帧、时间戳边界、窄绿区、位置自由、重开计数、自动激活、并发令牌、诊断事件、构建和实机待确认均有对应任务。
- 占位扫描：未命中占位词、模糊实现步骤或未定义接口。
- 类型一致：状态机和核心均使用 `restart_round(now: float) -> bool`；UI 对外继续保留 `resume(activate: bool = False)`，内部语义由工作线程改为重开。
