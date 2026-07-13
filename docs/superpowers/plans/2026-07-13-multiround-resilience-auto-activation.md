# 多轮衔接、进度识别容错与自动切回游戏实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复轮间等待错误暂停，为仍有顶部槽结构的进度模糊提供 60 帧安全容错，并实现默认开启的一次绑定并开始及开始/继续自动切回游戏。

**Architecture:** 状态机只负责确定的轮间延迟，自动化核心用两个独立连续计数区分结构性模糊与完全丢失。`AutomationEngine` 通过显式 `activate` 参数封装前台激活，`AppController` 编排单次绑定并开始，`MainWindow` 只负责用户设置与交互状态。

**Tech Stack:** Python 3.13、Tkinter/ttk、pytest 9.1、Win32 ctypes、现有 PyInstaller 构建链。

## Global Constraints

- 不修改 3.10～3.60 秒结算点击、屏幕键盘尺寸、F/A/D 鼠标输入、上钩识别和进度中心控制算法。
- 无有效进度观测的第一帧必须释放 A/D；60 帧容错期间不得盲按。
- 自动激活仅能由用户直接点击“开始”或“继续”触发；绑定后的立即开始、轮间自动抛竿和 worker 不得调用 `SetForegroundWindow`。
- “自动切回游戏”默认开启，关闭后完整保留现有三秒手动切回流程。
- 初次绑定仍需用户在唯一一次三秒倒计时内手动切到游戏；程序不得猜测未绑定窗口。
- F8、输入失败、窗口失败和关闭必须继续释放所有输入并使迟到回调失效。
- 测试临时文件只写入 `try/`；发布物覆盖前备份到 `D:\0文件夹\备份`。
- 未完成真实 `2/2` 多轮验收前不得合并 `main` 或推送 GitHub。

## 文件职责与修改范围

- `src/auto_fishing/automation/state_machine.py`：移除轮间状态的通用错误超时语义。
- `src/auto_fishing/automation/engine.py`：双阈值进度丢失计数、显式开始/继续激活参数。
- `src/auto_fishing/app.py`：一次绑定并开始、倒计时取消和显式激活编排。
- `src/auto_fishing/ui/main_window.py`：按钮文案、自动切回复选框、开始/继续路径选择和设置保存。
- `src/auto_fishing/storage/settings.py`：严格布尔设置 `auto_activate_game`。
- `try/tests/test_state_machine.py`、`try/tests/test_engine.py`、`try/tests/test_storage.py`、`try/tests/test_ui_smoke.py`：每层确定性回归测试。
- `AGENTS.md`、`doc/验收标准.md`、`doc/进展记录/2026-7-13.md`：长期规则、验收证据和问题—原因—解决方案。

---

### Task 1: 修复轮间等待与第二轮自动抛竿

**Files:**
- Modify: `src/auto_fishing/automation/state_machine.py`
- Test: `try/tests/test_state_machine.py`
- Test: `try/tests/test_engine.py`

**Interfaces:**
- Consumes: `FishingStateMachine.check_interval(now: float) -> bool`、`AutomationCore.process(...)`。
- Produces: `INTER_ROUND` 满 1 秒后只迁移到 `READY`，下一帧由现有 `READY` 分支发送 F。

- [ ] **Step 1: 写入会复现真实错误的失败测试**

在 `try/tests/test_engine.py` 增加直接核心测试，要求刚超过一秒时不进入暂停：

```python
def test_inter_round_interval_precedes_generic_timeout() -> None:
    core, input_service, state_machine = make_core()
    core.start(2, 0.0)
    state_machine.handle(Event.CAST_SENT, 0.1)
    state_machine.handle(Event.REEL_SENT, 0.2)
    state_machine.handle(Event.BAR_DETECTED, 0.3)
    state_machine.handle(Event.BAR_GONE, 0.4)
    state_machine.handle(Event.RESULT_CLICKED, 1.0)

    core.process(SceneObservation(), None, 2.001, CLIENT)
    assert core.snapshot.state is FishingState.READY
    core.process(SceneObservation(), None, 2.002, CLIENT)
    assert core.snapshot.state is FishingState.WAIT_BITE
    assert input_service.events.count("F") == 1
```

