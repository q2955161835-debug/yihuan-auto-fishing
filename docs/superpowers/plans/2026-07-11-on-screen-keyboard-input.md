# Windows 屏幕键盘输入适配实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让程序通过固定在游戏显示器左下角的 Windows 屏幕键盘可靠发送 F、按住/释放 A/D，并完成真实《异环》自动钓鱼验收。

**Architecture:** 新增独立的屏幕键盘窗口管理器和输入后端，复用 `SafeInput` 与现有状态机；输入后端把键盘按下/抬起转换为屏幕键盘上的真实可见鼠标按下/抬起。引擎把屏幕键盘窗口矩形换算为客户区遮挡矩形，仅从结算识别比例中排除重叠像素，同时保持原始运行截图。

**Tech Stack:** Python 3.13、Win32 ctypes、Tkinter、DXcam、NumPy、OpenCV、pytest、PyInstaller。

## Global Constraints

- 不安装驱动，不注入游戏进程，不读取游戏内存。
- Windows 屏幕键盘始终可见并固定在游戏显示器左下角。
- 自动化运行期间程序独占鼠标，F8、暂停、异常和退出必须释放鼠标左键。
- 已存在的屏幕键盘退出时保留；只有本程序启动的屏幕键盘由本程序尽力关闭，Windows 拒绝时安全保留。
- 发布清单请求管理员权限以操作受 `UIAccess`（用户界面访问）保护的屏幕键盘；源码普通权限运行仍保留位置与关闭失败降级。
- 顶部进度条和右下角准备区域不得被遮挡；结算识别排除左下角遮挡像素。
- 运行日志保存原始画面并记录屏幕键盘几何、按键坐标与鼠标结果。
- 每个生产改动先增加失败测试，再写最小实现并运行相关回归。

---

### Task 1: 屏幕键盘窗口生命周期与几何

**Files:**
- Create: `src/auto_fishing/platform/on_screen_keyboard.py`
- Create: `try/tests/test_on_screen_keyboard.py`

**Interfaces:**
- Produces: `OnScreenKeyboardError(RuntimeError)`。
- Produces: `KeyboardGeometry(hwnd: int, window_rect: Rect, client_rect: Rect, key_points: dict[str, tuple[int, int]])`。
- Produces: `OnScreenKeyboardWindow.ensure(monitor_rect: Rect) -> KeyboardGeometry`、`geometry() -> KeyboardGeometry`、`close() -> None`。

- [ ] **Step 1: 写窗口复用、自启动和所有权失败测试**

```python
def test_ensure_reuses_existing_keyboard_without_owning_it():
    api = FakeKeyboardApi(existing_hwnd=55)
    keyboard = OnScreenKeyboardWindow(api=api, launcher=FakeLauncher())
    geometry = keyboard.ensure(MONITOR)
    assert geometry.hwnd == 55
    keyboard.close()
    assert api.closed == []

def test_ensure_launches_missing_keyboard_and_closes_owned_window():
    api = FakeKeyboardApi(existing_hwnd=0, hwnd_after_poll=77)
    launcher = FakeLauncher()
    keyboard = OnScreenKeyboardWindow(api=api, launcher=launcher, sleep=lambda _: None)
    keyboard.ensure(MONITOR)
    keyboard.close()
    assert launcher.started == 1
    assert api.closed == [77]
```

- [ ] **Step 2: 运行测试确认模块不存在而失败**

Run: `py -3.13 -m pytest try/tests/test_on_screen_keyboard.py -q`

Expected: collection fails with `ModuleNotFoundError: auto_fishing.platform.on_screen_keyboard`。

- [ ] **Step 3: 实现窗口 API、启动轮询与所有权**

```python
class OnScreenKeyboardWindow:
    def __init__(self, api=None, launcher=None, sleep=real_sleep, recorder=None):
        self.api = api or Win32KeyboardApi()
        self.launcher = launcher or OskLauncher()
        self.sleep = sleep
        self.recorder = recorder
        self._owned = False
        self._hwnd = 0

    def ensure(self, monitor_rect: Rect) -> KeyboardGeometry:
        hwnd = self.api.find_window()
        if not hwnd:
            self.launcher.start()
            self._owned = True
            for _ in range(50):
                hwnd = self.api.find_window()
                if hwnd:
                    break
                self.sleep(0.1)
        if not hwnd:
            raise OnScreenKeyboardError("启动 Windows 屏幕键盘超时")
        self._hwnd = hwnd
        self.api.position_bottom_left(hwnd, monitor_rect)
        return self.geometry()
```

- [ ] **Step 4: 增加左下角定位、负显示器坐标、按键比例与遮挡拒绝测试**

