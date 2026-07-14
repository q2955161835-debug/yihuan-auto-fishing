# D 盘数据迁移与 100 MiB 配额实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将全部生产数据迁移到 `D:\29551\异环自动钓鱼数据\`，并让整个数据根目录在每次稳定写入返回前保持不超过 100 MiB，随后构建、合并并推送 `main`。

**Architecture:** 新增共享 `StorageQuotaManager`，启动时扫描一次，正常写入只登记字节增量，超限时才重新扫描并依次删除旧运行、旧诊断、活动运行最早帧和事件日志最早完整行。设置、诊断和运行日志继续保持各自职责，通过可选的共享配额对象接入；实际 C→D 迁移在发布阶段离线完成并用全量 SHA256 清单验证。

**Tech Stack:** Python 3.13、`pathlib`、`threading.RLock`、Tkinter 应用装配、pytest 9.1、PowerShell、PyInstaller 6.19、Git。

## Global Constraints

- 新数据根目录固定为 `D:\29551\异环自动钓鱼数据\`。
- 整个数据根目录硬上限为 104,857,600 字节，包含运行日志、帧、诊断、配置和未知普通文件。
- `config.json` 和最新完整事件必须保留；未知文件计入容量但不自动删除。
- 清理顺序固定为旧已结束运行 → 旧诊断组 → 活动/最新运行最早帧 → `events.jsonl` 最早完整行。
- 不改变截图 480 像素最长边、JPEG 质量 50、识别、状态机、输入或屏幕键盘逻辑。
- 所有测试文件只放在 `try/`；生产迁移不得在 C 盘创建新项目数据。
- 数据删除前必须完成备份、全量 SHA256 清单核对、新 EXE 构建和哈希核对。
- 本计划在 `codex/feat-d-drive-storage-cap` 分支执行；通过后合并 `main`、删除工作树/分支并直接推送 `origin/main`。

---

### Task 0: 建立隔离工作树并确认基线

**Files:**
- No file changes.

**Interfaces:**
- Consumes: 已提交的 `codex/feat-d-drive-storage-cap` 规格和计划。
- Produces: `D:\1Folder\异环自动钓鱼\.worktrees\codex-feat-d-drive-storage-cap` 隔离工作树。

- [ ] **Step 1: 从主工作区切回 main 并验证工作树目录已忽略**

```powershell
git status --short --branch
git switch main
git check-ignore -q .worktrees
if ($LASTEXITCODE -ne 0) { throw '.worktrees 未被 Git 忽略' }
```

Expected: 主工作区干净，`.worktrees` 返回已忽略。

- [ ] **Step 2: 用已有功能分支创建工作树**

```powershell
git worktree add .worktrees/codex-feat-d-drive-storage-cap codex/feat-d-drive-storage-cap
```

Expected: 工作树创建成功且分支为 `codex/feat-d-drive-storage-cap`。

- [ ] **Step 3: 运行完整基线测试**

```powershell
Set-Location .worktrees/codex-feat-d-drive-storage-cap
py -3.13 -m pytest try/tests -q
```

Expected: 340 项通过、0 项失败；否则停止实施并报告。

---

### Task 1: 测试先行实现整个数据根目录配额

**Files:**
- Create: `src/auto_fishing/storage/quota.py`
- Modify: `src/auto_fishing/storage/__init__.py`
- Modify: `try/tests/test_storage.py`

**Interfaces:**
- Consumes: 数据根目录 `Path` 和最大字节数。
- Produces: `StorageQuotaManager.initialize()`、`StorageQuotaManager.register_write()`、`StorageQuotaError`。

- [ ] **Step 1: 写入容量、删除顺序和安全边界失败测试**

在 `try/tests/test_storage.py` 增加：

```python
from auto_fishing.storage.quota import StorageQuotaError, StorageQuotaManager


def write_sized(path, size, stamp):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (stamp, stamp))
    os.utime(path.parent, (stamp, stamp))


def tree_bytes(root):
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def test_quota_deletes_old_completed_run_before_diagnostics(tmp_path):
    root = tmp_path / "data"
    write_sized(root / "config.json", 5, 1)
    write_sized(root / "runs" / "run-old" / "events.jsonl", 20, 2)
    write_sized(root / "runs" / "run-new" / "events.jsonl", 20, 4)
    write_sized(root / "diagnostics" / "incident.json", 10, 3)

    StorageQuotaManager(root, max_bytes=35).initialize()

    assert not (root / "runs" / "run-old").exists()
    assert (root / "runs" / "run-new").is_dir()
    assert (root / "diagnostics" / "incident.json").is_file()
    assert (root / "config.json").is_file()
    assert tree_bytes(root) <= 35


