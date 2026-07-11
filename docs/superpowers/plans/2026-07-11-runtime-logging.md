# 完整运行日志与逐帧截图实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为每次应用启动保存可回放的完整结构化运行遥测和低分辨率逐帧截图，并在日志无法完整保存时安全暂停自动化。

**Architecture:** 新增独立的 `RuntimeLogStore`，它在 `%LOCALAPPDATA%\异环自动钓鱼\runs` 下建立每次启动的目录，将 JSON Lines 事件和 JPEG 缩略图交给单一写入线程顺序写入。应用创建该存储并把它注入输入与自动化边界；引擎在每个已处理客户端帧记录观测和前后状态，输入后端记录 `SendInput` 结果。队列溢出或写入失败通过 `RuntimeLogError` 反馈，引擎以 `E_LOGGING` 释放输入并暂停。

**Tech Stack:** Python 3.13、标准库 `queue`/`threading`/`json`/`pathlib`、OpenCV JPEG 编码、pytest、PyInstaller。

## Global Constraints

- 不读取游戏内存、不注入进程、不启用管理员权限；保持现有 Windows `SendInput` 输入实现。
- 正常帧使用客户端画面生成最长边 480 像素、JPEG 质量 50 的缩略图；异常诊断保持现有全分辨率策略。
- 每次应用启动建立独立运行目录，仅保留最近 30 个完整运行目录；清理只可作用于 `runs` 子目录。
- 日志包含逐帧遥测、状态、窗口、输入请求与 `SendInput` 返回值；成功发送不得记录可能陈旧的 Windows 错误码。
- 队列上限固定 300 项；不能完整写入时不得丢帧后继续自动化，必须进入 `PAUSED/E_LOGGING` 并释放 F/A/D/鼠标。
- 不新增第三方依赖；测试临时产物只写入 pytest 的 `tmp_path`。
- 完成每个阶段后更新 `doc/进展记录/2026-7-11.md` 和 `doc/验收标准.md`；同步维护 `AGENTS.md`。

---

### Task 1: 运行记录存储与异步写入

**Files:**
- Create: `src/auto_fishing/storage/runtime_logging.py`
- Modify: `src/auto_fishing/storage/__init__.py`
- Modify: `try/tests/test_storage.py`

**Interfaces:**
- Produces: `RuntimeLogError(RuntimeError)`、`RuntimeLogStore(root: Path, *, queue_size: int = 300, now: Callable[[], datetime] | None = None)`。
- Produces: `RuntimeLogStore.start() -> Path`、`event(name: str, **fields: JSONValue) -> None`、`record_frame(frame: np.ndarray, *, observation: SceneObservation, state_before: FishingState, snapshot: RuntimeSnapshot, frame_timestamp: float, now_monotonic: float) -> int`、`raise_if_failed() -> None`、`cleanup() -> None`、`close() -> None`。
- Consumes: 现有 `SceneObservation`、`RuntimeSnapshot` 和 OpenCV；不得依赖 Tk、窗口服务或自动化引擎。

- [ ] **Step 1: 写入失败的测试：启动目录、事件与缩略图**

在 `try/tests/test_storage.py` 追加下列测试，要求测试先引用尚不存在的模块，并补充 `import cv2`：

```python
from auto_fishing.storage.runtime_logging import RuntimeLogStore
from auto_fishing.model import FishingState, RuntimeSnapshot, SceneObservation


def test_runtime_log_writes_jsonl_and_480px_jpeg(tmp_path):
    store = RuntimeLogStore(tmp_path / "runs", queue_size=3)
    run_dir = store.start()
    store.event("application.started", pid=123)
    store.record_frame(
        np.zeros((1080, 1920, 3), dtype=np.uint8),
        observation=SceneObservation(ready=True),
        state_before=FishingState.READY,
        snapshot=RuntimeSnapshot(FishingState.WAIT_BITE, 0, 1, 30.0),
        frame_timestamp=10.0,
        now_monotonic=10.01,
    )
    store.close()

    entries = [json.loads(line) for line in (run_dir / "events.jsonl").read_text("utf-8").splitlines()]
    image = cv2.imread(str(run_dir / "frames" / "00000001.jpg"))
    assert entries[0]["event"] == "application.started"
    assert entries[-1]["event"] == "frame.processed"
    assert max(image.shape[:2]) == 480
```