在 `try/tests/test_state_machine.py` 增加约束：

```python
def test_inter_round_is_not_a_generic_timeout_state() -> None:
    assert FishingState.INTER_ROUND not in TIMEOUTS
```

- [ ] **Step 2: 运行测试并确认旧实现失败**

```powershell
py -3.13 -m pytest try/tests/test_state_machine.py try/tests/test_engine.py -q -k "inter_round"
```

预期：核心测试得到 `PAUSED/E_TIMEOUT`，超时表测试失败。

- [ ] **Step 3: 写入最小实现**

从 `TIMEOUTS` 删除轮间项：

```python
TIMEOUTS = {
    FishingState.READY: 3.0,
    FishingState.WAIT_BITE: 120.0,
    FishingState.WAIT_BAR: 8.0,
    FishingState.CONTROL: 120.0,
    FishingState.WAIT_RESULT: 10.0,
}
```

保留 `AutomationCore.process()` 末尾已有逻辑：

```python
elif (
    state is FishingState.INTER_ROUND
    and self.state_machine.check_interval(now)
):
    self.state_machine.handle(Event.INTERVAL_ELAPSED, now)
```

- [ ] **Step 4: 验证状态机、核心和线程生命周期**

```powershell
py -3.13 -m pytest try/tests/test_state_machine.py try/tests/test_engine.py -q
```

预期：全部通过，且现有完成/重启/暂停竞态测试不回归。

- [ ] **Step 5: 提交轮间修复**

```powershell
git add src/auto_fishing/automation/state_machine.py try/tests/test_state_machine.py try/tests/test_engine.py
git commit -m "fix: advance automatically between fishing rounds"
```

---

### Task 2: 实现结构性模糊 60 帧与完全丢失 6 帧双阈值

**Files:**
- Modify: `src/auto_fishing/automation/engine.py`
- Test: `try/tests/test_engine.py`

**Interfaces:**
- Consumes: `SceneObservation.progress_scanlines`、`progress_candidates`、`progress_rejection`。
- Produces: `structured_missing_frames: int` 与 `blank_missing_frames: int`；所有生命周期入口通过 `_reset_progress_tracking()` 清零。

- [ ] **Step 1: 写入双阈值失败测试**

在 `try/tests/test_engine.py` 定义真实日志同类观测并覆盖 59/60、恢复和类别切换：

```python
def structured_progress_ambiguity() -> SceneObservation:
    return SceneObservation(
        progress_scanlines=4,
        progress_candidates=8,
        progress_rejection="bar_too_narrow",
    )


def test_structured_progress_ambiguity_pauses_on_sixtieth_frame() -> None:
    core, input_service, _ = make_core(state=FishingState.CONTROL)
    missing = structured_progress_ambiguity()
    for index in range(59):
        core.process(missing, None, index / 30, CLIENT)
    assert core.snapshot.state is FishingState.CONTROL
    assert input_service.events[-1] == "release"

    core.process(missing, None, 59 / 30, CLIENT)
    assert core.snapshot.state is FishingState.PAUSED
    assert core.pause_code == "E_PROGRESS_LOST"
    assert "六十帧" in core.snapshot.error


def test_valid_progress_resets_structured_ambiguity_counter() -> None:
    core, _, _ = make_core(state=FishingState.CONTROL)
    missing = structured_progress_ambiguity()
    progress = ProgressObservation(0.3, 0.7, 0.5, 1.0, 0.0)
    for index in range(59):
        core.process(missing, None, index / 30, CLIENT)
    core.process(SceneObservation(progress=progress), None, 2.0, CLIENT)
    core.process(missing, None, 2.1, CLIENT)
    assert core.snapshot.state is FishingState.CONTROL
    assert core.structured_missing_frames == 1


def test_switching_missing_class_starts_blank_count_from_one() -> None:
    core, _, _ = make_core(state=FishingState.CONTROL)
    for index in range(59):
        core.process(structured_progress_ambiguity(), None, index / 30, CLIENT)
    core.process(SceneObservation(), None, 2.0, CLIENT)
    assert core.snapshot.state is FishingState.CONTROL
    assert core.blank_missing_frames == 1
    assert core.structured_missing_frames == 0
```

