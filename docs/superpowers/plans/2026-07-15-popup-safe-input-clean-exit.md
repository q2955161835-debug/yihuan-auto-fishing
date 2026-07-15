# Windows 弹窗输入安全与干净退出实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Windows 系统弹窗或控制窗口抢占前台后，不再产生新的 F/A/D/结算点击；所有已按下输入仍立即释放，并让正常退出不再显示未处理异常。

**Architecture:** 在 `SafeInput`（安全输入）真正发出新按下或点击前增加可动态更新的目标窗口守卫，`AutomationEngine`（自动化引擎）在绑定后把守卫连接到当前游戏前台校验。前台丢失继续走安全暂停；控制窗口抢占按用户操作暂停且不生成错误包，其他窗口（含 Windows 系统弹窗）按 `E_WINDOW` 记录。退出路径先停止控制器，再保存设置；清理失败写入 `E_CLEANUP` 诊断而不作为窗口版 EXE 的未处理异常重新抛出；已经消失的屏幕键盘句柄按幂等关闭处理。

**Tech Stack:** Python 3.13、Tkinter、Win32 ctypes、pytest、PyInstaller。

## Global Constraints

- 游戏不是前台时不得产生新的 F/A/D 按下或结算点击，但释放 A/D/F/鼠标必须始终允许。
- Windows 弹窗出现后不得自动恢复；安全暂停后由用户关闭弹窗并点击“继续”，继续仍放弃当前未完成轮并从第一 F 重开。
- 控制窗口取得前台属于用户交互，必须安全暂停且不生成自动错误诊断。
- V2 正常退出不得显示 PyInstaller 未处理异常；真实清理失败必须保留 `E_CLEANUP` 诊断证据。
- 本程序启动的屏幕键盘若已被 Windows 关闭，后续 `close()` 必须成功返回；错误 5 仍按现有策略安全保留系统屏幕键盘。
- 不改变识别阈值、状态机控制公式、结算计时或诊断保留上限。

---

### Task 1: 每次新输入前重新确认游戏前台

**Files:**
- Modify: `src/auto_fishing/platform/input.py`
- Modify: `src/auto_fishing/automation/engine.py`
- Modify: `src/auto_fishing/platform/windowing.py`
- Test: `try/tests/test_safe_input.py`
- Test: `try/tests/test_engine.py`
- Test: `try/tests/test_capture_window.py`

**Interfaces:**
- Consumes: `WindowService.is_foreground(bound: BoundWindow) -> bool`、`WindowService.own_hwnd`。
- Produces: `InputTargetUnavailable`、`SafeInput.set_target_guard(guard)`、`WindowService.is_control_foreground() -> bool`。

- [ ] **Step 1: 写 F 延迟期间被弹窗抢焦点的失败测试**

```python
def test_target_guard_blocks_f_when_focus_is_lost_during_pre_press_delay():
    focus = {"game": True}
    backend = FakeBackend()
    safe = SafeInput(
        backend,
        sleep=lambda _seconds: focus.__setitem__("game", False),
        random_uniform=lambda _lower, _upper: 0.10,
    )
    safe.set_target_guard(lambda: focus["game"])

    with pytest.raises(InputTargetUnavailable, match="Windows 系统弹窗"):
        safe.tap_f()

    assert backend.events == []
```

- [ ] **Step 2: 写失焦后仍允许释放的失败测试**

```python
def test_target_guard_never_blocks_release_after_focus_loss():
    focus = {"game": True}
    backend = FakeBackend()
    safe = SafeInput(backend, sleep=lambda _: None)
    safe.set_target_guard(lambda: focus["game"])
    safe.set_direction(Direction.LEFT)
    focus["game"] = False

    safe.release_all()

    assert backend.events == [("down", "A"), ("up", "A")]
```

- [ ] **Step 3: 运行定向测试并确认按缺失行为失败**

Run: `py -3.13 -m pytest try/tests/test_safe_input.py -q`

Expected: FAIL，`SafeInput` 尚无 `set_target_guard` / `InputTargetUnavailable`。

- [ ] **Step 4: 实现最小输入守卫**

```python
class InputTargetUnavailable(InputFailure):
    pass

def set_target_guard(self, guard: Callable[[], bool] | None) -> None:
    self._target_guard = guard

def _ensure_target_available(self) -> None:
    if self._target_guard is None:
        return
    try:
        available = bool(self._target_guard())
    except Exception as error:
        raise InputTargetUnavailable(
            f"无法确认游戏窗口前台状态: {error}"
        ) from error
    if not available:
        raise InputTargetUnavailable(
            "游戏窗口已失去前台，可能被 Windows 系统弹窗或其他窗口遮挡"
        )
```

`_down()` 每次调用（包括按键已处于 held 集合）先调用守卫；`click()` 在记录及物理点击前调用守卫；`_up()`、`release_all()` 和 `mouse_up()` 不调用守卫。