def test_quota_deletes_oldest_diagnostic_group_atomically(tmp_path):
    root = tmp_path / "data"
    write_sized(root / "config.json", 5, 1)
    write_sized(root / "diagnostics" / "old.png", 10, 2)
    write_sized(root / "diagnostics" / "old.json", 10, 2)
    write_sized(root / "diagnostics" / "new.png", 10, 3)

    StorageQuotaManager(root, max_bytes=15).initialize()

    assert not (root / "diagnostics" / "old.png").exists()
    assert not (root / "diagnostics" / "old.json").exists()
    assert (root / "diagnostics" / "new.png").is_file()
    assert (root / "config.json").is_file()


def test_quota_keeps_recent_frames_from_newest_run(tmp_path):
    root = tmp_path / "data"
    write_sized(root / "config.json", 5, 1)
    events = root / "runs" / "run-new" / "events.jsonl"
    write_sized(events, 5, 2)
    write_sized(events.parent / "frames" / "00000001.jpg", 10, 3)
    write_sized(events.parent / "frames" / "00000002.jpg", 10, 4)

    StorageQuotaManager(root, max_bytes=20).initialize()

    assert not (events.parent / "frames" / "00000001.jpg").exists()
    assert (events.parent / "frames" / "00000002.jpg").is_file()
    assert events.is_file()


def test_quota_trims_old_event_lines_and_keeps_latest_complete_line(tmp_path):
    root = tmp_path / "data"
    write_sized(root / "config.json", 5, 1)
    events = root / "runs" / "run-new" / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_bytes(b'{"n":1}\n{"n":2}\n{"n":3}\n')

    StorageQuotaManager(root, max_bytes=13).initialize()

    assert events.read_bytes() == b'{"n":3}\n'
    assert tree_bytes(root) <= 13


def test_quota_counts_unknown_files_but_never_deletes_them(tmp_path):
    root = tmp_path / "data"
    write_sized(root / "config.json", 5, 1)
    unknown = root / "keep.bin"
    write_sized(unknown, 20, 2)

    with pytest.raises(StorageQuotaError, match="无法清理到容量上限"):
        StorageQuotaManager(root, max_bytes=10).initialize()

    assert unknown.is_file()
    assert (root / "config.json").is_file()


def test_quota_rejects_registered_path_outside_data_root(tmp_path):
    root = tmp_path / "data"
    outside = tmp_path / "outside.log"
    outside.write_bytes(b"x")
    quota = StorageQuotaManager(root, max_bytes=100)
    quota.initialize()

    with pytest.raises(StorageQuotaError, match="超出数据根目录"):
        quota.register_write(outside, 0)
```

- [ ] **Step 2: 运行新测试并确认按预期失败**

```powershell
py -3.13 -m pytest try/tests/test_storage.py -q
```

Expected: 测试收集失败，原因为 `auto_fishing.storage.quota` 不存在。

- [ ] **Step 3: 实现最小配额管理器**

创建 `src/auto_fishing/storage/quota.py`：

```python
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import shutil
import threading


DEFAULT_MAX_BYTES = 100 * 1024 * 1024


class StorageQuotaError(RuntimeError):
    """数据目录无法恢复到配置的容量上限。"""