保留并调整现有普通零结构第 6 帧暂停、稳定后三帧干净消失测试。

- [ ] **Step 2: 运行测试确认当前统一六帧策略失败**

```powershell
py -3.13 -m pytest try/tests/test_engine.py -q -k "structured_progress or missing_bar or clean_missing or ambiguous_loss"
```

预期：结构性模糊在第 6 帧提前暂停，新增属性不存在。

- [ ] **Step 3: 实现独立计数与统一重置**

在模块顶部增加：

```python
_STRUCTURED_PROGRESS_LOSS_LIMIT = 60
_BLANK_PROGRESS_LOSS_LIMIT = 6
```

在核心构造、开始、恢复和取消路径使用：

```python
def _reset_progress_tracking(self) -> None:
    self.structured_missing_frames = 0
    self.blank_missing_frames = 0
```

将 `_control()` 的无观测分支改为：

```python
self._input(self.input_service.release_all)
has_structure = (
    observation.progress_scanlines > 0
    or observation.progress_candidates > 0
)
if has_structure:
    self.structured_missing_frames += 1
    self.blank_missing_frames = 0
    if self.structured_missing_frames >= _STRUCTURED_PROGRESS_LOSS_LIMIT:
        self.pause(
            "连续六十帧进度条结构不稳定",
            now,
            code="E_PROGRESS_LOST",
        )
    return

self.blank_missing_frames += 1
self.structured_missing_frames = 0
clean_disappearance = (
    observation.progress_rejection == "yellow_missing"
)
if (
    self.bar_valid_frames >= 15
    and self.blank_missing_frames >= 3
    and clean_disappearance
):
    self.bar_valid_frames = 0
    self._enter_wait_result(now)
    return
if self.blank_missing_frames >= _BLANK_PROGRESS_LOSS_LIMIT:
    self.pause("连续六帧未识别进度条", now, code="E_PROGRESS_LOST")
```

有效进度观测先调用 `_reset_progress_tracking()`，再更新 `bar_valid_frames` 和方向。

- [ ] **Step 4: 验证所有进度与结算切换测试**

```powershell
py -3.13 -m pytest try/tests/test_engine.py try/tests/test_progress.py try/tests/test_scenes.py -q
```

预期：双阈值、第一帧释放、干净结束、不同分辨率窄区和结算计时全部通过。

- [ ] **Step 5: 提交双阈值**

```powershell
git add src/auto_fishing/automation/engine.py try/tests/test_engine.py
git commit -m "fix: tolerate structured progress ambiguity"
```

---

### Task 3: 增加严格布尔设置与复选框

**Files:**
- Modify: `src/auto_fishing/storage/settings.py`
- Modify: `src/auto_fishing/ui/main_window.py`
- Test: `try/tests/test_storage.py`
- Test: `try/tests/test_ui_smoke.py`

**Interfaces:**
- Produces: `AppSettings.auto_activate_game: bool = True`、`MainWindow.auto_activate_var: tk.BooleanVar`。
- Consumes: Task 4/5 通过 `bool(self.auto_activate_var.get())` 选择自动或手动路径。

- [ ] **Step 1: 写入设置与 UI 失败测试**

```python
def test_settings_auto_activate_defaults_true_and_round_trips(tmp_path) -> None:
    path = tmp_path / "config.json"
    store = SettingsStore(path)
    assert store.load().auto_activate_game is True
    store.save(AppSettings(auto_activate_game=False))
    assert store.load().auto_activate_game is False


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_settings_reject_non_boolean_auto_activate(tmp_path, value) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"auto_activate_game": value}), "utf-8")
    assert SettingsStore(path).load().auto_activate_game is True
```

在 UI 测试断言复选框默认选中、设置为假时未选中、关闭后保存当前值。

