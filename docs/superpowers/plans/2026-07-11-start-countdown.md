# “开始”倒计时与前台确认实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 点击“开始”后提供 3 秒手动切回游戏的倒计时，并在游戏确实位于前台时才启动自动钓鱼。

**Architecture:** `MainWindow` 负责展示倒计时和锁定控件，`AppController` 复用现有调度器及代际取消机制延迟调用引擎，`AutomationEngine` 只验证已绑定游戏是否处于前台，不再尝试强制抢占焦点。暂停后的继续流程保持原有主动激活行为。

**Tech Stack:** Python 3.13、Tkinter/ttk、Win32 ctypes、pytest、PyInstaller。

## Global Constraints

- 开始倒计时固定为 3 秒，按 3、2、1、0 调度。
- 不使用线程输入附加、模拟 Alt 键、管理员权限或游戏进程注入。
- 倒计时结束前不得启动截图、worker 或发送输入。
- 关闭、重复绑定或生命周期代际变化后，迟到回调不得启动引擎。
- 暂停后的继续仍保留主动激活与前台复核。
- 所有测试文件继续放在 `try/`，发布物继续为 `dist/异环自动钓鱼.exe`。

## 文件结构

- `src/auto_fishing/automation/engine.py`：开始时只做前台验证，保留运行期和恢复期窗口安全逻辑。
- `src/auto_fishing/app.py`：拥有异步开始倒计时、命令计数、关闭取消和完成回调。
- `src/auto_fishing/ui/main_window.py`：展示开始倒计时、锁定控件、呈现错误并恢复可操作状态。
- `try/tests/test_engine.py`：验证开始阶段不强制激活及前台失败可重试。
- `try/tests/test_ui_smoke.py`：验证 Controller 与 UI 的倒计时、取消和控件状态。
- `AGENTS.md`、`doc/验收标准.md`、`doc/进展记录/2026-7-11.md`：同步长期规则、验收证据和阶段记录。

---

### Task 1: 引擎开始阶段只验证前台

**Files:**
- Modify: `src/auto_fishing/automation/engine.py:373-410`
- Test: `try/tests/test_engine.py:1181-1200`

**Interfaces:**
- Consumes: `WindowService.is_foreground(bound: BoundWindow) -> bool`
- Produces: `AutomationEngine.start(target: int) -> None` 在游戏已处于前台时启动；否则同步抛出可重试错误，且不调用 `activate()`。

- [ ] **Step 1: 将现有启动激活测试改为两个失败测试**

将 `test_engine_start_activates_game_before_worker_checks_foreground` 替换为：

```python
def test_engine_start_checks_foreground_without_forcing_activation(tmp_path) -> None:
    window_service = RecordingWindowService()
    engine, core, _input, _window, _source = make_engine(
        tmp_path,
        window_service=window_service,
    )

    engine.start(1)
    try:
        wait_until(lambda: core.snapshot.state is FishingState.WAIT_BITE)
        assert window_service.activate_calls == 1
    finally:
        engine.shutdown()


def test_engine_start_rejects_background_game_without_worker(tmp_path) -> None:
    window_service = RecordingWindowService()
    window_service.foreground = False
    engine, core, _input, _window, source = make_engine(
        tmp_path,
        window_service=window_service,
    )

    with pytest.raises(
        RuntimeError,
        match="请在倒计时结束前切回已绑定的游戏窗口",
    ):
        engine.start(1)

    assert window_service.activate_calls == 0
    assert source.started == []
    assert engine.is_running is False
    assert core.snapshot.state is FishingState.UNBOUND
```

说明：第一个测试中的一次 `activate_calls` 来自进入 `READY` 后发送第一次 F 的既有 `AutomationCore.activate_game` 安全调用；断言事件顺序时不得把它误认为开始阶段的强制切换。

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest try/tests/test_engine.py -q -k "start_checks_foreground_without_forcing_activation or start_rejects_background_game_without_worker"
```

Expected: 2 项 FAIL；前台测试观察到开始阶段多调用一次 `activate()`，背景游戏测试得到旧的激活失败提示而不是新的倒计时提示。

- [ ] **Step 3: 最小修改 `AutomationEngine.start`**

用以下前台验证替换当前 `activate()` 与二次 `is_foreground()` 逻辑：

```python
        try:
            foreground = bool(self.window_service.is_foreground(bound))
        except Exception as error:
            raise RuntimeError(f"无法确认游戏窗口前台状态: {error}") from error
        if not foreground:
            raise RuntimeError("请在倒计时结束前切回已绑定的游戏窗口")
```

保留验证后的生命周期锁复核：

```python
        with self._lifecycle_lock:
            if self._bound is not bound:
                raise RuntimeError("绑定窗口已变化，请重新开始")
            if self.is_running or self._starting or self._cancelling:
                raise RuntimeError("自动化已在运行")
            if self._shutdown_started and not self._cleanup_done.is_set():
                raise RuntimeError("自动化仍在关闭中")
```

- [ ] **Step 4: 运行聚焦与引擎全套测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest try/tests/test_engine.py -q
```