- [ ] **Step 2: 运行测试确认正确失败**

运行：`py -3.13 -m pytest try/tests/test_storage.py::test_runtime_log_writes_jsonl_and_480px_jpeg -q`

预期：收集失败，提示 `auto_fishing.storage.runtime_logging` 不存在。

- [ ] **Step 3: 追加保留与故障边界的失败测试**

追加下列三个独立测试：

```python
def test_runtime_log_cleanup_keeps_newest_thirty_runs(tmp_path):
    root = tmp_path / "runs"
    base = datetime(2026, 7, 11, tzinfo=timezone.utc)
    for index in range(31):
        run = root / f"run-{index:02d}"
        run.mkdir(parents=True)
        (run / "events.jsonl").write_text("", "utf-8")
        stamp = (base + timedelta(seconds=index)).timestamp()
        os.utime(run, (stamp, stamp))
    RuntimeLogStore(root).cleanup()
    assert [path.name for path in root.iterdir()] == [
        f"run-{index:02d}" for index in range(1, 31)
    ]


def test_runtime_log_cleanup_never_traverses_outside_root(tmp_path):
    root = tmp_path / "runs"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", "utf-8")
    (root / "not-a-run.txt").write_text("ignore", "utf-8")
    RuntimeLogStore(root).cleanup()
    assert sentinel.read_text("utf-8") == "keep"
    assert (root / "not-a-run.txt").is_file()


def test_runtime_log_queue_full_surfaces_runtime_log_error(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingStore(RuntimeLogStore):
        def _write_item(self, item):
            entered.set()
            assert release.wait(timeout=1)
            super()._write_item(item)

    store = BlockingStore(tmp_path / "runs", queue_size=1)
    store.start()
    store.event("first")
    assert entered.wait(timeout=1)
    store.event("second")
    with pytest.raises(RuntimeLogError, match="日志队列已满"):
        store.raise_if_failed()
    release.set()
    store.close()
```

测试文件要补充 `import os`、`import threading` 和 `from datetime import timedelta, timezone`。第三个测试通过覆写私有写入钩子稳定制造积压，断言存储器报告错误而不是丢弃帧。

- [ ] **Step 4: 实现最小存储器**

实现下列核心行为：

```python
class RuntimeLogStore:
    def start(self) -> Path:
        """创建唯一目录、frames/ 和 events.jsonl，启动单个 daemon 写入线程。"""

    def event(self, name: str, **fields: JSONValue) -> None:
        """加入 timestamp_utc、monotonic、event 与 fields，失败即保存首个错误。"""

    def record_frame(
        self, frame: np.ndarray, *, observation: SceneObservation,
        state_before: FishingState, snapshot: RuntimeSnapshot,
        frame_timestamp: float, now_monotonic: float,
    ) -> int:
        """生成最长边 480 的独立 BGR 图并入队 JPEG 写入任务，返回递增序号。"""

    def raise_if_failed(self) -> None:
        """将首个后台失败包装为 RuntimeLogError。"""

    def close(self) -> None:
        """发送结束标记，等待队列和写入线程完成，随后执行 30 次保留清理。"""
```

JSON Lines 中帧事件必须含 `frame_index`、`frame_timestamp`、`frame_age`、`fps`、`state_before`、`state_after`、完成数、目标数、`bite`、`ready`、`result`、`result_candidate`，以及进度条存在时的 `green_left`、`green_right`、`yellow_x` 与 `confidence`。写入线程使用 `cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 50])`，并对 `events.jsonl` 以 UTF-8 逐行写入和 `flush()`。

- [ ] **Step 5: 运行存储测试并提交**

运行：`py -3.13 -m pytest try/tests/test_storage.py -q`

预期：全部通过；不修改 `流程截图` 或真实 `%LOCALAPPDATA%` 内容。