- [ ] **Step 2: 运行测试确认字段和控件不存在**

```powershell
py -3.13 -m pytest try/tests/test_storage.py try/tests/test_ui_smoke.py -q -k "settings or auto_activate"
```

- [ ] **Step 3: 实现严格布尔设置**

```python
@dataclass(frozen=True)
class AppSettings:
    target_count: int = 1
    window_x: int = 20
    window_y: int = 20
    auto_activate_game: bool = True


def _strict_bool(value: object, default: bool = True) -> bool:
    return value if isinstance(value, bool) else default
```

`load()` 用关键字参数构造，避免新增字段破坏位置参数；`save()` 继续使用 `asdict()`。

- [ ] **Step 4: 增加复选框并保存**

在 `MainWindow.__init__` 创建：

```python
self.auto_activate_var = tk.BooleanVar(
    master=root,
    value=self.settings.auto_activate_game,
)
```

在按钮框第二行中间增加：

```python
self.auto_activate_check = ttk.Checkbutton(
    buttons,
    text="自动切回游戏",
    variable=self.auto_activate_var,
)
self.auto_activate_check.grid(row=1, column=1, padx=2, pady=3, sticky="w")
```

关闭保存增加：

```python
auto_activate_game=bool(self.auto_activate_var.get()),
```

- [ ] **Step 5: 验证设置和窗口测试并提交**

```powershell
py -3.13 -m pytest try/tests/test_storage.py try/tests/test_ui_smoke.py -q
git add src/auto_fishing/storage/settings.py src/auto_fishing/ui/main_window.py try/tests/test_storage.py try/tests/test_ui_smoke.py
git commit -m "feat: persist automatic game activation setting"
```

---

### Task 4: 为开始与继续增加显式自动激活

**Files:**
- Modify: `src/auto_fishing/automation/engine.py`
- Modify: `src/auto_fishing/app.py`
- Test: `try/tests/test_engine.py`
- Test: `try/tests/test_ui_smoke.py`

**Interfaces:**
- Produces: `AutomationEngine.start(target: int, *, activate: bool = False) -> None`、`resume(*, activate: bool = False) -> None`。
- Produces: `AppController.start(target: int, *, activate: bool = False) -> None`、`resume(*, activate: bool = False) -> None`。
- Consumes: `WindowService.activate(bound) -> bool`。

- [ ] **Step 1: 写入自动激活成功/失败测试**

在 `try/tests/test_engine.py` 增加：

```python
def test_engine_start_explicit_activation_switches_before_start(tmp_path) -> None:
    window = ActivatingWindowService([])
    engine, core, input_service, _, _ = make_engine(
        tmp_path, window_service=window
    )
    engine.start(1, activate=True)
    try:
        wait_until(lambda: core.snapshot.state is FishingState.WAIT_BITE)
        assert window.activate_calls == 1
        assert input_service.events.count("F") == 1
    finally:
        engine.shutdown()


def test_engine_start_activation_failure_sends_no_input(tmp_path) -> None:
    window = ActivatingWindowService([])
    window.activate_succeeds = False
    engine, _, input_service, _, _ = make_engine(
        tmp_path, window_service=window
    )
    with pytest.raises(RuntimeError, match="自动切换到游戏失败"):
        engine.start(1, activate=True)
    assert not engine.is_running
    assert "F" not in input_service.events
```

为暂停后的 `resume(activate=True)` 增加同类成功与失败断言，失败时 `_resume_request is None`。

- [ ] **Step 2: 运行测试确认签名不接受 activate**

```powershell
py -3.13 -m pytest try/tests/test_engine.py -q -k "activation"
```

- [ ] **Step 3: 在引擎中实现显式激活边界**

```python
def _activate_bound(self, bound: Any) -> None:
    self._runtime_event("window.activation_requested", hwnd=bound.hwnd)
    try:
        activated = bool(self.window_service.activate(bound))
        foreground = bool(self.window_service.is_foreground(bound))
    except Exception as error:
        self._runtime_event(
            "window.activation_result", success=False, detail=str(error)
        )
        raise RuntimeError(
            "自动切换到游戏失败，请关闭自动切回或手动切到游戏后重试"
        ) from error
    success = activated and foreground
    self._runtime_event("window.activation_result", success=success)
    if not success:
        raise RuntimeError(
            "自动切换到游戏失败，请关闭自动切回或手动切到游戏后重试"
        )
```