Expected: 全部 PASS；运行期 `E_WINDOW`、恢复主动激活和 epoch 竞态测试保持通过。

- [ ] **Step 5: 提交引擎契约修改**

```powershell
git add src/auto_fishing/automation/engine.py try/tests/test_engine.py
git commit -m "fix: verify game foreground before start"
```

---

### Task 2: Controller 与 UI 增加开始倒计时

**Files:**
- Modify: `src/auto_fishing/app.py:12-205`
- Modify: `src/auto_fishing/ui/main_window.py:10-224`
- Test: `try/tests/test_ui_smoke.py:12-110`
- Test: `try/tests/test_ui_smoke.py:226-495`

**Interfaces:**
- Consumes: `Scheduler = Callable[[int, Callable[[], None]], Any]`、`AutomationEngine.start(target: int) -> None`
- Produces: `AppController.start_after_countdown(target: int, on_tick: Callable[[int], None], on_done: Callable[[str | None], None]) -> None`
- Produces: `MainWindow._on_start_tick(seconds: int) -> None` 与 `MainWindow._on_start_done(error: str | None) -> None`

- [ ] **Step 1: 添加 Controller 倒计时失败测试**

在 `try/tests/test_ui_smoke.py` 增加：

```python
def test_controller_counts_down_before_starting_engine() -> None:
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    controller = AppController(engine, BindingService(), scheduler)
    ticks: list[int] = []
    completed: list[str | None] = []

    controller.start_after_countdown(4, ticks.append, completed.append)

    assert ticks == [3]
    assert ("start", 4) not in engine.calls
    for _ in range(2):
        scheduler.run_next(1000)
    assert ticks == [3, 2, 1]
    assert ("start", 4) not in engine.calls
    scheduler.run_next(1000)

    assert engine.calls == [("start", 4)]
    assert completed == [None]


def test_shutdown_cancels_pending_start_countdown() -> None:
    scheduler = ManualScheduler()
    engine = BridgeEngine()
    controller = AppController(engine, BindingService(), scheduler)
    completed: list[str | None] = []

    controller.start_after_countdown(2, lambda _seconds: None, completed.append)
    controller.shutdown()
    while scheduler.pending:
        scheduler.run_next()

    assert ("start", 2) not in engine.calls
    assert completed == []
    assert engine.calls[-1] == "shutdown"
```

- [ ] **Step 2: 添加 UI 倒计时失败测试并更新 FakeController**

为 `FakeController` 增加：

```python
        self.start_callbacks = None

    def start_after_countdown(self, target, on_tick, on_done) -> None:
        self.calls.append(("start_after_countdown", target))
        self.start_callbacks = (on_tick, on_done)
```

将窗口启动断言改为倒计时入口，并新增状态检查：

```python
    window.on_start()
    assert controller.calls == [("start_after_countdown", 3)]
    on_tick, on_done = controller.start_callbacks
    for seconds in (3, 2, 1):
        on_tick(seconds)
        assert window.state_var.get() == f"开始倒计时：{seconds}"
        assert window.start_button.instate(["disabled"])
        assert window.count_spinbox.instate(["disabled"])
    on_done("请在倒计时结束前切回已绑定的游戏窗口")
    assert window.error_var.get() == "请在倒计时结束前切回已绑定的游戏窗口"
    assert not window.start_button.instate(["disabled"])
```

- [ ] **Step 3: 运行 UI 测试并确认 RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest try/tests/test_ui_smoke.py -q -k "counts_down_before_starting_engine or shutdown_cancels_pending_start_countdown or window_is_topmost_and_validates_count"
```

Expected: FAIL，原因是 `start_after_countdown`、开始回调和 UI 文本尚不存在。

- [ ] **Step 4: 在 `AppController` 实现共享倒计时门控**

新增类型：

```python
StartTick = Callable[[int], None]
StartDone = Callable[[str | None], None]
```

把 `_binding` 替换为 `_countdown_active`，绑定与开始入口都必须在 `_command_condition` 下检查并设置该标志。新增方法：

```python
    def start_after_countdown(
        self,
        target: int,
        on_tick: StartTick,
        on_done: StartDone,
    ) -> None:
        with self._command_condition:
            if self._closed:
                return
            if self._countdown_active:
                on_done("倒计时正在进行")
                return
            self._countdown_active = True
            self._countdown_generation += 1
            generation = self._countdown_generation

        def advance(seconds: int) -> None:
            with self._command_condition:
                if self._closed or generation != self._countdown_generation:
                    return
                if seconds > 0:
                    on_tick(seconds)
                    self.schedule(1000, lambda: advance(seconds - 1))
                    return
                self._countdown_active = False
                self._starting = True
                self._active_commands += 1

            error_message: str | None = None
            try:
                self.engine.start(target)
            except Exception as error:
                error_message = str(error)
            finally:
                with self._command_condition:
                    self._starting = False
                self._finish_command()
            on_done(error_message)

        try:
            advance(3)
        except BaseException:
            with self._command_condition:
                self._countdown_active = False
                self._countdown_generation += 1
            raise