提交：`git add src/auto_fishing/storage/runtime_logging.py src/auto_fishing/storage/__init__.py try/tests/test_storage.py && git commit -m "feat: add runtime log storage"`

### Task 2: 记录键鼠请求与 SendInput 结果

**Files:**
- Modify: `src/auto_fishing/platform/input.py`
- Modify: `try/tests/test_safe_input.py`

**Interfaces:**
- Consumes: 任务 1 的 `RuntimeLogStore.event()`；生产构造器允许 `recorder: Any | None = None`，以兼容原有测试和调用者。
- Produces: `SafeInput` 的业务输入事件 `input.request`；`Win32InputBackend` 的 `sendinput.result` 和 `cursor.result` 事件。

- [ ] **Step 1: 写入成功键盘发送的失败测试**

为 `FakeUser32` 增加可收集的 `events` 记录器，并新增：

```python
def test_win32_records_sendinput_success_without_stale_error_code():
    recorder = RecordingLog()
    backend = Win32InputBackend(user32=FakeUser32(), recorder=recorder)
    backend.key_down("F")
    assert recorder.events == [{"event": "sendinput.result", "requested": 1, "sent": 1}]
```

`RecordingLog.event(name, **fields)` 只追加 `{"event": name, **fields}`。断言成功事件没有 `windows_error` 字段。

- [ ] **Step 2: 运行测试确认正确失败**

运行：`py -3.13 -m pytest try/tests/test_safe_input.py::test_win32_records_sendinput_success_without_stale_error_code -q`

预期：失败，提示构造器不接受 `recorder` 参数。

- [ ] **Step 3: 写入失败和业务请求的失败测试**

新增下列两个测试：

```python
def test_win32_records_partial_send_with_windows_error(monkeypatch):
    recorder = RecordingLog()
    user32 = FakeUser32()
    user32.send_result = 0
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)
    backend = Win32InputBackend(user32=user32, recorder=recorder)
    with pytest.raises(InputFailure, match="SendInput sent 0 of 1"):
        backend.key_down("F")
    assert recorder.events == [{
        "event": "sendinput.result", "requested": 1,
        "sent": 0, "windows_error": 5,
    }]


def test_safe_input_records_tap_and_direction_requests():
    recorder = RecordingLog()
    safe = SafeInput(FakeBackend(), sleep=lambda _: None, recorder=recorder)
    safe.tap_f()
    safe.set_direction(Direction.RIGHT)
    safe.release_all()
    assert recorder.events == [
        {"event": "input.request", "action": "tap", "key": "F"},
        {"event": "input.request", "action": "key_down", "key": "F"},
        {"event": "input.request", "action": "key_up", "key": "F"},
        {"event": "input.request", "action": "key_down", "key": "D"},
        {"event": "input.request", "action": "key_up", "key": "D"},
    ]
```

前者令 `SendInput` 返回 0、将 `ctypes.get_last_error` 固定为 5，并断言事件为 `requested=1`、`sent=0`、`windows_error=5` 后抛 `InputFailure`；后者调用 `tap_f()`、`set_direction(Direction.RIGHT)` 和 `release_all()`，断言有区分 F 点击、D 按下和释放的 `input.request` 业务事件。

- [ ] **Step 4: 最小实现与回归验证**

在 `SafeInput` 和 `Win32InputBackend` 构造器保存可选记录器；每项业务操作前调用：

```python
if self.recorder is not None:
    self.recorder.event("input.request", action="key_down", key=key)
```

在 `_send_inputs()` 返回后记录：

```python
fields = {"requested": requested, "sent": int(sent)}
if sent != requested:
    fields["windows_error"] = ctypes.get_last_error()
self.recorder.event("sendinput.result", **fields)
```

保持原有异常文本与按键释放语义不变。

- [ ] **Step 5: 运行输入测试并提交**

运行：`py -3.13 -m pytest try/tests/test_safe_input.py -q`

预期：全部通过。

提交：`git add src/auto_fishing/platform/input.py try/tests/test_safe_input.py && git commit -m "feat: log input requests and results"`