`start()` 在现有前台检查前按需调用；`resume()` 在创建恢复令牌前按需调用。默认 `activate=False`，保留现有直接引擎测试“不调用 activate”的语义。

- [ ] **Step 4: 让控制器透传显式参数**

```python
def start(self, target: int, *, activate: bool = False) -> None:
    ...
    self.engine.start(target, activate=activate)


def resume(self, *, activate: bool = False) -> None:
    if not self._begin_command():
        return
    try:
        self.engine.resume(activate=activate)
    finally:
        self._finish_command()
```

- [ ] **Step 5: 验证引擎并提交**

```powershell
py -3.13 -m pytest try/tests/test_engine.py try/tests/test_ui_smoke.py -q
git add src/auto_fishing/automation/engine.py src/auto_fishing/app.py try/tests/test_engine.py try/tests/test_ui_smoke.py
git commit -m "feat: activate game for explicit start and resume"
```

---

### Task 5: 实现一次绑定并开始及 UI 路径选择

**Files:**
- Modify: `src/auto_fishing/app.py`
- Modify: `src/auto_fishing/ui/main_window.py`
- Test: `try/tests/test_ui_smoke.py`

**Interfaces:**
- Produces: `AppController.bind_and_start_after_countdown(target, on_tick, on_done) -> None`。
- Consumes: Task 3 的 `auto_activate_var`，Task 4 的 `start(..., activate=True)` 与 `resume(activate=True)`。

- [ ] **Step 1: 写入绑定并开始与 UI 失败测试**

控制器测试使用同步调度器推进 3、2、1、0，断言：

```python
def test_bind_and_start_uses_one_countdown_and_starts_without_activation() -> None:
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    window = BindingService()
    controller = AppController(engine, window, scheduler)
    ticks: list[int] = []
    done: list[tuple[str | None, str | None]] = []
    controller.bind_and_start_after_countdown(
        2,
        ticks.append,
        lambda title, error: done.append((title, error)),
    )
    assert ticks == [3]
    scheduler.run_next(1000)
    scheduler.run_next(1000)
    scheduler.run_next(1000)
    assert window.calls == 1
    assert engine.calls == [
        ("bind", window.bound),
        ("start", 2, False),
    ]
    assert done == [("异环", None)]
```

Task 4 同步把 `BridgeEngine.start/resume` 测试替身改为接受关键字参数并记录：

```python
def start(self, target: int, *, activate: bool = False) -> None:
    self.calls.append(("start", target, activate))


def resume(self, *, activate: bool = False) -> None:
    self.calls.append(("resume", activate))
```

UI 测试断言按钮文字为“绑定并开始”，点击后仅调用组合方法；自动设置开启时开始/继续调用立即路径和 `activate=True`，关闭时调用原倒计时路径。

- [ ] **Step 2: 运行测试确认组合接口不存在**

```powershell
py -3.13 -m pytest try/tests/test_ui_smoke.py -q -k "bind_and_start or auto_activate or start_countdown or resume_countdown"
```

- [ ] **Step 3: 在控制器中实现单次倒计时组合命令**

复用 `_start_binding_countdown` 的窗口绑定部分，但让成功回调执行：

```python
bound = self.window_service.bind_foreground()
self.engine.bind(bound)
self.engine.start(target, activate=False)
```

新增 `_pending_bind_start_done: BindDone | None`，F8 的 `_cancel_pending_countdowns_for_pause()` 同时清除它、递增 generation，并向 UI 投递“绑定并开始倒计时已被紧急暂停取消”。绑定成功但 `engine.start()` 失败时保留 `_last_bound_title` 并把错误返回 UI。

- [ ] **Step 4: 在窗口中选择立即或手动路径**

按钮和绑定入口：