```

`_start_binding_countdown()` 使用相同 `_countdown_active` 门控；其成功、失败和异常路径必须清除标志。`shutdown()` 将标志设为 `False` 并递增代际。

- [ ] **Step 5: 在 `MainWindow` 接入开始倒计时**

将 `on_start()` 的直接调用替换为：

```python
        self._countdown_active = True
        self.error_var.set("无")
        self._refresh_control_states()
        try:
            self.controller.start_after_countdown(
                target,
                self._on_start_tick,
                self._on_start_done,
            )
        except Exception as error:
            self._on_start_done(str(error))
```

新增回调：

```python
    def _on_start_tick(self, seconds: int) -> None:
        self.state_var.set(f"开始倒计时：{seconds}")

    def _on_start_done(self, error: str | None) -> None:
        self._countdown_active = False
        self.state_var.set(self._state.value)
        if error:
            self.error_var.set(error)
        self._refresh_control_states()
```

在 `_refresh_control_states()` 中让次数输入在 `_countdown_active or _runtime_active` 时禁用。

- [ ] **Step 6: 更新所有直接开始与关闭回归测试**

- `test_failed_rebind_preserves_old_binding_and_can_start`：执行三次 `scheduler.run_next(1000)` 后再断言 `("start", 2)`。
- `test_controller_bridges_engine_commands_and_cancels_countdown_on_shutdown`：保留 `controller.start(4)` 对同步内部入口的覆盖，并新增的异步测试单独验证倒计时。
- `test_controller_ignores_all_commands_after_shutdown`：增加 `start_after_countdown` 调用并断言无 tick、无完成回调。
- `test_close_saves_position_and_shuts_down_controller`：若存在待开始倒计时，关闭后调度回调不得启动。

- [ ] **Step 7: 运行 UI 与全套测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest try/tests/test_ui_smoke.py -q
.\.venv\Scripts\python.exe -m pytest try/tests -q
```

Expected: 全部 PASS；测试总数为 207 项。

- [ ] **Step 8: 提交倒计时交互**

```powershell
git add src/auto_fishing/app.py src/auto_fishing/ui/main_window.py try/tests/test_ui_smoke.py
git commit -m "fix: start after manual foreground countdown"
```

---

### Task 3: 文档、构建与交付验证

**Files:**
- Modify: `AGENTS.md`
- Modify: `doc/验收标准.md`
- Modify: `doc/进展记录/2026-7-11.md`
- Generated: `dist/异环自动钓鱼.exe`（Git 忽略）

**Interfaces:**
- Consumes: Tasks 1-2 的完整代码与测试结果。
- Produces: 与源码提交一致的单文件 EXE、哈希、烟雾证据和人工复测步骤。

- [ ] **Step 1: 更新长期规则与验收项**

在 `AGENTS.md` 将开始数据流更新为：

```text
绑定游戏窗口 → 点击开始并进行 3 秒手动切回游戏倒计时 → 验证游戏前台 → 获取客户区与显示器 → 30 帧/秒截图
```

在 `doc/验收标准.md` 记录：

- 开始倒计时期间无截图、worker 和输入。
- 前台成功后进入等待上钩；失败显示可重试提示。
- 最新 pytest 数量、构建结果、烟雾结果、大小和 SHA256。

在 `doc/进展记录/2026-7-11.md` 记录问题—原因—解决方案、修改文件、RED/GREEN、构建备份和人工待确认项。

- [ ] **Step 2: 构建前备份旧 EXE**

使用以下命令生成带实际分钟时间戳的唯一备份目录：

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$backupDir = "D:\0文件夹\备份\异环自动钓鱼-start-countdown-prebuild-$stamp"
New-Item -ItemType Directory -Path $backupDir
Copy-Item -LiteralPath 'dist\异环自动钓鱼.exe' -Destination $backupDir
```

记录旧文件大小和 SHA256，确认备份一致后再覆盖构建。

- [ ] **Step 3: 运行完整构建门与烟雾**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build.ps1
powershell -ExecutionPolicy Bypass -File try/smoke_exe.ps1
```

Expected: 构建门内全套测试通过，PyInstaller 退出码 0，烟雾输出 `SMOKE_OK`，残留进程数为 0。

- [ ] **Step 4: 核对并复制交付文件**

计算工作树 `dist/异环自动钓鱼.exe` 的大小和 SHA256，复制到：

```text
D:\1Folder\异环自动钓鱼\dist\异环自动钓鱼.exe
```

重新计算交付文件哈希并断言两者完全一致。

- [ ] **Step 5: 最终验证并提交文档**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest try/tests -q
git diff --check "$(git merge-base main HEAD)..HEAD"
git status --short --branch
```

Expected: 全套测试通过、差异检查无输出；提交后工作树干净。

```powershell
git add AGENTS.md 'doc/验收标准.md' 'doc/进展记录/2026-7-11.md'
git commit -m "docs: record start countdown verification"
```

- [ ] **Step 6: 人工验收交接**

要求用户按以下顺序复测：绑定游戏 → 点击开始 → 3 秒内点击游戏 → 确认第一次 F；再测试一次故意不切回游戏，确认无按键且提示可重试。真实游戏通过前继续保留任务分支，不合并 `main`。