class StorageQuotaManager:
    def __init__(self, root: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes 必须至少为 1")
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self._known_total: int | None = None
        self._active_run: Path | None = None
        self._active_events: Path | None = None

    @property
    def total_bytes(self) -> int:
        with self._lock:
            self._known_total = self._tree_bytes(self.root)
            return self._known_total

    def initialize(self) -> None:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            self._known_total = self._tree_bytes(self.root)
            self._enforce()

    def register_write(
        self,
        path: Path,
        previous_size: int,
        *,
        active_run: Path | None = None,
        active_events: Path | None = None,
    ) -> None:
        with self._lock:
            resolved = self._inside(path)
            if active_run is not None:
                self._active_run = self._inside(active_run)
            if active_events is not None:
                self._active_events = self._inside(active_events)
            if self._known_total is None:
                self.root.mkdir(parents=True, exist_ok=True)
                self._known_total = self._tree_bytes(self.root)
            current_size = resolved.stat().st_size if resolved.is_file() else 0
            self._known_total += current_size - max(0, previous_size)
            if self._known_total > self.max_bytes:
                self._enforce()

    def _enforce(self) -> None:
        total = self._tree_bytes(self.root)
        if total <= self.max_bytes:
            self._known_total = total
            return
        active_run = self._effective_active_run()
        active_events = self._effective_events(active_run)
        for run in self._completed_runs(active_run):
            shutil.rmtree(self._inside(run))
            total = self._tree_bytes(self.root)
            if total <= self.max_bytes:
                self._known_total = total
                return
        for files in self._diagnostic_groups():
            for path in files:
                self._inside(path).unlink(missing_ok=True)
            total = self._tree_bytes(self.root)
            if total <= self.max_bytes:
                self._known_total = total
                return
        if active_run is not None:
            frames = active_run / "frames"
            if frames.is_dir():
                for path in sorted(frames.glob("*.jpg")):
                    self._inside(path).unlink(missing_ok=True)
                    total = self._tree_bytes(self.root)
                    if total <= self.max_bytes:
                        self._known_total = total
                        return
        if active_events is not None and active_events.is_file():
            other_bytes = total - active_events.stat().st_size
            self._trim_events(active_events, self.max_bytes - other_bytes)
            total = self._tree_bytes(self.root)
        self._known_total = total
        if total > self.max_bytes:
            raise StorageQuotaError(
                f"数据目录无法清理到容量上限：{total}>{self.max_bytes}"
            )

    def _effective_active_run(self) -> Path | None:
        if self._active_run is not None and self._active_run.is_dir():
            return self._active_run
        runs_root = self.root / "runs"
        runs = [path for path in runs_root.iterdir() if path.is_dir()] if runs_root.is_dir() else []
        return max(runs, key=lambda path: path.stat().st_mtime, default=None)

    def _effective_events(self, active_run: Path | None) -> Path | None:
        if self._active_events is not None and self._active_events.is_file():
            return self._active_events
        return None if active_run is None else active_run / "events.jsonl"

    def _completed_runs(self, active_run: Path | None) -> list[Path]:
        runs_root = self.root / "runs"
        if not runs_root.is_dir():
            return []
        runs = [
            path for path in runs_root.iterdir()
            if path.is_dir() and path.resolve() != active_run
        ]
        return sorted(runs, key=lambda path: path.stat().st_mtime)

    def _diagnostic_groups(self) -> list[list[Path]]:
        diagnostics = self.root / "diagnostics"
        groups: dict[str, list[Path]] = defaultdict(list)
        if not diagnostics.is_dir():
            return []
        for path in diagnostics.iterdir():
            if not path.is_file() or path.suffix not in {".png", ".json", ".jpg"}:
                continue
            stem = path.stem
            if path.suffix == ".jpg" and stem.endswith("_progress"):
                stem = stem[: -len("_progress")]
            groups[stem].append(path)
        return sorted(
            groups.values(),
            key=lambda files: max(path.stat().st_mtime for path in files),
        )

    def _trim_events(self, path: Path, budget: int) -> None:
        lines = path.read_bytes().splitlines(keepends=True)
        kept: list[bytes] = []
        used = 0
        for line in reversed(lines):
            if used + len(line) > budget:
                break
            kept.append(line)
            used += len(line)
        if lines and not kept:
            raise StorageQuotaError("最新事件行超过剩余容量预算")
        payload = b"".join(reversed(kept))
        temp = path.with_suffix(".quota.tmp")
        temp.write_bytes(payload)
        temp.replace(path)

    def _inside(self, path: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise StorageQuotaError(f"路径超出数据根目录：{resolved}") from error
        return resolved

    @staticmethod
    def _tree_bytes(root: Path) -> int:
        if not root.exists():
            return 0
        return sum(
            path.stat().st_size
            for path in root.rglob("*")
            if path.is_file()
        )
```

修改 `src/auto_fishing/storage/__init__.py`：

```python
from .quota import StorageQuotaError, StorageQuotaManager
from .runtime_logging import RuntimeLogError, RuntimeLogStore

__all__ = [
    "RuntimeLogError",
    "RuntimeLogStore",
    "StorageQuotaError",
    "StorageQuotaManager",
]
```

- [ ] **Step 4: 运行容量测试并修正实现直到通过**

```powershell
py -3.13 -m pytest try/tests/test_storage.py -q
```

Expected: 新增测试和既有存储测试全部通过。

- [ ] **Step 5: 提交配额核心**

```powershell
git add src/auto_fishing/storage/quota.py src/auto_fishing/storage/__init__.py try/tests/test_storage.py
git commit -m "feat: add strict data directory quota"
```

---

### Task 2: 测试先行接入设置、诊断和异步运行日志

**Files:**
- Modify: `src/auto_fishing/storage/settings.py`
- Modify: `src/auto_fishing/storage/diagnostics.py`
- Modify: `src/auto_fishing/storage/runtime_logging.py`
- Modify: `try/tests/test_storage.py`

**Interfaces:**
- Consumes: `StorageQuotaManager.register_write(path, previous_size, active_run=..., active_events=...)`。
- Produces: 三个存储类可选 `quota: StorageQuotaManager | None` 注入，未注入时保持现有测试和调用兼容。

- [ ] **Step 1: 写入三个存储写入者的失败测试**

```python
def test_settings_reports_replacement_to_shared_quota(tmp_path):
    root = tmp_path / "data"
    quota = StorageQuotaManager(root, max_bytes=100)
    quota.initialize()
    store = SettingsStore(root / "config.json", quota=quota)

    store.save(AppSettings(target_count=9))

    assert quota.total_bytes == (root / "config.json").stat().st_size


def test_diagnostics_write_enforces_entire_directory_quota(tmp_path):
    root = tmp_path / "data"
    write_sized(root / "runs" / "run-old" / "events.jsonl", 80, 1)
    quota = StorageQuotaManager(root, max_bytes=90)
    quota.initialize()
    store = DiagnosticsStore(root / "diagnostics", quota=quota)

    store.save(np.zeros((20, 20, 3), dtype=np.uint8), "E_TEST", "quota")

    assert not (root / "runs" / "run-old").exists()
    assert quota.total_bytes <= 90


def test_runtime_writer_prunes_oldest_active_frame_when_quota_is_full(tmp_path):
    root = tmp_path / "data"
    quota = StorageQuotaManager(root, max_bytes=2500)
    quota.initialize()
    store = RuntimeLogStore(root / "runs", queue_size=10, quota=quota)
    run_dir = store.start()
    for index in range(8):
        store.record_frame(
            np.full((120, 160, 3), index, dtype=np.uint8),
            observation=SceneObservation(),
            state_before=FishingState.READY,
            snapshot=RuntimeSnapshot(FishingState.READY, 0, 1, 30.0),
            frame_timestamp=float(index),
            now_monotonic=float(index),
        )
    store.close()

    frames = sorted((run_dir / "frames").glob("*.jpg"))
    assert frames
    assert frames[-1].name == "00000008.jpg"
    assert frames[0].name != "00000001.jpg"
    assert quota.total_bytes <= 2500
```

- [ ] **Step 2: 运行测试并确认失败原因是构造函数不接受 quota**

```powershell
py -3.13 -m pytest try/tests/test_storage.py -q
```

Expected: 三项新测试以 `unexpected keyword argument 'quota'` 失败。

- [ ] **Step 3: 接入 SettingsStore 和 DiagnosticsStore**

`SettingsStore.__init__` 保存 `self.quota`；`save()` 在替换前记录旧大小，替换后登记：

```python
previous_size = self.path.stat().st_size if self.path.is_file() else 0
temp.replace(self.path)
if self.quota is not None:
    self.quota.register_write(self.path, previous_size)
```

`DiagnosticsStore.__init__` 同样接受可选 `quota`；`save()` 在创建三个可能文件前保存各自旧大小，全部写入后逐个登记：

```python
for path in written_paths:
    if self.quota is not None:
        self.quota.register_write(path, previous_sizes[path])
```

- [ ] **Step 4: 接入 RuntimeLogStore 异步写线程**

构造函数增加 `quota` 并保存公开属性 `self.quota`。`start()` 创建事件文件后用零增量登记活动上下文：

```python
if self.quota is not None:
    self.quota.register_write(
        events_path,
        0,
        active_run=run_dir,
        active_events=events_path,
    )
```

帧写入后立即登记：

```python
image_path.write_bytes(encoded[1].tobytes())
if self.quota is not None:
    self.quota.register_write(
        image_path,
        0,
        active_run=self._run_dir,
        active_events=self._events_path,
    )
```

事件追加前后登记大小变化：

```python
previous_size = self._events_path.stat().st_size
with self._events_path.open("a", encoding="utf-8", newline="\n") as output:
    output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    output.write("\n")
    output.flush()
if self.quota is not None:
    self.quota.register_write(
        self._events_path,
        previous_size,
        active_run=self._run_dir,
        active_events=self._events_path,
    )
```

配额异常继续由现有 `_write_loop` 捕获并通过 `RuntimeLogError` 暴露，无需新增第二套错误通道。

- [ ] **Step 5: 运行存储和引擎相关测试**

```powershell
py -3.13 -m pytest try/tests/test_storage.py try/tests/test_engine.py -q
```

Expected: 全部通过，现有队列满与运行日志失败暂停路径不回退。

- [ ] **Step 6: 提交存储集成**

```powershell
git add src/auto_fishing/storage/settings.py src/auto_fishing/storage/diagnostics.py src/auto_fishing/storage/runtime_logging.py try/tests/test_storage.py
git commit -m "feat: enforce quota for all stored data"
```

---

### Task 3: 测试先行切换固定 D 盘路径与共享实例

**Files:**
- Modify: `src/auto_fishing/app.py`
- Modify: `try/tests/test_ui_smoke.py`

**Interfaces:**
- Consumes: `StorageQuotaManager` 和三个支持 quota 的存储类。
- Produces: `DEFAULT_DATA_DIR = Path(r"D:\29551\异环自动钓鱼数据")`；应用装配共享同一个配额实例。

- [ ] **Step 1: 写入默认路径和共享配额失败测试**

```python
from auto_fishing.app import DEFAULT_DATA_DIR


def test_default_data_directory_is_on_d_drive() -> None:
    assert DEFAULT_DATA_DIR == Path(r"D:\29551\异环自动钓鱼数据")


def test_application_build_services_shares_storage_quota(tmp_path) -> None:
    services = Application._build_services(tmp_path)

    assert services.runtime_log.quota is services.diagnostics.quota
    assert services.runtime_log.quota is services.settings.quota
    assert services.runtime_log.quota.root == tmp_path.resolve()
```

- [ ] **Step 2: 运行测试并确认 DEFAULT_DATA_DIR 缺失或 quota 未共享**

```powershell
py -3.13 -m pytest try/tests/test_ui_smoke.py -q -k "default_data_directory or shares_storage_quota"
```

Expected: 新测试失败，原因为常量不存在或存储类没有共享 quota。

- [ ] **Step 3: 修改应用装配**

在 `src/auto_fishing/app.py` 模块顶部定义：

```python
DEFAULT_DATA_DIR = Path(r"D:\29551\异环自动钓鱼数据")
```

默认路径改为：

```python
data_dir = self._data_dir or DEFAULT_DATA_DIR
```

`_build_services()` 中先创建并初始化共享配额：

```python
from auto_fishing.storage.quota import StorageQuotaManager

quota = StorageQuotaManager(data_dir)
quota.initialize()
runtime_log = RuntimeLogStore(data_dir / "runs", quota=quota)
diagnostics = DiagnosticsStore(data_dir / "diagnostics", quota=quota)
settings = SettingsStore(data_dir / "config.json", quota=quota)
```

返回服务时使用 `settings=settings`。保留 `Application(data_dir=tmp_path)` 测试覆盖能力，生产默认值不再读取 `LOCALAPPDATA`。

- [ ] **Step 4: 运行界面、存储和应用装配测试**

```powershell
py -3.13 -m pytest try/tests/test_ui_smoke.py try/tests/test_storage.py -q
```

Expected: 全部通过。

- [ ] **Step 5: 提交 D 盘装配**

```powershell
git add src/auto_fishing/app.py try/tests/test_ui_smoke.py
git commit -m "feat: store application data on D drive"
```

---

### Task 4: 全量回归与长期文档

**Files:**
- Modify: `AGENTS.md`
- Modify: `doc/验收标准.md`
- Modify: `doc/进展记录/2026-7-14.md`

**Interfaces:**
- Consumes: 已通过的配额和 D 盘装配实现。
- Produces: 最新长期规则、可验证验收项、高风险迁移与回退记录。

- [ ] **Step 1: 运行全量验证**

```powershell
py -3.13 -m pytest try/tests -q
py -3.13 -m compileall -q src
git diff --check
```

Expected: 全部测试通过，编译和空白检查退出码为 0。

- [ ] **Step 2: 更新长期规则和验收项**

明确记录：

- 数据根目录为 `D:\29551\异环自动钓鱼数据\`，C 盘路径废弃。
- 整个目录硬上限 104,857,600 字节和固定删除顺序。
- 配置、最近事件、运行日志写入失败安全暂停边界。
- 自动测试命令与迁移后的实际字节数。
- 新增技术债检查：没有第二套日志系统、环境变量或路径设置界面。

- [ ] **Step 3: 在进展记录中标记高风险迁移和回退**

记录旧 C 盘路径、当前 426.58 MiB、备份目录命名、全量 SHA256 校验、旧 EXE 备份、C 盘删除前置条件和恢复步骤。

- [ ] **Step 4: 提交实现文档**

```powershell
git add AGENTS.md doc
git commit -m "docs: document D drive storage quota"
```

---

### Task 5: 全量备份、迁移、配额裁剪与发布物构建

**Files:**
- Create outside workspace: `D:\0文件夹\备份\异环自动钓鱼-data-migration-YYYYMMDD-HHmm\`
- Create outside workspace: `D:\29551\异环自动钓鱼数据\`
- Build ignored artifact: `dist/异环自动钓鱼.exe`

**Interfaces:**
- Consumes: 旧 C 盘数据、根目录验收 EXE、新配额实现。
- Produces: 完整备份、≤100 MiB 的 D 盘生产数据、新单文件发布物。

- [ ] **Step 1: 确认程序未运行、磁盘空间充足和同类备份数量**

```powershell
$old = [IO.Path]::GetFullPath("$env:LOCALAPPDATA\异环自动钓鱼")
$new = [IO.Path]::GetFullPath('D:\29551\异环自动钓鱼数据')
$backupRoot = [IO.Path]::GetFullPath('D:\0文件夹\备份')
$running = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -and $_.ExecutablePath.EndsWith('异环自动钓鱼.exe')
})
if ($running) { throw '异环自动钓鱼仍在运行' }
if (-not (Test-Path -LiteralPath $old)) { throw '旧数据目录不存在' }
if (Test-Path -LiteralPath $new) { throw 'D 盘目标已存在，停止避免覆盖' }
$required = (Get-ChildItem -LiteralPath $old -File -Recurse | Measure-Object Length -Sum).Sum * 2
$free = (Get-PSDrive D).Free
if ($free -lt $required) { throw 'D 盘空间不足以同时保存备份和迁移副本' }
Get-ChildItem -LiteralPath $backupRoot -Directory -Filter '异环自动钓鱼-data-migration-*' |
    Sort-Object LastWriteTime -Descending | Select-Object FullName,LastWriteTime