```python
self.bind_button.configure(text="绑定并开始")
```

`on_bind()` 先验证目标，再调用组合方法。`on_start()` 中：

```python
if self.auto_activate_var.get():
    try:
        self.controller.start(target, activate=True)
    except Exception as error:
        self._on_start_done(str(error))
    else:
        self._on_start_done(None)
    return
self.controller.start_after_countdown(
    target, self._on_start_tick, self._on_start_done
)
```

暂停后的继续使用相同选择：开启时 `controller.resume(activate=True)`，关闭时调用 `resume_after_countdown()`。立即路径调用期间锁定按钮，完成或失败后必须恢复状态。

- [ ] **Step 5: 验证 UI、倒计时、F8 和关闭竞态**

```powershell
py -3.13 -m pytest try/tests/test_ui_smoke.py -q
```

预期：绑定并开始只有一次三秒序列；F8 取消后不会迟到绑定/启动；自动激活失败保持可重试；原手动倒计时测试继续通过。

- [ ] **Step 6: 提交交互实现**

```powershell
git add src/auto_fishing/app.py src/auto_fishing/ui/main_window.py try/tests/test_ui_smoke.py
git commit -m "feat: bind and start in one interaction"
```

---

### Task 6: 文档、全量验证与发布物

**Files:**
- Modify: `AGENTS.md`
- Modify: `doc/验收标准.md`
- Modify: `doc/进展记录/2026-7-13.md`
- Generate ignored artifact: `dist/异环自动钓鱼.exe`

**Interfaces:**
- Consumes: Task 1～5 的最终行为与测试证据。
- Produces: 可人工验证的新 EXE、回退备份、哈希和未通过项清单。

- [ ] **Step 1: 运行全量测试和差异检查**

```powershell
py -3.13 -m pytest try/tests -q
git diff --check
```

预期：全部测试通过，差异检查无错误。

- [ ] **Step 2: 更新长期规则和验收记录**

`AGENTS.md` 必须记录：轮间不属于通用超时、结构性模糊 60 帧/完全丢失 6 帧、自动切回默认开启及只有直接开始/继续能抢焦点。

`doc/验收标准.md` 必须新增可验证项：目标 2 自动进入第二轮、59/60 帧边界、零结构 6 帧、绑定并开始一次倒计时、设置开关两条路径和自动激活失败零输入。

`doc/进展记录/2026-7-13.md` 必须写入本轮真实日志证据、问题—原因—解决方案、修改文件、测试命令、构建哈希、工作区外备份和技术债检查。

- [ ] **Step 3: 提交文档检查点**

```powershell
git add AGENTS.md doc/验收标准.md doc/进展记录/2026-7-13.md
git commit -m "docs: record multiround resilience changes"
```

- [ ] **Step 4: 备份当前发布物并核对哈希**

将 `dist/异环自动钓鱼.exe` 复制到：

```text
D:\0文件夹\备份\异环自动钓鱼-multiround-auto-activation-prebuild-<YYYYMMDD-HHmm>\异环自动钓鱼.exe
```

使用 `Get-FileHash -Algorithm SHA256` 核对源与备份一致后才构建。

- [ ] **Step 5: 构建、清单校验和烟雾**

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build.ps1 -PythonPath "C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe"
& .\try\smoke_exe.ps1
```

预期：构建脚本再次全量通过，输出 `RELEASE_MANIFEST_OK requireAdministrator uiAccess=false`；烟雾输出 `SMOKE_OK` 且不残留本次拥有的进程。

- [ ] **Step 6: 完成前验证与交付**

```powershell
py -3.13 -m pytest try/tests/test_state_machine.py try/tests/test_engine.py try/tests/test_storage.py try/tests/test_ui_smoke.py -q
py -3.13 scripts/verify_release.py "dist\异环自动钓鱼.exe"
git status --porcelain
```

记录 EXE 大小和 SHA256。明确人工待验：一次绑定并开始、自动开始/继续切窗、高难度结构性模糊恢复、目标 2 自动完成。人工通过前保持当前分支，不合并 `main`、不推送 GitHub。