```python
def test_geometry_maps_default_layout_keys_inside_client_rect():
    geometry = make_keyboard().ensure(Rect(-1920, 0, 0, 1080))
    assert geometry.key_points["A"][0] < geometry.key_points["D"][0]
    assert geometry.key_points["D"][0] < geometry.key_points["F"][0]
    assert all(geometry.client_rect.left <= x < geometry.client_rect.right for x, _ in geometry.key_points.values())

def test_ensure_rejects_overlap_with_top_or_ready_regions():
    keyboard = make_keyboard(window_rect=Rect(500, 0, 1500, 400))
    with pytest.raises(OnScreenKeyboardError, match="遮挡关键识别区域"):
        keyboard.ensure(MONITOR, game_client=CLIENT)
```

- [ ] **Step 5: 实现归一化键坐标和关键区域校验**

按 1350×377 实机客户区基线使用 `A=(160/1350, 205/377)`、`D=(310/1350, 205/377)`、`F=(383/1350, 205/377)`；所有结果经 `ClientToScreen` 转换。使用 `TOP_ROI` 和 `READY_ROI` 换算屏幕矩形，任何相交都抛出 `OnScreenKeyboardError("屏幕键盘遮挡关键识别区域")`。

- [ ] **Step 6: 运行窗口测试并提交**

Run: `py -3.13 -m pytest try/tests/test_on_screen_keyboard.py -q`

Expected: all tests pass。

Commit: `git commit -m "feat: manage Windows on-screen keyboard"`

### Task 2: 屏幕键盘鼠标输入后端

**Files:**
- Modify: `src/auto_fishing/platform/input.py`
- Modify: `src/auto_fishing/platform/on_screen_keyboard.py`
- Modify: `try/tests/test_safe_input.py`
- Modify: `try/tests/test_on_screen_keyboard.py`

**Interfaces:**
- Produces: `Win32MouseDriver.move(x: int, y: int)`、`down()`、`up()`、`click(x: int, y: int)`。
- Produces: `OnScreenKeyboardInputBackend.prepare(monitor_rect: Rect, client_rect: Rect) -> None`、`occlusion_rect() -> Rect | None`、`key_down(key: str)`、`key_up(key: str)`、`click(x: int, y: int)`、`mouse_up()`、`close()`。
- `SafeInput.prepare(...)`、`occlusion_rect()`、`close()` 委托给后端。

- [ ] **Step 1: 写 F 轻点和 A/D 按住换向失败测试**

```python
def test_keyboard_backend_holds_direction_with_mouse_and_releases_before_switch():
    mouse = RecordingMouse()
    backend = OnScreenKeyboardInputBackend(window=ReadyKeyboard(), mouse=mouse)
    backend.key_down("A")
    backend.key_up("A")
    backend.key_down("D")
    assert mouse.events == [
        ("move", 160, 205), ("down",), ("up",),
        ("move", 310, 205), ("down",),
    ]

def test_keyboard_backend_f_down_and_up_are_balanced():
    mouse = RecordingMouse()
    backend = OnScreenKeyboardInputBackend(window=ReadyKeyboard(), mouse=mouse)
    backend.key_down("F")
    backend.key_up("F")
    assert mouse.events[-2:] == [("down",), ("up",)]
```

- [ ] **Step 2: 运行目标测试确认类不存在而失败**

Run: `py -3.13 -m pytest try/tests/test_on_screen_keyboard.py try/tests/test_safe_input.py -q`

Expected: tests fail because `OnScreenKeyboardInputBackend` and `Win32MouseDriver` are missing。

- [ ] **Step 3: 拆分鼠标原语并实现后端状态机**

```python
def key_down(self, key: str) -> None:
    key = key.upper()
    if self._held_key is not None:
        if self._held_key == key:
            return
        raise InputFailure(f"屏幕键盘仍按住 {self._held_key}")
    point = self.window.geometry().key_points[key]
    self.mouse.move(*point)
    try:
        self.mouse.down()
    except Exception:
        self.mouse.up()
        raise
    self._held_key = key

def key_up(self, key: str) -> None:
    if self._held_key != key.upper():
        return
    try:
        self.mouse.up()
    finally:
        self._held_key = None
```

- [ ] **Step 4: 写移动、按下、抬起失败及幂等清理测试**

覆盖 `move()` 失败不按下、`down()` 失败额外抬起、首次 `up()` 失败保留状态供 `release_all()` 重试、重复 `mouse_up()` 不新增逻辑按键、直接结算点击前释放方向。

- [ ] **Step 5: 实现失败清理、记录字段与 SafeInput 委托**