- [ ] **Step 5: 写引擎绑定守卫及错误分类失败测试**

```python
class GuardedRecordingInput(RecordingInput):
    def __init__(self):
        super().__init__()
        self.target_guard = None

    def set_target_guard(self, guard):
        self.target_guard = guard


class ControlForegroundAfterStartWindowService(
    ForegroundDropsAfterStartWindowService
):
    def is_control_foreground(self) -> bool:
        return True


def test_bound_input_guard_maps_system_popup_to_window_pause(tmp_path):
    input_service = GuardedRecordingInput()
    window_service = RecordingWindowService()
    make_engine(
        tmp_path,
        input_service=input_service,
        window_service=window_service,
    )
    assert input_service.target_guard is not None
    assert input_service.target_guard() is True
    window_service.foreground = False
    assert input_service.target_guard() is False

def test_control_window_foreground_is_user_pause_without_diagnostic(tmp_path):
    reporter = RecordingReporter()
    window_service = ControlForegroundAfterStartWindowService()
    engine, core, _input, _window, _source = make_engine(
        tmp_path,
        window_service=window_service,
        diagnostic_reporter=reporter,
    )
    engine.start(1)
    wait_until(lambda: core.snapshot.state is FishingState.PAUSED)
    engine.shutdown()
    assert core.pause_code == "E_USER_PAUSE"
    assert "控制窗口" in core.snapshot.error
    assert reporter.requests == []
```

- [ ] **Step 6: 在窗口与引擎边界接线**

```python
def is_control_foreground(self) -> bool:
    foreground = int(self.user32.GetForegroundWindow() or 0)
    return self.own_hwnd is not None and foreground == self.own_hwnd

def _input_target_is_foreground(self) -> bool:
    bound = self._bound
    return bound is not None and bool(self.window_service.is_foreground(bound))
```

`AutomationEngine.bind()` 调用可选 `set_target_guard(self._input_target_is_foreground)`；`AutomationCore._input()` 将 `InputTargetUnavailable` 映射为专用 `ForegroundLostError`。worker 对该异常调用统一前台中断处理：控制窗口前台使用 `E_USER_PAUSE` 且 `save_diagnostic=False`；其他窗口使用 `E_WINDOW` 和明确的 Windows 弹窗提示。

- [ ] **Step 7: 运行输入、窗口、引擎定向测试**

Run: `py -3.13 -m pytest try/tests/test_safe_input.py try/tests/test_capture_window.py try/tests/test_engine.py -q`

Expected: PASS。

- [ ] **Step 8: 提交输入安全阶段**

```powershell
git add src/auto_fishing/platform/input.py src/auto_fishing/platform/windowing.py src/auto_fishing/automation/engine.py try/tests/test_safe_input.py try/tests/test_capture_window.py try/tests/test_engine.py
git commit -m "fix: guard input against foreground popups"
```

### Task 2: 幂等关闭屏幕键盘并避免退出异常弹窗

**Files:**
- Modify: `src/auto_fishing/platform/on_screen_keyboard.py`
- Modify: `src/auto_fishing/ui/main_window.py`
- Modify: `src/auto_fishing/app.py`
- Test: `try/tests/test_on_screen_keyboard.py`
- Test: `try/tests/test_ui_smoke.py`

**Interfaces:**
- Consumes: `DiagnosticBundleService.request_report(report_type, code, detail, state, frame, context)`、`MainWindow.controller.shutdown()`。
- Produces: 自动清理诊断 `E_CLEANUP`；正常退出不抛清理异常组。

- [ ] **Step 1: 写失效屏幕键盘句柄的失败测试**

```python
def test_win32_api_treats_invalid_window_handle_as_already_closed(monkeypatch):
    user32 = FakeUser32()
    user32.post_result = 0
    monkeypatch.setattr("ctypes.get_last_error", lambda: 1400)

    Win32KeyboardApi(user32=user32).close_window(55)

    assert user32.messages == [(55, 0x0010)]
```

- [ ] **Step 2: 确认测试因错误 1400 被抛出而失败**

Run: `py -3.13 -m pytest try/tests/test_on_screen_keyboard.py -q`

Expected: FAIL，当前实现抛 `OnScreenKeyboardError`。

- [ ] **Step 3: 将错误 1400 视为已关闭**

```python
ERROR_INVALID_WINDOW_HANDLE = 1400

if not self.user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0):
    error_code = ctypes.get_last_error()
    if error_code == ERROR_INVALID_WINDOW_HANDLE:
        return
```

- [ ] **Step 4: 写退出顺序与清理诊断的失败测试**