### Task 3: 把运行记录接入自动化与应用生命周期

**Files:**
- Modify: `src/auto_fishing/automation/engine.py`
- Modify: `src/auto_fishing/app.py`
- Modify: `try/tests/test_engine.py`
- Modify: `try/tests/test_ui_smoke.py`

**Interfaces:**
- Consumes: `RuntimeLogStore.event()`、`record_frame()`、`raise_if_failed()` 与 `close()`；日志对象以可选依赖注入，旧测试的构造函数保持可用。
- Produces: `E_LOGGING` 暂停代码；每个成功处理的客户端帧都有一个 `frame.processed` 事件和对应 JPEG。

- [ ] **Step 1: 为每个已处理帧写入失败测试**

在 `try/tests/test_engine.py` 定义 `RecordingRuntimeLog`，提供 `event()`、`record_frame()`、`raise_if_failed()` 和 `close()`。新增：

```python
def test_engine_records_observation_and_state_for_each_processed_frame(tmp_path):
    logger = RecordingRuntimeLog()
    source = BlockingSecondLatestFailure()
    engine, core, _input, _window, _source = make_engine(
        tmp_path, frame_source=source, runtime_log=logger,
    )
    try:
        engine.start(1)
        wait_until(lambda: len(logger.frames) == 1)
        assert logger.frames[0]["state_before"] is FishingState.READY
        assert logger.frames[0]["snapshot"].state is FishingState.WAIT_BITE
    finally:
        source.allow_second_latest.set()
        engine.shutdown()
```

- [ ] **Step 2: 运行测试确认正确失败**

运行：`py -3.13 -m pytest try/tests/test_engine.py::test_engine_records_observation_and_state_for_each_processed_frame -q`

预期：失败，因为 `make_engine()` 和 `AutomationEngine` 尚不接收 `runtime_log`，或帧记录数为零。

- [ ] **Step 3: 为日志失败写入安全暂停失败测试**

新增：

```python
def test_engine_pauses_with_e_logging_and_releases_inputs_when_runtime_log_fails(tmp_path):
    logger = FailingRuntimeLog("日志队列已满")
    engine, core, input_service, _window, _source = make_engine(
        tmp_path, runtime_log=logger,
    )
    try:
        engine.start(1)
        wait_until(lambda: core.snapshot.state is FishingState.PAUSED)
        assert core.pause_code == "E_LOGGING"
        assert "日志队列已满" in core.snapshot.error
        assert input_service.events[-1] == "release"
    finally:
        engine.shutdown()
```

另在 UI 冒烟测试中验证应用关闭动作包含 `runtime_log.close`，并且构建服务把同一个记录器传给输入后端和引擎。

- [ ] **Step 4: 最小接入实现**

扩展 `AutomationEngine.__init__`：

```python
def __init__(
    self, *, core: AutomationCore, window_service: Any, frame_source: Any,
    scene_recognizer: Any, diagnostics: Any, runtime_log: Any | None = None,
    clock: Callable[[], float] = time.monotonic,
    logger: logging.Logger | None = None,
):
    self.runtime_log = runtime_log
```

在 `bind()`、`start()`、`resume()`、`_pause()`、窗口刷新/重启截屏、异常捕获和 `_publish()` 的状态变化处写事件。每个成功完成 `scene_recognizer.observe()` 与 `core.process()` 的循环，以处理前状态和得到的 `snapshot` 调用 `runtime_log.record_frame(client_frame, observation=observation, state_before=state_before, snapshot=snapshot, frame_timestamp=packet.timestamp, now_monotonic=now)`，紧接着调用 `raise_if_failed()`；任何 `RuntimeLogError` 走：

```python
self._pause("E_LOGGING", str(error), packet.frame, expected_epoch=frame_epoch)
```