日志事件 `osk.prepared`、`osk.key_target`、`osk.mouse_down`、`osk.mouse_up` 至少包含 `hwnd`、`key`、`x`、`y`、`success`；失败事件增加 `windows_error`。

- [ ] **Step 6: 运行输入回归并提交**

Run: `py -3.13 -m pytest try/tests/test_on_screen_keyboard.py try/tests/test_safe_input.py -q`

Expected: all tests pass。

Commit: `git commit -m "feat: route fishing keys through screen keyboard"`

### Task 3: 应用与引擎生命周期接入

**Files:**
- Modify: `src/auto_fishing/app.py`
- Modify: `src/auto_fishing/automation/engine.py`
- Modify: `try/tests/test_engine.py`
- Modify: `try/tests/test_ui_smoke.py`

**Interfaces:**
- Consumes: `SafeInput.prepare(monitor_rect, client_rect)`、`close()`。
- Produces: 引擎绑定与显示器变化时重新准备屏幕键盘；应用关闭时释放后关闭屏幕键盘。

- [ ] **Step 1: 写绑定准备、准备失败和关闭顺序测试**

```python
def test_engine_bind_prepares_input_for_bound_monitor():
    engine, _, input_service = build_engine_with_preparable_input()
    engine.bind(BOUND)
    assert input_service.prepared == [(BOUND.monitor_rect, BOUND.client_rect)]

def test_application_releases_input_before_closing_keyboard():
    app.run()
    assert events.index("input.release") < events.index("input.close")
    assert events.index("input.close") < events.index("runtime.close")
```

- [ ] **Step 2: 运行目标测试确认缺少委托而失败**

Run: `py -3.13 -m pytest try/tests/test_engine.py try/tests/test_ui_smoke.py -q`

Expected: new tests fail because prepare/close are not called。

- [ ] **Step 3: 在绑定、刷新和应用清理中接入**

`AutomationEngine.bind()` 在保存 `_bound` 前调用 `core.input_service.prepare(...)`；`_refresh_and_validate_window()` 检测客户区或显示器变化时再次调用。失败在绑定阶段显示“屏幕键盘准备失败”，运行阶段以 `E_OSK` 暂停。`Application._cleanup()` 在“释放输入”之后增加“关闭屏幕键盘输入”。

- [ ] **Step 4: 替换默认输入后端**

```python
keyboard = OnScreenKeyboardWindow(recorder=runtime_log)
backend = OnScreenKeyboardInputBackend(
    window=keyboard,
    mouse=Win32MouseDriver(recorder=runtime_log),
    recorder=runtime_log,
)
safe_input = SafeInput(backend, recorder=runtime_log)
```

- [ ] **Step 5: 运行引擎、界面和输入测试并提交**

Run: `py -3.13 -m pytest try/tests/test_engine.py try/tests/test_ui_smoke.py try/tests/test_safe_input.py try/tests/test_on_screen_keyboard.py -q`

Expected: all tests pass。

Commit: `git commit -m "feat: integrate screen keyboard lifecycle"`

### Task 4: 结算识别遮挡排除

**Files:**
- Modify: `src/auto_fishing/vision/scenes.py`
- Modify: `src/auto_fishing/automation/engine.py`
- Modify: `try/tests/test_vision.py`
- Modify: `try/tests/test_engine.py`

**Interfaces:**
- Produces: `SceneRecognizer.observe(client_frame, timestamp, occlusion: Rect | None = None)`。
- Produces: `_masked_ratio(roi, predicate, excluded_rect) -> float`，有效像素不足时抛出 `ValueError("结算识别有效像素不足")`。

- [ ] **Step 1: 写遮挡像素不参与结算比例的失败测试**

```python
def test_result_ratios_ignore_screen_keyboard_occlusion():
    frame = result_like_frame_with_dark_blue_keyboard_patch()
    observation = SceneRecognizer().observe(
        frame, 1.0, occlusion=Rect(0, 700, 900, 1080)
    )
    assert not observation.result_candidate

def test_result_rejects_too_few_unoccluded_pixels():
    with pytest.raises(ValueError, match="有效像素不足"):
        SceneRecognizer().observe(frame, 1.0, occlusion=Rect(0, 0, 1920, 1080))
```

- [ ] **Step 2: 运行视觉测试确认接口不接受遮挡参数而失败**

Run: `py -3.13 -m pytest try/tests/test_vision.py -q`

Expected: tests fail with unexpected `occlusion` argument。

- [ ] **Step 3: 实现屏幕到客户区遮挡换算和有效像素比例**

