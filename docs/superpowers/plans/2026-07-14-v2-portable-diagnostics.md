# 异环自动钓鱼 V2 免安装与按需诊断 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变已验收自动钓鱼核心的前提下，构建正常运行无持续日志、错误时可生成诊断 ZIP、支持主动报告且可直接分发的 Windows 10/11 单文件 V2。

**Architecture:** 保留现有 V1 自动化、视觉、截图、窗口与屏幕键盘核心，通过产品配置和独立 V2 入口选择内存记录器与诊断包服务。引擎继续使用同一记录接口，V1 仍落盘，V2 只维护最近 10 秒内存环；自动错误和主动报告都调用同一个异步诊断生成器。兼容性仅补低分辨率屏幕键盘、DPI 回退/发布物验证和控制窗可见位置，不建立第二套坐标系统。

**Tech Stack:** Python 3.13、Tkinter/ttk、DXcam 0.3.0、NumPy 2.4.1、OpenCV 4.13.0.92、Win32 ctypes、PyInstaller 6.19.0、pytest 9.1.0、PowerShell。

## Global Constraints

- 不修改已验收的状态机、视觉阈值、黄标控制、F 节奏和结算时序。
- V2 窗口标题为“异环自动钓鱼 V2”，产物名为 `异环自动钓鱼V2.exe`。
- V2 正常运行不得创建 `runs` 或持续落盘事件/帧；只允许设置和按需诊断持久化。
- V2 设置根目录为 `%LOCALAPPDATA%\异环自动钓鱼V2\`，诊断 ZIP 最多保留 5 份。
- 内存事件保留最近 10 秒；帧最高 10 FPS、最长边 480、JPEG 质量 50；错误图最长边 1280、质量 75。
- 主动报告必须先取消倒计时/恢复令牌并释放所有输入；未绑定或无帧时仍生成报告。
- 自动报告只响应非 `E_USER_PAUSE` 错误，同一暂停事件只生成一份；诊断失败不得影响主功能。
- 目标平台为 64 位 Windows 10/11；自动矩阵覆盖 100%、125%、150%、175%、200% 和 1280×720 至 3840×2160。
- 最终 EXE 内嵌清单必须含 `requireAdministrator`、`uiAccess=false`、`PerMonitorV2` 和 `true/pm`。
- V1 `v1.0.0` 必须归档当前已验收哈希；V2 通过后发布 `v2.0.0` 并替换本地根目录发布物。
- 所有测试文件只放在 `try/`；每阶段更新验收与进展文档；高风险替换前备份到 `D:\0文件夹\备份`。

---

## 文件结构映射

- Create: `src/auto_fishing/product.py` — V1/V2 产品配置、版本标题和数据目录解析。
- Create: `src/auto_fishing/storage/recording.py` — V1/V2 共用的帧事件字段和缩略图编码。
- Create: `src/auto_fishing/storage/memory_diagnostics.py` — 最近 10 秒事件/帧内存环形缓冲区。
- Create: `src/auto_fishing/storage/diagnostic_bundles.py` — ZIP 生成、最多 5 份保留、异步结果与资源管理器打开。
- Create: `src/auto_fishing/__main_v2__.py` — V2 独立启动入口。
- Create: `packaging/auto_fishing_v2.spec` — V2 单文件构建规格。
- Create: `scripts/build_v2.ps1` — V2 测试、构建、清单校验和哈希脚本。
- Create: `try/tests/test_v2_diagnostics.py` — 内存环和诊断包测试。
- Modify: `src/auto_fishing/storage/runtime_logging.py` — 复用共用记录函数，保持 V1 行为。
- Modify: `src/auto_fishing/app.py` — 产品配置、V2 服务装配、诊断结果交付和关闭顺序。
- Modify: `src/auto_fishing/automation/engine.py` — 自动报告、主动报告和最新客户区帧上下文。
- Modify: `src/auto_fishing/ui/main_window.py` — V2 标题、“报告错误”、路径显示与打开位置按钮。
- Modify: `src/auto_fishing/platform/windowing.py` — DPI 回退、DPI 状态和控制窗位置夹取。
- Modify: `src/auto_fishing/platform/on_screen_keyboard.py` — 低分辨率安全目标宽度。
- Modify: `packaging/app.manifest`、`scripts/verify_release.py` — DPI 回退清单与最终资源校验。
- Modify: `try/tests/test_storage.py`、`test_engine.py`、`test_ui_smoke.py`、`test_capture_window.py`、`test_on_screen_keyboard.py`、`test_geometry.py`、`test_packaging.py` — 对应回归与矩阵。
- Modify: `AGENTS.md`、`doc/验收标准.md`、`doc/进展记录/2026-7-14.md` — 当前版本、存储、命令和证据。

---

### Task 1: 共用记录字段与 V2 内存环

**Files:**
- Create: `src/auto_fishing/storage/recording.py`
- Create: `src/auto_fishing/storage/memory_diagnostics.py`
- Modify: `src/auto_fishing/storage/runtime_logging.py`
- Create: `try/tests/test_v2_diagnostics.py`
- Modify: `try/tests/test_storage.py`

**Interfaces:**
- Produces: `thumbnail(frame: np.ndarray, max_edge: int) -> np.ndarray`
- Produces: `frame_event_fields(*, observation: SceneObservation, state_before: FishingState, snapshot: RuntimeSnapshot, frame_timestamp: float, now_monotonic: float) -> dict[str, Any]`
- Produces: `MemoryDiagnosticRecorder.start()`, `event()`, `record_frame()`, `snapshot()`, `raise_if_failed()`, `cleanup()`, `close()`
- Produces: `DiagnosticSnapshot(events, frames, dropped_items)` and `BufferedDiagnosticFrame(name, monotonic, jpeg)`

- [ ] **Step 1: Write failing tests for time retention, 10 FPS sampling and no disk writes**

```python
def test_memory_recorder_keeps_ten_seconds_and_samples_frames_at_ten_fps(tmp_path):
    clock = [0.0]
    recorder = MemoryDiagnosticRecorder(
        clock=lambda: clock[0],
        now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    recorder.start()
    for index in range(121):
        clock[0] = index / 10
        recorder.event("tick", index=index)
        recorder.record_frame(
            np.zeros((720, 1280, 3), np.uint8),
            observation=SceneObservation(),
            state_before=FishingState.CONTROL,
            snapshot=RuntimeSnapshot(FishingState.CONTROL, 0, 1, 30.0),
            frame_timestamp=clock[0],
            now_monotonic=clock[0],
        )
    snapshot = recorder.snapshot()
    assert snapshot.events[0]["monotonic"] >= 2.0
    assert len(snapshot.frames) <= 101
    decoded = cv2.imdecode(
        np.frombuffer(snapshot.frames[-1].jpeg, dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    assert max(decoded.shape[:2]) <= 480
    assert list(tmp_path.iterdir()) == []
```

- [ ] **Step 2: Run the focused tests and verify the new module is missing**

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_v2_diagnostics.py try/tests/test_storage.py -q`

Expected: collection fails with `ModuleNotFoundError: auto_fishing.storage.memory_diagnostics`.

- [ ] **Step 3: Extract shared recording helpers without changing V1 output**

```python
def thumbnail(frame: np.ndarray, max_edge: int) -> np.ndarray:
    height, width = frame.shape[:2]
    scale = min(1.0, max_edge / max(height, width))
    if scale == 1.0:
        return np.ascontiguousarray(frame).copy()
    return cv2.resize(
        frame,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )

def encode_jpeg(frame: np.ndarray, *, max_edge: int, quality: int) -> bytes:
    encoded, payload = cv2.imencode(
        ".jpg", thumbnail(frame, max_edge), [cv2.IMWRITE_JPEG_QUALITY, quality]
    )
    if not encoded:
        raise OSError("JPEG 编码失败")
    return payload.tobytes()
```

Move the existing `frame.processed` field construction into `frame_event_fields`; make `RuntimeLogStore.record_frame` call it and keep its filenames, queue behavior and JSON schema unchanged.

- [ ] **Step 4: Implement the thread-safe memory recorder**

```python
class MemoryDiagnosticRecorder:
    _WINDOW_SECONDS = 10.0
    _FRAME_INTERVAL = 0.1

    def event(self, name: str, **fields: Any) -> None:
        monotonic = self._clock()
        record = self._event_record(name, fields, monotonic)
        with self._lock:
            self._events.append(record)
            self._prune_locked(monotonic)

    def record_frame(self, frame: np.ndarray, *, observation, state_before,
                     snapshot, frame_timestamp: float,
                     now_monotonic: float) -> int:
        fields = frame_event_fields(
            observation=observation, state_before=state_before,
            snapshot=snapshot, frame_timestamp=frame_timestamp,
            now_monotonic=now_monotonic,
        )
        with self._lock:
            self._sequence += 1
            index = self._sequence
        self._append_event("frame.processed", fields, now_monotonic)
        if now_monotonic - self._last_frame_sample < self._FRAME_INTERVAL:
            return index
        try:
            jpeg = encode_jpeg(frame, max_edge=480, quality=50)
        except Exception:
            with self._lock:
                self._dropped_items += 1
            return index
        with self._lock:
            self._last_frame_sample = now_monotonic
            self._frames.append(
                BufferedDiagnosticFrame(f"{index:08d}.jpg", now_monotonic, jpeg)
            )
            self._prune_locked(now_monotonic)
        return index
```

`start/cleanup/raise_if_failed` are no-ops, `snapshot` returns immutable copies, and `close` clears memory without touching disk.

- [ ] **Step 5: Run focused and V1 storage regression tests**

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_v2_diagnostics.py try/tests/test_storage.py -q`

Expected: PASS; existing V1 event JSON and JPEG dimensions assertions remain unchanged.

- [ ] **Step 6: Commit**

```powershell
git add src/auto_fishing/storage/recording.py src/auto_fishing/storage/memory_diagnostics.py src/auto_fishing/storage/runtime_logging.py try/tests/test_v2_diagnostics.py try/tests/test_storage.py
git commit -m "feat: add in-memory diagnostic recorder"
```

---

### Task 2: 诊断 ZIP 生成、保留和打开位置

**Files:**
- Create: `src/auto_fishing/storage/diagnostic_bundles.py`
- Modify: `try/tests/test_v2_diagnostics.py`

**Interfaces:**
- Consumes: `MemoryDiagnosticRecorder.snapshot() -> DiagnosticSnapshot`
- Produces: `DiagnosticReportResult(path: Path | None, error: str | None)`
- Produces: `DiagnosticBundleService.subscribe(callback: Callable[[DiagnosticReportResult], None])`, `request_report(*, report_type: str, code: str, detail: str, state: str, frame: np.ndarray | None, context: Mapping[str, Any]) -> Future[DiagnosticReportResult]`, `open_location(path: Path)`, `close(timeout: float = 2.0)`
- Produces: `NullDiagnosticsStore.cleanup()` and `save()` as disk-free compatibility no-ops for the existing application service slot.

- [ ] **Step 1: Write failing tests for ZIP contents, missing frames, max-five cleanup and Explorer arguments**

```python
def test_bundle_contains_metadata_events_frames_and_error_image(tmp_path):
    recorder = MemoryDiagnosticRecorder(
        clock=lambda: 1.0,
        now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    recorder.event("test.event", value=1)
    recorder.record_frame(
        np.zeros((720, 1280, 3), np.uint8),
        observation=SceneObservation(),
        state_before=FishingState.CONTROL,
        snapshot=RuntimeSnapshot(FishingState.CONTROL, 0, 1, 30.0),
        frame_timestamp=1.0,
        now_monotonic=1.0,
    )
    service = DiagnosticBundleService(
        tmp_path / "诊断",
        recorder=recorder,
        version="2.0.0",
        now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
        system_info=lambda: {"windows": "test"},
    )
    result = service.request_report(
        report_type="automatic", code="E_VISION", detail="识别失败",
        state="已暂停", frame=np.zeros((1080, 1920, 3), np.uint8),
        context={"client_rect": [0, 0, 1920, 1080]},
    ).result()
    assert result.error is None
    with ZipFile(result.path) as archive:
        assert {"error.json", "events.jsonl", "error.jpg"} <= set(archive.namelist())
        assert any(name.startswith("frames/") for name in archive.namelist())
        metadata = json.loads(archive.read("error.json"))
        assert metadata["version"] == "2.0.0"
        assert metadata["code"] == "E_VISION"

def test_bundle_retains_only_five_matching_zip_files(tmp_path):
    unrelated = tmp_path / "诊断" / "unrelated.zip"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"unrelated")
    service = DiagnosticBundleService(
        unrelated.parent,
        recorder=MemoryDiagnosticRecorder(),
        version="2.0.0",
        now=incrementing_utc_clock(),
        system_info=lambda: {"windows": "test"},
    )
    paths = [
        service.request_report(
            report_type="manual_report",
            code="MANUAL_REPORT",
            detail="用户主动报告错误",
            state="未绑定",
            frame=None,
            context={},
        ).result().path
        for _ in range(6)
    ]
    assert not paths[0].exists()
    assert len(list(service.root.glob("yihuan-v2-*.zip"))) == 5
    assert unrelated.exists()
```

Define `incrementing_utc_clock` in the test file as a closure that returns `2026-07-14T00:00:00Z` plus one second per call, guaranteeing six distinct names without sleeps.

- [ ] **Step 2: Run the tests and verify the bundle service is missing**

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_v2_diagnostics.py -q`

Expected: FAIL importing `DiagnosticBundleService`.

- [ ] **Step 3: Implement atomic ZIP creation with exact contents**

```python
def _write_bundle(self, request: DiagnosticReportRequest) -> DiagnosticReportResult:
    self.root.mkdir(parents=True, exist_ok=True)
    final_path = self._unique_path(request.code)
    temp_path = final_path.with_suffix(".tmp")
    try:
        snapshot = request.snapshot
        with ZipFile(temp_path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("error.json", self._metadata_json(request, snapshot))
            archive.writestr("events.jsonl", self._events_jsonl(snapshot.events))
            for buffered in snapshot.frames:
                archive.writestr(f"frames/{buffered.name}", buffered.jpeg)
            if request.frame is not None:
                archive.writestr(
                    "error.jpg",
                    encode_jpeg(request.frame, max_edge=1280, quality=75),
                )
        temp_path.replace(final_path)
        self._cleanup_keep_five()
        result = DiagnosticReportResult(final_path, None)
    except Exception as error:
        temp_path.unlink(missing_ok=True)
        result = DiagnosticReportResult(None, str(error))
    self._publish(result)
    return result
```

Only delete files matching `yihuan-v2-*.zip` whose resolved parent equals `root`; sort by modification time and name, retain newest five.

- [ ] **Step 4: Implement non-blocking requests and safe Explorer selection**

```python
def request_report(self, **fields: Any) -> Future[DiagnosticReportResult]:
    request = DiagnosticReportRequest(
        **fields,
        snapshot=self.recorder.snapshot(),
    )
    return self._executor.submit(self._write_bundle, request)

def open_location(self, path: Path) -> None:
    resolved = path.resolve(strict=True)
    subprocess.Popen(["explorer.exe", f"/select,{resolved}"], close_fds=True)
```

Callbacks receive `DiagnosticReportResult`; callback exceptions are swallowed and recorded only in the service logger. `close(2.0)` waits at most two seconds and does not raise on timeout.

`request_report` must copy the supplied frame before submitting the job, so both the 10-second snapshot and error frame are frozen at request time rather than when the worker eventually runs. `NullDiagnosticsStore.cleanup` returns `None`; `save` returns an empty string and never creates a directory.

- [ ] **Step 5: Run diagnostics tests**

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_v2_diagnostics.py -q`

Expected: PASS for Unicode path, no-frame metadata, atomic temp cleanup, max-five retention, callback isolation and exact Explorer argument list.

- [ ] **Step 6: Commit**

```powershell
git add src/auto_fishing/storage/diagnostic_bundles.py try/tests/test_v2_diagnostics.py
git commit -m "feat: generate bounded diagnostic bundles"
```

---

### Task 3: V1/V2 产品配置和无日志装配

**Files:**
- Create: `src/auto_fishing/product.py`
- Create: `src/auto_fishing/__main_v2__.py`
- Modify: `src/auto_fishing/app.py`
- Modify: `src/auto_fishing/storage/__init__.py`
- Modify: `try/tests/test_ui_smoke.py`

**Interfaces:**
- Produces: `ProductProfile(version, window_title, data_dir, use_disk_runtime_log, use_bundle_diagnostics)`
- Produces: `v1_profile() -> ProductProfile`, `v2_profile(environ=None) -> ProductProfile`
- Produces: `Application(*, profile: ProductProfile | None = None, services: ApplicationServices | None = None, root_factory: Callable[[], Any] = tkinter.Tk, main_window_factory: Callable[..., Any] = MainWindow, data_dir: Path | None = None)`
- Extends: `ApplicationServices.diagnostic_reporter: Any | None`

- [ ] **Step 1: Write failing V2 profile and service-wiring tests**

```python
def test_v2_profile_uses_local_app_data_and_explicit_version(tmp_path):
    profile = v2_profile({"LOCALAPPDATA": str(tmp_path)})
    assert profile.version == "2.0.0"
    assert profile.window_title == "异环自动钓鱼 V2"
    assert profile.data_dir == tmp_path / "异环自动钓鱼V2"
    assert profile.use_disk_runtime_log is False

def test_v2_services_use_memory_recorder_and_never_create_runs(tmp_path):
    services = Application._build_services(v2_profile({"LOCALAPPDATA": str(tmp_path)}))
    assert isinstance(services.runtime_log, MemoryDiagnosticRecorder)
    assert services.diagnostic_reporter is not None
    services.runtime_log.start()
    assert not (tmp_path / "异环自动钓鱼V2" / "runs").exists()
```

- [ ] **Step 2: Run the tests and verify product configuration is missing**

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_ui_smoke.py -q`

Expected: FAIL importing `auto_fishing.product`.

- [ ] **Step 3: Implement product profiles with explicit missing-LOCALAPPDATA error**

```python
@dataclass(frozen=True)
class ProductProfile:
    version: str
    window_title: str
    data_dir: Path
    use_disk_runtime_log: bool
    use_bundle_diagnostics: bool

def v2_profile(environ: Mapping[str, str] = os.environ) -> ProductProfile:
    local_app_data = environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA 未配置，无法确定 V2 数据目录")
    return ProductProfile(
        version="2.0.0", window_title="异环自动钓鱼 V2",
        data_dir=Path(local_app_data) / "异环自动钓鱼V2",
        use_disk_runtime_log=False, use_bundle_diagnostics=True,
    )
```

`v1_profile` keeps `D:\29551\异环自动钓鱼数据`, title “异环自动钓鱼”, disk logs and legacy diagnostics.

- [ ] **Step 4: Branch service construction without duplicating automation core**

```python
if profile.use_disk_runtime_log:
    quota = StorageQuotaManager(data_dir)
    quota.initialize()
    recorder = RuntimeLogStore(data_dir / "runs", quota=quota)
    diagnostics = DiagnosticsStore(data_dir / "diagnostics", quota=quota)
    reporter = None
else:
    recorder = MemoryDiagnosticRecorder()
    diagnostics = NullDiagnosticsStore()
    reporter = DiagnosticBundleService(
        data_dir / "diagnostics", recorder=recorder, version=profile.version
    )
```

Build the same `WindowService`, `SceneRecognizer`, `AutomationCore`, input backend and `AutomationEngine` exactly once using `recorder`. V2 must not instantiate `StorageQuotaManager` or `RuntimeLogStore`.

- [ ] **Step 5: Add the dedicated entrypoint and preserve V1 default entrypoint**

```python
# src/auto_fishing/__main_v2__.py
from auto_fishing.app import Application
from auto_fishing.product import v2_profile

if __name__ == "__main__":
    Application(profile=v2_profile()).run()
```

- [ ] **Step 6: Run wiring regression tests**

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_ui_smoke.py try/tests/test_storage.py -q`

Expected: PASS; V1 data path and quota assertions remain unchanged, V2 creates no `runs` directory.

- [ ] **Step 7: Commit**

```powershell
git add src/auto_fishing/product.py src/auto_fishing/__main_v2__.py src/auto_fishing/app.py src/auto_fishing/storage/__init__.py try/tests/test_ui_smoke.py
git commit -m "feat: add dedicated v2 application profile"
```

---

### Task 4: 自动错误、主动报告和 V2 界面

**Files:**
- Modify: `src/auto_fishing/automation/engine.py`
- Modify: `src/auto_fishing/app.py`
- Modify: `src/auto_fishing/ui/main_window.py`
- Modify: `try/tests/test_engine.py`
- Modify: `try/tests/test_ui_smoke.py`
- Modify: `try/tests/test_v2_diagnostics.py`

**Interfaces:**
- Produces: `AutomationEngine.report_error() -> Future | None`
- Produces: `AutomationEngine.open_report_location(path: Path) -> None`
- Produces: `AppController.report_error()` and `open_report_location(path)`
- Produces: `MainWindow.show_diagnostic_result(result)`

- [ ] **Step 1: Write failing tests for automatic report deduplication and manual safe pause**

```python
def test_engine_auto_error_reports_once_after_inputs_are_released(tmp_path):
    reporter = RecordingReporter()
    engine, core, input_service, _window, _source = make_engine(
        tmp_path, diagnostic_reporter=reporter
    )
    frame = np.zeros((1080, 1920, 3), np.uint8)
    engine._pause("E_VISION", "识别失败", frame)
    engine._pause("E_VISION", "识别失败", frame)
    assert input_service.events.count("release") >= 1
    assert len(reporter.requests) == 1
    assert reporter.requests[0]["report_type"] == "automatic"

def test_manual_report_releases_inputs_even_when_unbound(tmp_path):
    reporter = RecordingReporter()
    engine, core, input_service, _window, _source = make_engine(
        tmp_path, diagnostic_reporter=reporter, bind=False
    )
    engine.report_error()
    assert input_service.events.count("release") == 1
    assert reporter.requests[0]["report_type"] == "manual_report"
    assert reporter.requests[0]["frame"] is None
```

Add `RecordingReporter.request_report(**fields)` that appends `fields` and returns a completed `Future`; extend the existing `make_engine` helper with `diagnostic_reporter=None` and `bind=True`, pass the reporter into `AutomationEngine`, and call `engine.bind(BOUND)` only when `bind` is true.

- [ ] **Step 2: Write failing UI tests**

```python
def test_v2_window_shows_version_and_report_controls(root):
    window = MainWindow(root, FakeController(), FakeSettings(),
                        window_title="异环自动钓鱼 V2",
                        diagnostics_enabled=True)
    assert root.title() == "异环自动钓鱼 V2"
    window.report_button.invoke()
    assert window.controller.calls[-1] == "report_error"

def test_report_result_enables_open_location(root, tmp_path):
    window = make_v2_window(root)
    path = tmp_path / "诊断.zip"
    path.write_bytes(b"zip")
    window.show_diagnostic_result(DiagnosticReportResult(path, None))
    assert str(path) in window.diagnostic_path_var.get()
    assert window.open_report_button.instate(["!disabled"])
```

- [ ] **Step 3: Run focused tests and verify missing methods/controls**

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_engine.py try/tests/test_ui_smoke.py -q`

Expected: FAIL because `diagnostic_reporter`, `report_error` and report controls do not exist.

- [ ] **Step 4: Add latest-client-frame and automatic reporting without changing V1 diagnostics**

```python
if self.diagnostic_reporter is not None:
    should_report = actual_code != "E_USER_PAUSE" and not self._diagnostic_recorded
    if should_report:
        self._diagnostic_recorded = True
        diagnostic_frame = (
            self._last_client_frame.copy()
            if self._last_client_frame is not None else None
        )
        self.diagnostic_reporter.request_report(
            report_type="automatic", code=actual_code,
            detail=actual_detail, state=self.core.snapshot.state.value,
            frame=diagnostic_frame, context=self._diagnostic_context(),
        )
else:
    save_diagnostic_now = False
    with self._pause_lock:
        should_save = save_diagnostic or actual_code == "E_INPUT"
        if should_save and not self._diagnostic_recorded and frame is not None:
            self._diagnostic_recorded = True
            save_diagnostic_now = True
    if save_diagnostic_now:
        self.diagnostics.save(
            frame,
            actual_code,
            actual_detail,
            progress_frames=(
                tuple(self._progress_frames)
                if actual_code == "E_PROGRESS_LOST" else ()
            ),
        )
```

Set `_last_client_frame` only after a successful client crop; clear it on start/cancel. Reset report deduplication only after a real resume/new start, as existing `_diagnostic_recorded` already does.

- [ ] **Step 5: Implement manual report and controller cancellation**

```python
def report_error(self) -> Any | None:
    self.core.block_input()
    self.core.release_inputs()
    if self.core.snapshot.state not in {FishingState.UNBOUND, FishingState.COMPLETE}:
        self._pause("E_USER_PAUSE", "主动报告错误", self._last_frame,
                    save_diagnostic=False, replace_existing=True)
    if self.diagnostic_reporter is None:
        return None
    return self.diagnostic_reporter.request_report(
        report_type="manual_report", code="MANUAL_REPORT",
        detail="用户主动报告错误", state=self.core.snapshot.state.value,
        frame=self._copy_last_client_frame(), context=self._diagnostic_context(),
    )
```

`AppController.report_error` increments countdown generation, clears pending callbacks/resume, then calls the engine. Exceptions are delivered to the UI queue.

- [ ] **Step 6: Add V2-only controls and thread-safe result display**

Extend `MainWindow.__init__` with keyword-only `window_title` and `diagnostics_enabled`. Put “报告错误” and “打开文件位置” on a third button row; set the V2 initial/minimum height to the measured widget request height after `update_idletasks`, never below 280 pixels. Application subscribes once to `DiagnosticBundleService` and schedules `root.after(0, lambda current=result: main_window.show_diagnostic_result(current))`.

- [ ] **Step 7: Report application-level exceptions when the V2 reporter already exists**

In `Application.run`, when the service graph has been built and a later startup/mainloop/cleanup exception occurs, call `diagnostic_reporter.request_report(report_type="automatic", code="E_APPLICATION", detail=str(error), state="应用异常", frame=None, context={"phase": "run"})`. During cleanup, close the reporter before clearing the memory recorder; wait at most two seconds. A failure to create this report is appended to cleanup warnings but never replaces the original exception.

- [ ] **Step 8: Run engine, UI and diagnostics tests**

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_engine.py try/tests/test_ui_smoke.py try/tests/test_v2_diagnostics.py -q`

Expected: PASS; V1 UI remains 400×240 and has no report row, V2 shows V2 and both diagnostic controls.

- [ ] **Step 9: Commit**

```powershell
git add src/auto_fishing/automation/engine.py src/auto_fishing/app.py src/auto_fishing/ui/main_window.py try/tests/test_engine.py try/tests/test_ui_smoke.py try/tests/test_v2_diagnostics.py
git commit -m "feat: add automatic and manual error reports"
```

---

### Task 5: DPI、多显示器与低分辨率兼容性补缺

**Files:**
- Modify: `src/auto_fishing/platform/windowing.py`
- Modify: `src/auto_fishing/platform/on_screen_keyboard.py`
- Modify: `src/auto_fishing/ui/main_window.py`
- Modify: `packaging/app.manifest`
- Modify: `scripts/verify_release.py`
- Modify: `try/tests/test_capture_window.py`
- Modify: `try/tests/test_on_screen_keyboard.py`
- Modify: `try/tests/test_geometry.py`
- Modify: `try/tests/test_ui_smoke.py`
- Modify: `try/tests/test_packaging.py`

**Interfaces:**
- Produces: `WindowService.enable_dpi_awareness() -> str`
- Produces: `WindowService.clamp_window_position(x, y, width, height) -> tuple[int, int]`
- Produces: `_target_outer_width(monitor_rect, game_client) -> int`
- Extends: `validate_manifest()` to require both DPI elements.

- [ ] **Step 1: Write failing low-resolution OSK tests**

```python
@pytest.mark.parametrize(
    ("monitor", "game", "expected_width"),
    [
        (Rect(0, 0, 1280, 720), Rect(0, 0, 1280, 720), 1075),
        (Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1080), 1365),
        (Rect(0, 0, 2560, 1440), Rect(0, 0, 2560, 1440), 1707),
        (Rect(-1280, 0, 0, 720), Rect(-1280, 0, 0, 720), 1075),
    ],
)
def test_keyboard_target_width_leaves_ready_roi_visible(monitor, game, expected_width):
    assert _target_outer_width(monitor, game) == expected_width
```

The 1280 expectation is `READY_ROI.to_pixels(game).left - monitor.left = round(1280 * 0.84) = 1075`; the final real geometry must still pass the existing minimum client and aspect checks.

- [ ] **Step 2: Write failing DPI fallback, manifest and placement tests**

```python
def test_dpi_awareness_tries_v2_then_shcore_then_legacy():
    service = make_window_service(dpi_context=False, shcore_result=0)
    assert service.enable_dpi_awareness() == "per_monitor"
    assert service.user32.legacy_calls == 0

def test_window_position_is_clamped_into_nearest_monitor_work_area():
    service = make_window_service(
        work_areas=[Rect(-1920, 0, 0, 1080), Rect(0, 0, 1920, 1080)]
    )
    assert service.clamp_window_position(5000, 5000, 400, 280) == (1520, 800)

def test_manifest_contains_modern_and_legacy_per_monitor_dpi():
    manifest = read_source_manifest()
    assert find_text(manifest, "dpiAwareness") == "PerMonitorV2"
    assert find_text(manifest, "dpiAware") == "true/pm"
```

- [ ] **Step 3: Run focused compatibility tests and verify failures**

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_capture_window.py try/tests/test_on_screen_keyboard.py try/tests/test_geometry.py try/tests/test_ui_smoke.py try/tests/test_packaging.py -q`

Expected: FAIL on 1280 width, missing shcore fallback/clamp method and missing `dpiAware` element.

- [ ] **Step 4: Implement the low-resolution target width as the only OSK behavior change**

```python
def _target_outer_width(monitor_rect: Rect, game_client: Rect) -> int:
    preferred = min(
        monitor_rect.width,
        max(_CANONICAL_OUTER_WIDTH, round(game_client.width * 2 / 3)),
    )
    ready_left = READY_ROI.to_pixels(game_client).left
    max_without_ready_overlap = ready_left - monitor_rect.left
    return max(1, min(preferred, max_without_ready_overlap))
```

Call this helper from `ensure`; retain `_read_geometry` and `_validate_placement` unchanged so unsupported small layouts still fail safely.

- [ ] **Step 5: Implement DPI fallback and virtual desktop clamp**

```python
def enable_dpi_awareness(self) -> str:
    if self._try_per_monitor_v2():
        self.dpi_awareness = "per_monitor_v2"
    elif self._try_shcore_per_monitor():
        self.dpi_awareness = "per_monitor"
    elif self._try_legacy_system_dpi():
        self.dpi_awareness = "system"
    else:
        self.dpi_awareness = "manifest_or_unknown"
    return self.dpi_awareness

def clamp_window_position(self, x: int, y: int, width: int, height: int) -> tuple[int, int]:
    work = self.nearest_monitor_work_rect(Rect(x, y, x + width, y + height))
    return (
        min(max(x, work.left), max(work.left, work.right - width)),
        min(max(y, work.top), max(work.top, work.bottom - height)),
    )
```

Construct `_WinRect(x, y, x + width, y + height)`, call `self.user32.MonitorFromRect(ctypes.byref(native_rect), MONITOR_DEFAULTTONEAREST)`, then call the existing `GetMonitorInfoW` binding and read `info.rcWork`. Declare both native functions with pointer-width `argtypes/restype`. `MainWindow` applies the injected clamp before calling `root.geometry`; this preserves negative coordinates while recovering an off-screen saved position after a monitor-topology change.

- [ ] **Step 6: Add legacy DPI manifest and enforce it in final-resource verifier**

```xml
<dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true/pm</dpiAware>
<dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2</dpiAwareness>
```

`validate_manifest` must raise if either element is absent or has another value; success output becomes `RELEASE_MANIFEST_OK requireAdministrator uiAccess=false dpi=PerMonitorV2 fallback=true/pm`.

- [ ] **Step 7: Expand the resolution/scale matrix**

Parameterize normalized ROI/crop and injected physical-coordinate tests across `(1280,720)`, `(1600,900)`, `(1920,1080)`, `(2560,1440)`, `(3440,1440)`, `(3840,2160)` and scale labels `100,125,150,175,200`. Assert physical client crop shape and normalized ROI percentages, not hard-coded logical-to-physical multipliers. Preserve cross-monitor rejection.

- [ ] **Step 8: Run compatibility tests**

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_capture_window.py try/tests/test_on_screen_keyboard.py try/tests/test_geometry.py try/tests/test_ui_smoke.py try/tests/test_packaging.py -q`

Expected: PASS for all matrix cases and existing error-5/negative-coordinate tests.

- [ ] **Step 9: Commit**

```powershell
git add src/auto_fishing/platform/windowing.py src/auto_fishing/platform/on_screen_keyboard.py src/auto_fishing/ui/main_window.py packaging/app.manifest scripts/verify_release.py try/tests/test_capture_window.py try/tests/test_on_screen_keyboard.py try/tests/test_geometry.py try/tests/test_ui_smoke.py try/tests/test_packaging.py
git commit -m "feat: harden v2 windows display compatibility"
```

---

### Task 6: V2 单文件构建与发布物验证

**Files:**
- Create: `packaging/auto_fishing_v2.spec`
- Create: `scripts/build_v2.ps1`
- Modify: `try/smoke_exe.ps1`
- Modify: `try/tests/test_packaging.py`

**Interfaces:**
- Produces: `dist/异环自动钓鱼V2.exe`
- Produces: `scripts/build_v2.ps1 -PythonPath <path>`
- Extends: smoke script with optional `-TargetPath`, defaulting to the existing V1 path for backward compatibility; the name deliberately avoids the existing safety assertion that forbids WMI `ExecutablePath` filtering.

- [ ] **Step 1: Write failing packaging structure tests**

```python
def test_v2_spec_uses_v2_entry_and_filename():
    spec = (ROOT / "packaging/auto_fishing_v2.spec").read_text("utf-8")
    assert "src/auto_fishing/__main_v2__.py" in spec
    assert "name='异环自动钓鱼V2'" in spec
    assert "console=False" in spec
    assert "uac_admin=True" in spec

def test_v2_build_runs_tests_verifies_manifest_and_hash():
    script = (ROOT / "scripts/build_v2.ps1").read_text("utf-8-sig")
    assert "auto_fishing_v2.spec" in script
    assert "dist\\异环自动钓鱼V2.exe" in script
    assert "verify_release.py" in script
    assert "Get-FileHash -Algorithm SHA256" in script
```

- [ ] **Step 2: Run packaging tests and verify missing files**

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_packaging.py -q`

Expected: FAIL because V2 spec/build script do not exist.

- [ ] **Step 3: Create V2 spec by reusing the existing dependency collection and manifest**

```python
dxcam_datas, dxcam_binaries, dxcam_hiddenimports = collect_all('dxcam')
root = Path(SPECPATH).parent
manifest = str(root / 'packaging' / 'app.manifest')
a = Analysis(
    [str(root / 'src/auto_fishing/__main_v2__.py')],
    pathex=[str(root / 'src')],
    binaries=dxcam_binaries,
    datas=dxcam_datas,
    hiddenimports=dxcam_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='异环自动钓鱼V2',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    manifest=manifest,
    uac_admin=True,
)
```

- [ ] **Step 4: Create UTF-8 BOM PowerShell build script and parameterize smoke**

`build_v2.ps1` follows `build.ps1`: resolve `.venv` or override, run full `try/tests`, build V2 spec, verify the final embedded manifest, print SHA256. `try/smoke_exe.ps1 -TargetPath` must still track and stop only the launched process tree.

- [ ] **Step 5: Run packaging tests**

Run: `$env:PYTHONPATH='src'; py -3.13 -m pytest try/tests/test_packaging.py -q`

Expected: PASS, including UTF-8 BOM checks for both build scripts.

- [ ] **Step 6: Commit**

```powershell
git add packaging/auto_fishing_v2.spec scripts/build_v2.ps1 try/smoke_exe.ps1 try/tests/test_packaging.py
git commit -m "build: add v2 onefile release pipeline"
```

---

### Task 7: 全量验证、文档、V1/V2 发布和本地替换

**Files:**
- Modify: `AGENTS.md`
- Modify: `doc/验收标准.md`
- Modify: `doc/进展记录/2026-7-14.md`
- Build: `dist/异环自动钓鱼V2.exe`
- Replace: root `异环自动钓鱼.exe` → root `异环自动钓鱼V2.exe` only after V1 remote archive and local backup verify.

**Interfaces:**
- Consumes: all previous tasks.
- Produces: GitHub Releases `v1.0.0` and `v2.0.0`, clean synced `main`, root V2 executable.

- [ ] **Step 1: Run source compilation and the complete test suite**

Run:

```powershell
$env:PYTHONPATH='src'
py -3.13 -m compileall -q src try/tests scripts
py -3.13 -m pytest try/tests -q
```

Expected: all tests PASS; count is at least the current 359 plus new V2 tests.

- [ ] **Step 2: Run a fixed 412-frame slice from the retained real control run**

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

Expected: `REAL_CONTROL_REPLAY_OK 412`，最大样本数不超过 15，动作只包含 `left/right/release`，最后一次缺失观测将样本数清零。

- [ ] **Step 3: Build and verify V2**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_v2.ps1 -PythonPath C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe
py -3.13 scripts/verify_release.py dist/异环自动钓鱼V2.exe
powershell -ExecutionPolicy Bypass -File try/smoke_exe.ps1 -TargetPath dist/异环自动钓鱼V2.exe
Get-FileHash -Algorithm SHA256 -LiteralPath dist/异环自动钓鱼V2.exe
```

Expected: full tests PASS during build, manifest output includes both DPI modes, smoke outputs `SMOKE_OK`, hash is recorded.

- [ ] **Step 4: Verify normal V2 startup writes no runtime log**

Use an isolated `LOCALAPPDATA` under `try/output/v2-localappdata`, start and close the V2 smoke instance, then assert no `runs` directory and no loose frame/event files exist. A config file is permitted; diagnostics must be absent until a report is requested.

- [ ] **Step 5: Perform Windows 11 manual acceptance**

Record exact results for UAC, title, binding, OSK F/A/D, multi-round loop, manual report, automatic error report, Explorer selection and sixth-report eviction. Test window/borderless/fullscreen modes available on the current machine. Do not operate the game unless the user has it open and authorizes the real test at that moment.

- [ ] **Step 6: Update long-term docs and acceptance evidence**

Update `AGENTS.md` with V2 current state, V1/V2 entry/build commands, V2 storage paths and no-log behavior. Add reproducible V2 acceptance sections to `doc/验收标准.md`. Append one exact-time progress section including files, commands, counts, hashes, external paths, errors and Windows 10 real-test status.

- [ ] **Step 7: Commit implementation documentation**

```powershell
git add AGENTS.md doc/验收标准.md doc/进展记录/2026-7-14.md
git commit -m "docs: record v2 release acceptance"
```

- [ ] **Step 8: Archive the exact V1 binary before local replacement**

Verify root `异环自动钓鱼.exe` SHA256 equals `9A4768D6930F939C32EE8AB23DABA3BA4B98B03351ECCEE9A7ECD96225E52CA4`. Create GitHub Release `v1.0.0` at source commit `6602974`, upload it as `异环自动钓鱼V1.exe`, and verify remote asset size/hash. Back up the same file to `D:\0文件夹\备份\异环自动钓鱼-v1-before-v2-YYYYMMDD-HHmmss\`; retain at most two backups of this class.

- [ ] **Step 9: Merge to main, delete branch and push**

Run focused tests again after merge, then full suite. Merge `codex/feat-v2-portable-diagnostics` to `main`, delete the task branch, confirm no extra branch/worktree remains, and push `main` to the private repository.

- [ ] **Step 10: Publish V2 and replace the local root executable**

Create GitHub Release `v2.0.0` at the final merged commit and upload `dist/异环自动钓鱼V2.exe`. Verify remote size/hash. Copy the verified candidate to a same-volume temporary root path, verify hash, atomically move it to root `异环自动钓鱼V2.exe`, then remove root `异环自动钓鱼.exe`. Re-run embedded manifest verification and smoke against the root V2 file.

- [ ] **Step 11: Final state audit**

Assert:

```text
git status: clean main tracking origin/main
root: only 异环自动钓鱼V2.exe as current release
GitHub: v1.0.0 and v2.0.0 assets present and hashes verified
V2 normal data: no runs/events/frames
V2 diagnostics: at most five ZIP files
Windows 10: explicitly marked untested unless real evidence was produced
```

If any release or replacement verification fails, restore the V1 backup, do not delete the task branch, and record the blocker instead of claiming completion.

---

## Plan Self-Review

- Spec coverage: product identity, V1 reuse, normal no-log, 10-second buffer, automatic/manual report, ZIP contents/retention, UI opening, DPI/low-resolution/multi-monitor compatibility, packaging, Windows 10 caveat, V1/V2 releases and rollback are each assigned to a task.
- Placeholder scan: no `TBD`, `TODO`, “implement later”, unspecified tests or undefined later-task dependencies remain.
- Type consistency: recorder, report result, product profile, engine/controller/UI methods and build artifact names are introduced before downstream use and use one spelling throughout.
- Scope: recognition/control behavior is explicitly excluded; all compatibility work stays in existing platform/packaging boundaries.