从应用的 `_build_services()` 创建但不启动 `RuntimeLogStore(data_dir / "runs")`，依次注入 `Win32InputBackend(recorder=runtime_log)` 和 `AutomationEngine(runtime_log=runtime_log)`，并把可选字段 `runtime_log: Any | None = None` 加到 `ApplicationServices` 的最后，以保持现有测试构造器兼容。`Application.run()` 在创建 Tk 根窗口后、创建控制窗口前调用 `runtime_log.start()`；成功后记录应用运行、热键注册结果、排除捕获结果和清理错误。`_cleanup()` 在关闭引擎和释放输入后调用 `runtime_log.close`，并将关闭失败并入现有 `BaseExceptionGroup`。

若运行记录器创建失败，应用在创建控制窗口后调用 `main_window.block_start(f"运行日志初始化失败：{error}")`，保持程序可退出但绝不启动自动化。

- [ ] **Step 5: 运行引擎与 UI 测试并提交**

运行：`py -3.13 -m pytest try/tests/test_engine.py try/tests/test_ui_smoke.py -q`

预期：全部通过，`E_LOGGING` 失败路径释放输入，正常路径每个已处理帧恰好记录一次。

提交：`git add src/auto_fishing/automation/engine.py src/auto_fishing/app.py try/tests/test_engine.py try/tests/test_ui_smoke.py && git commit -m "feat: capture complete runtime telemetry"`

### Task 4: 文档、全量验证、构建与真实游戏验收准备

**Files:**
- Modify: `AGENTS.md`
- Modify: `doc/验收标准.md`
- Modify: `doc/进展记录/2026-7-11.md`

- [ ] **Step 1: 补充验收项与长期维护说明**

在 `AGENTS.md` 的本地数据、架构和验收章节写明运行记录路径、`events.jsonl`/`frames` 构成、480 像素/质量 50、最近 30 次保留、300 项队列和 `E_LOGGING` 语义。`doc/验收标准.md` 增加真实游戏复现步骤：开始后核对首帧 `frame.processed`、`input.request`、`sendinput.result` 与 `00000001.jpg`，并在队列/写盘故障模拟中确认 `PAUSED/E_LOGGING` 和按键释放。当天进展记录写入实际时间段、文件清单、缺失依赖安装、基线和最终验证结果。

- [ ] **Step 2: 运行完整自动验证**

运行：`py -3.13 -m pytest try/tests -q`

预期：全量通过且无测试警告。

- [ ] **Step 3: 构建发布物并运行烟雾**

运行：`powershell -ExecutionPolicy Bypass -File scripts/build.ps1 -PythonPath C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe`

再运行：`powershell -ExecutionPolicy Bypass -File try/smoke_exe.ps1`

预期：构建脚本先通过全量测试并生成 `dist/异环自动钓鱼.exe`，烟雾输出 `SMOKE_OK`。构建会覆盖已忽略 `build/` 与 `dist/`，因此执行前运行下列 PowerShell（Windows 命令行）命令备份当前发布物：

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$backup = "D:\0文件夹\备份\异环自动钓鱼-runtime-logging-prebuild-$stamp"
New-Item -ItemType Directory -Force -Path $backup
Copy-Item -LiteralPath dist\异环自动钓鱼.exe -Destination $backup
```

- [ ] **Step 4: 提交文档和验收记录**

运行：`git diff --check && git status --short`

提交：`git add AGENTS.md doc/验收标准.md doc/进展记录/2026-7-11.md && git commit -m "docs: record runtime logging validation"`

### Task 5: 完成交付与分支整合

**Files:**
- Modify only if required by Task 4 evidence corrections.

- [ ] **Step 1: 复核提交范围和测试证据**

运行：`git log --oneline main..HEAD`、`git diff --check main...HEAD`、`py -3.13 -m pytest try/tests -q`。

预期：仅包含规格、计划、运行记录实现、测试和项目文档；无真实日志、截图、构建物或敏感配置进入 Git。

- [ ] **Step 2: 合并回 main 并清理隔离分支**

在全量测试、构建和烟雾通过且用户完成一次真实游戏人工验收后，切回主工作区执行快进合并 `codex/feat-runtime-logging` 到 `main`，再移除 `.worktrees/codex-feat-runtime-logging` 与已合并分支。若真实游戏验收未能在本次会话执行，保留分支并明确报告原因，不得声称已完成主线合并。