引擎从 `safe_input.occlusion_rect()` 取得屏幕矩形，与 `bound.client_rect` 相交并平移为客户区 `Rect`。`SceneRecognizer` 只把该矩形映射到 `RESULT_ROI` 的局部坐标，暗色与蓝色比例共同使用相同有效像素布尔掩码；顶部和右下角裁剪保持原状。

- [ ] **Step 4: 加入真实夜景回放，修复现有 669/671 结算误报**

从 `%LOCALAPPDATA%\异环自动钓鱼\runs\run-20260711T121316419566Z\frames` 选取准备、等待和最后画面复制到 `try/fixtures/runtime-night/`，新增回放测试要求准备与等待帧的 `result_candidate=False`。根据真实结算参考图收紧结算特征，不得只调低/调高单一阈值；记录样本和精确结果。

- [ ] **Step 5: 运行视觉与引擎回归并提交**

Run: `py -3.13 -m pytest try/tests/test_vision.py try/tests/test_engine.py -q`

Expected: all tests pass，夜景样本无结算误报。

Commit: `git commit -m "fix: exclude screen keyboard from result vision"`

### Task 5: 自动验证、构建与真实游戏迭代

**Files:**
- Modify: `AGENTS.md`
- Modify: `doc/验收标准.md`
- Modify: `doc/进展记录/2026-7-11.md`
- Modify as evidence requires: `src/auto_fishing/platform/on_screen_keyboard.py`、`src/auto_fishing/vision/*.py`、对应测试。

**Interfaces:**
- Consumes: 完整屏幕键盘输入、遮挡识别与运行日志。
- Produces: `dist/异环自动钓鱼.exe` 和真实游戏验收证据。

- [ ] **Step 1: 运行全量测试**

Run: `py -3.13 -m pytest try/tests -q`

Expected: all tests pass，测试数不少于现有 228 项加本功能新增用例。

- [ ] **Step 2: 构建前记录高风险与备份发布物**

在进展记录写明构建会覆盖隔离工作树 `build/` 与 `dist/`；将现有已验证发布物备份到 `D:\0文件夹\备份\异环自动钓鱼-osk-prebuild-<时间>\`，记录大小和 SHA256。

- [ ] **Step 3: 构建单文件并执行烟雾**

Run: `powershell -ExecutionPolicy Bypass -File scripts/build.ps1 -PythonPath C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe`

Run: `powershell -ExecutionPolicy Bypass -File try/smoke_exe.ps1`

Expected: 构建门内全量测试通过，烟雾输出 `SMOKE_OK`。

- [ ] **Step 4: 真实游戏单轮验证**

运行新 EXE，绑定当前《异环》，确认屏幕键盘自动停放左下角；目标次数设为 1。核对运行日志中的 F 坐标与屏幕键盘按键高亮、上钩后 F、A/D 按住和换向、结算关闭、成功数 1。任一失败先读本轮日志与截图，增加可重复测试后再修复。

- [ ] **Step 5: F8 与连续五轮验证**

控制阶段按 F8，确认鼠标抬起且日志存在释放事件；继续后目标设为 5，记录完成数、暂停次数、实际帧率、运行目录和截图磁盘占用。Expected: 5/5 完成，无输入残留和遮挡误判。

- [ ] **Step 6: 更新长期文档并提交**

更新 `AGENTS.md` 架构/运行命令/验收基线；更新验收标准的命令、截图、日志与真实结果；进展记录写明问题、原因、解决方案、修改文件、测试、构建哈希和实机结论。

Commit: `git commit -m "docs: record screen keyboard acceptance"`

### Task 6: 完成审计、合并与清理

**Files:**
- Verify: all files named in the design and this plan。

- [ ] **Step 1: 核对所有显式要求证据**

逐项核对：F、上钩 F、A/D 按住/换向/释放、F8、结算、1 轮、5 轮、日志、低分辨率截图、全量测试、构建、烟雾、文档。任何缺少真实证据的项目保持未完成。

- [ ] **Step 2: 检查工作树和重复实现**

Run: `git status --short`

Run: `rg -n "class OnScreenKeyboard|SendInput|SetCursorPos" src/auto_fishing`

Expected: 工作树干净；键盘生命周期、鼠标原语和遮挡换算没有重复实现或临时脚本。

- [ ] **Step 3: 合并主分支并删除任务分支**

只有 Task 5 的真实单轮和 5 轮均通过后，回到主工作区，将 `codex/feat-runtime-logging` 快进或正常合并到 `main`，在 `main` 复跑关键测试，删除隔离工作树和旧任务分支。若远程推送未经用户授权，仅完成本地合并并明确询问是否推送私有 GitHub 仓库。