```python
def test_close_stops_controller_before_saving_settings(root):
    events = []
    class OrderedController(FakeController):
        def shutdown(self):
            events.append("shutdown")
            super().shutdown()
    class OrderedSettings(FakeSettings):
        def save(self, settings):
            events.append("save")
            super().save(settings)
    window = MainWindow(root, OrderedController(), OrderedSettings())
    window.close()
    assert events == ["shutdown", "save"]

def test_application_reports_cleanup_failure_and_exits_cleanly():
    events = []
    reporter = AppReporter(events)
    runtime_log = AppRuntimeLog(events)
    services = ApplicationServices(
        window_service=AppWindowService(events),
        hotkey=AppHotkey(events, succeeds=True),
        safe_input=FailingCloseAppSafeInput(events),
        engine=BridgeEngine(events),
        diagnostics=AppDiagnostics(events),
        settings=FakeSettings(),
        runtime_log=runtime_log,
        diagnostic_reporter=reporter,
    )
    Application(
        services=services,
        root_factory=lambda: AppRoot(events),
        main_window_factory=lambda root, controller, settings: AppMainWindow(
            root, controller, settings, events
        ),
    ).run()
    assert reporter.requests[0]["code"] == "E_CLEANUP"
    assert any(
        isinstance(event, tuple)
        and event[0:2] == ("runtime_log.event", "application.cleanup_failed")
        for event in events
    )
```

- [ ] **Step 5: 先停止控制器，再保存设置并销毁窗口**

```python
try:
    self.controller.shutdown()
finally:
    try:
        self.settings_store.save(settings)
    finally:
        self.root.destroy()
```

- [ ] **Step 6: 清理失败写诊断但不重新抛成退出崩溃**

`Application._cleanup()` 在关闭 `diagnostic_reporter` 前，若已有清理错误则请求一次：

```python
services.diagnostic_reporter.request_report(
    report_type="automatic",
    code="E_CLEANUP",
    detail="；".join(f"{type(error).__name__}: {error}" for error in errors),
    state="程序关闭",
    frame=None,
    context={"phase": "cleanup", "cleanup_error_count": len(errors)},
).result(timeout=2.0)
```

`Application.run()` 仍重新抛运行期异常；只有“主循环正常结束、仅关闭清理失败”时记录并正常返回。

- [ ] **Step 7: 运行退出与屏幕键盘定向测试**

Run: `py -3.13 -m pytest try/tests/test_on_screen_keyboard.py try/tests/test_ui_smoke.py try/tests/test_v2_diagnostics.py -q`

Expected: PASS。

- [ ] **Step 8: 提交退出阶段**

```powershell
git add src/auto_fishing/platform/on_screen_keyboard.py src/auto_fishing/ui/main_window.py src/auto_fishing/app.py try/tests/test_on_screen_keyboard.py try/tests/test_ui_smoke.py
git commit -m "fix: exit cleanly after Windows interruptions"
```

### Task 3: 全量验证、构建和项目记录

**Files:**
- Modify: `AGENTS.md`
- Modify: `doc/验收标准.md`
- Modify/Create: `doc/进展记录/2026-7-15.md`
- Build: `dist/异环自动钓鱼V2.exe`（Git 忽略）
- Copy: `异环自动钓鱼V2.exe`（Git 忽略）

**Interfaces:**
- Consumes: Task 1 和 Task 2 的最终行为。
- Produces: 可验证的 V2 候选构建、SHA256 和人工验收清单。

- [ ] **Step 1: 运行完整 pytest 回归**

Run: `py -3.13 -m pytest try/tests -q`

Expected: 现有 419 项加新增回归全部 PASS。

- [ ] **Step 2: 构建 V2 单文件并验证清单**

Run: `powershell -ExecutionPolicy Bypass -File scripts/build_v2.ps1 -PythonPath C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe`

Expected: 测试通过，生成 `dist/异环自动钓鱼V2.exe`，并验证 `requireAdministrator`、`uiAccess=false`、`PerMonitorV2`、`true/pm`。

- [ ] **Step 3: 同步根目录候选并核对 SHA256**

使用 PowerShell `Copy-Item -LiteralPath` 同步构建产物，随后运行：

```powershell
Get-FileHash -Algorithm SHA256 .\dist\异环自动钓鱼V2.exe
Get-FileHash -Algorithm SHA256 .\异环自动钓鱼V2.exe
```

Expected: 两个哈希一致。

- [ ] **Step 4: 更新长期规则、验收标准和当日进展记录**

记录：输入守卫触发位置、Windows 弹窗后的安全暂停与人工继续流程、`E_CLEANUP`、测试总数、构建大小与 SHA256；提升权限烟雾和真实游戏弹窗复验标记为人工确认。

- [ ] **Step 5: 提交文档和构建记录**

```powershell
git add AGENTS.md doc/验收标准.md doc/进展记录/2026-7-15.md docs/superpowers/plans/2026-07-15-popup-safe-input-clean-exit.md
git commit -m "docs: record popup and exit hardening"
```

- [ ] **Step 6: 合并 main 并删除任务分支**

按 `superpowers:finishing-a-development-branch` 完成最终测试、切回 `main`、`--no-ff` 合并 `codex/fix-popup-exit`，复测后删除本地任务分支。未经用户确认不推送 GitHub，也不创建 Release。