```

Expected: 无运行进程、目标不存在、空间足够；同类备份最终只保留最新两份。

- [ ] **Step 2: 生成源清单、备份数据和旧 EXE**

在同一 PowerShell 会话中定义全量清单函数：

```powershell
function Get-DataManifest([string]$root) {
    $resolved = [IO.Path]::GetFullPath($root)
    @(
        Get-ChildItem -LiteralPath $resolved -File -Recurse -Force |
        Sort-Object FullName |
        ForEach-Object {
            [pscustomobject]@{
                RelativePath = [IO.Path]::GetRelativePath($resolved, $_.FullName)
                Length = $_.Length
                SHA256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash
            }
        }
    )
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$backup = [IO.Path]::GetFullPath((Join-Path $backupRoot "异环自动钓鱼-data-migration-$stamp"))
$backupData = Join-Path $backup 'data'
New-Item -ItemType Directory -Path $backupData | Out-Null
$sourceManifest = Get-DataManifest $old
Get-ChildItem -LiteralPath $old -Force |
    Copy-Item -Destination $backupData -Recurse -Force
Copy-Item -LiteralPath 'D:\1Folder\异环自动钓鱼\异环自动钓鱼.exe' `
    -Destination (Join-Path $backup 'pre-migration.exe')
$sourceManifest | ConvertTo-Json -Depth 3 |
    Set-Content -LiteralPath (Join-Path $backup 'source-manifest.json') -Encoding utf8
```

- [ ] **Step 3: 全量核对备份并复制到 D 盘目标**

```powershell
$backupManifest = Get-DataManifest $backupData
$backupDiff = Compare-Object $sourceManifest $backupManifest `
    -Property RelativePath,Length,SHA256
if ($backupDiff) { throw 'C→备份全量清单不一致' }
New-Item -ItemType Directory -Path $new | Out-Null
Get-ChildItem -LiteralPath $old -Force |
    Copy-Item -Destination $new -Recurse -Force
$targetManifest = Get-DataManifest $new
$targetDiff = Compare-Object $sourceManifest $targetManifest `
    -Property RelativePath,Length,SHA256
if ($targetDiff) { throw 'C→D 全量清单不一致' }
$sameType = @(
    Get-ChildItem -LiteralPath $backupRoot -Directory `
        -Filter '异环自动钓鱼-data-migration-*' |
    Sort-Object LastWriteTime -Descending
)
if ($sameType.Count -gt 2) {
    foreach ($older in ($sameType | Select-Object -Skip 2)) {
        $resolved = [IO.Path]::GetFullPath($older.FullName)
        if (-not $resolved.StartsWith($backupRoot + [IO.Path]::DirectorySeparatorChar)) {
            throw '旧备份目录越界'
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
```

Expected: 两次 `Compare-Object` 均为空。

- [ ] **Step 4: 用生产配额实现裁剪 D 盘目标并验证配置**

```powershell
$env:PYTHONPATH=(Resolve-Path src).Path
@'
from pathlib import Path
from auto_fishing.storage.quota import StorageQuotaManager

root = Path(r"D:\29551\异环自动钓鱼数据")
quota = StorageQuotaManager(root)
quota.initialize()
assert quota.total_bytes <= 100 * 1024 * 1024
assert (root / "config.json").is_file()
print("MIGRATION_QUOTA_OK", quota.total_bytes)
'@ | py -3.13 -
```

Expected: 输出 `MIGRATION_QUOTA_OK`，字节数不超过 104,857,600。

- [ ] **Step 5: 在 D 盘纯英文临时目录构建并烟雾验证**

```powershell
$tempRoot='D:\29551\auto-fishing-build-temp'
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
$env:PYINSTALLER_CONFIG_DIR=$tempRoot
$env:TEMP=$tempRoot
$env:TMP=$tempRoot
& .\scripts\build.ps1 -PythonPath 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe'
& .\try\smoke_exe.ps1
```

Expected: 构建门全量测试通过、`RELEASE_MANIFEST_OK` 和 `SMOKE_OK`。

- [ ] **Step 6: 提交迁移证据文档**

更新 `doc/进展记录/2026-7-14.md` 和 `doc/验收标准.md`，写入备份路径、清单文件数、迁移前后字节数、配额裁剪后字节数、发布物大小与 SHA256、构建和烟雾结果。

```powershell
git add doc
git commit -m "docs: record D drive data migration"
```

---

### Task 6: 合并 main、替换根目录 EXE、删除旧 C 数据并推送

**Files:**
- Replace ignored artifact: `D:\1Folder\异环自动钓鱼\异环自动钓鱼.exe`
- Delete migrated source: `C:\Users\29551\AppData\Local\异环自动钓鱼\`
- Modify: `doc/进展记录/2026-7-14.md`

**Interfaces:**
- Consumes: 已验证功能分支、工作树 `dist` 发布物、已裁剪 D 盘数据和完整备份。
- Produces: 干净且已推送的 `main`、新根目录 EXE、无旧 C 盘生产数据。

- [ ] **Step 1: 在主工作区同步并合并功能分支**

```powershell
Set-Location 'D:\1Folder\异环自动钓鱼'
git switch main
git pull --ff-only
git merge --no-ff codex/feat-d-drive-storage-cap -m "merge: D drive storage quota"
py -3.13 -m pytest try/tests -q
```

Expected: 无冲突，合并后全量测试通过。

- [ ] **Step 2: 原子替换根目录 EXE 并核对哈希**

```powershell
$source='D:\1Folder\异环自动钓鱼\.worktrees\codex-feat-d-drive-storage-cap\dist\异环自动钓鱼.exe'
$target='D:\1Folder\异环自动钓鱼\异环自动钓鱼.exe'
$staged="$target.new"
Copy-Item -LiteralPath $source -Destination $staged -Force
if ((Get-FileHash $source).Hash -ne (Get-FileHash $staged).Hash) {
    Remove-Item -LiteralPath $staged -Force
    throw '新发布物副本哈希不一致'
}
Move-Item -LiteralPath $staged -Destination $target -Force
py -3.13 scripts/verify_release.py $target
```

Expected: 根目录 EXE 与工作树发布物 SHA256 一致，最终清单校验通过。

- [ ] **Step 3: 精确删除已迁移旧 C 盘目录**

```powershell
$expected=[IO.Path]::GetFullPath('C:\Users\29551\AppData\Local\异环自动钓鱼')
$actual=[IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA '异环自动钓鱼'))
if ($actual -ne $expected) { throw '旧数据目录解析结果异常' }
if (-not (Test-Path -LiteralPath 'D:\29551\异环自动钓鱼数据\config.json')) {
    throw 'D 盘配置不存在，拒绝删除 C 盘数据'
}
$dBytes=(Get-ChildItem -LiteralPath 'D:\29551\异环自动钓鱼数据' -File -Recurse |
    Measure-Object Length -Sum).Sum
if ($dBytes -gt 104857600) { throw 'D 盘数据仍超过 100 MiB' }
Remove-Item -LiteralPath $actual -Recurse -Force
if (Test-Path -LiteralPath $actual) { throw '旧 C 盘数据删除失败' }
```

- [ ] **Step 4: 更新并提交最终迁移状态**

记录 C 盘旧目录已删除、D 盘最终字节数、根目录 EXE 哈希、合并测试和回退备份地址。

```powershell
git add AGENTS.md doc
git commit -m "docs: finalize D drive storage migration"
```

- [ ] **Step 5: 完成前最终验证**

```powershell
py -3.13 -m pytest try/tests -q
py -3.13 -m compileall -q src
git diff --check
py -3.13 scripts/verify_release.py '.\异环自动钓鱼.exe'
git status --short --branch
```

Expected: 全部测试通过，编译、差异和发布清单通过，工作区干净。

- [ ] **Step 6: 清理工作树和功能分支**

从主工作区验证目标严格位于 `.worktrees` 后执行：

```powershell
$main=[IO.Path]::GetFullPath('D:\1Folder\异环自动钓鱼')
$container=[IO.Path]::GetFullPath((Join-Path $main '.worktrees'))
$worktree=[IO.Path]::GetFullPath((Join-Path $container 'codex-feat-d-drive-storage-cap'))
if (-not $worktree.StartsWith($container + [IO.Path]::DirectorySeparatorChar)) {
    throw '工作树目标越界'
}
if (git -C $worktree status --porcelain) { throw '功能工作树不干净' }
git worktree remove -- $worktree
git worktree prune
git branch -d codex/feat-d-drive-storage-cap
```

- [ ] **Step 7: 检查 GitHub CLI 身份并推送 main**

```powershell
gh --version
gh auth status
git status --short --branch
git push origin main
git status --short --branch
```

Expected: GitHub CLI 已认证，`main` 推送成功并与 `origin/main` 同步。用户明确要求直接推送主分支，因此不创建 PR。

- [ ] **Step 8: 清理外部构建临时目录并终审数据位置**

```powershell
$temp=[IO.Path]::GetFullPath('D:\29551\auto-fishing-build-temp')
if (-not $temp.StartsWith('D:\29551\')) { throw '临时目录越界' }
if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force }
$data='D:\29551\异环自动钓鱼数据'
$bytes=(Get-ChildItem -LiteralPath $data -File -Recurse | Measure-Object Length -Sum).Sum
[pscustomobject]@{
    DataRoot=$data
    Bytes=$bytes
    WithinLimit=($bytes -le 104857600)
    OldCPathExists=(Test-Path -LiteralPath 'C:\Users\29551\AppData\Local\异环自动钓鱼')
    RootExeHash=(Get-FileHash 'D:\1Folder\异环自动钓鱼\异环自动钓鱼.exe').Hash
} | Format-List
```

Expected: `WithinLimit=True`、`OldCPathExists=False`，根目录 EXE 哈希与发布记录一致。
