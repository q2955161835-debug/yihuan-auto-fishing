# 运行日志增量清理实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在整个数据目录继续严格受 100 MiB 上限约束的同时，避免一次轻微超限触发整份历史运行目录递归删除，从而防止日志队列被清理工作堵满并暂停钓鱼主循环。

**Architecture:** 保留单一 `StorageQuotaManager` 容量账本和现有清理优先级，但把“旧运行”从整目录删除改为按最旧截图逐项释放空间；只有旧运行不再包含截图时才处理其事件文件和空目录。运行日志仍由后台写入线程调用配额管理器，实际写盘、编码或无法恢复容量等错误继续走 `E_LOGGING` 安全暂停，避免掩盖真实数据损坏。

**Tech Stack:** Python 3.13、pathlib、pytest 9.1、PyInstaller 6.19、PowerShell。

## Global Constraints

- 整个 `D:\29551\异环自动钓鱼数据\` 必须始终不超过 104,857,600 字节。
- `config.json`、未知普通文件和活动运行最新完整事件不得被自动删除。
- 清理工作不得长时间阻塞后台写入线程并间接暂停钓鱼主循环。
- 先写失败测试并确认失败，再修改生产代码。
- 修改完成后必须更新 `AGENTS.md`、`doc/验收标准.md` 和 `doc/进展记录/2026-7-14.md`。

---

### Task 1: 复现轻微超限触发整目录删除

**Files:**
- Modify: `try/tests/test_storage.py`

**Interfaces:**
- Consumes: `StorageQuotaManager.initialize()`、`StorageQuotaManager.register_write()`。
- Produces: 回归测试 `test_quota_incrementally_prunes_completed_run_frames_during_active_write`。

- [x] **Step 1: Write the failing test**

```python
def test_quota_incrementally_prunes_completed_run_frames_during_active_write(tmp_path):
    root = tmp_path / "data"
    old_run = root / "runs" / "run-old"
    old_events = old_run / "events.jsonl"
    write_sized(old_events, 10, 1)
    for index in range(20):
        write_sized(old_run / "frames" / f"{index:08d}.jpg", 10, 2 + index)
    quota = StorageQuotaManager(root, max_bytes=220)
    quota.initialize()
    active_events = root / "runs" / "run-new" / "events.jsonl"
    write_sized(active_events, 15, 30)
    quota.register_write(
        active_events,
        0,
        active_run=active_events.parent,
        active_events=active_events,
    )

    assert old_run.is_dir()
    assert len(list((old_run / "frames").glob("*.jpg"))) == 19
    assert old_events.is_file()
    assert quota.total_bytes <= 220
```

- [x] **Step 2: Run test to verify it fails**

Run: `py -3.13 -m pytest try/tests/test_storage.py::test_quota_incrementally_prunes_completed_run_frames_during_active_write -q`

Expected: FAIL because the current implementation calls `shutil.rmtree()` and removes the entire `run-old` directory.

- [x] **Step 3: Commit the red test**

```powershell
git add try/tests/test_storage.py
git commit -m "test: reproduce blocking full-run quota cleanup"
```

---

### Task 2: 按需增量清理旧运行

**Files:**
- Modify: `src/auto_fishing/storage/quota.py`
- Modify: `try/tests/test_storage.py`

**Interfaces:**
- Consumes: `_completed_runs(active_run)` 返回按时间升序排列的旧运行。
- Produces: `_prune_completed_run(run: Path, total: int) -> int`，只删除恢复上限所需的最旧截图；截图耗尽后才删除旧事件文件和空目录。

- [x] **Step 1: Implement the minimum incremental cleanup**

```python
def _prune_completed_run(self, run: Path, total: int) -> int:
    frames = run / "frames"
    if frames.is_dir():
        for path in sorted(frames.glob("*.jpg")):
            resolved = self._inside(path)
            size = resolved.stat().st_size
            resolved.unlink()
            total -= size
            if total <= self.max_bytes:
                return total
    if total > self.max_bytes:
        events = run / "events.jsonl"
        if events.is_file():
            size = events.stat().st_size
            events.unlink()
            total -= size
    self._remove_empty_run(run)
    return total
```

`_enforce()` 对每个旧运行调用该方法并在容量恢复后立即返回；不得再对含多张截图的旧运行直接执行 `shutil.rmtree()`。

- [x] **Step 2: Run the focused storage tests**

Run: `py -3.13 -m pytest try/tests/test_storage.py -q`

Expected: PASS，且旧清理优先级、诊断组原子清理、活动事件保留和未知文件保护测试继续通过。

- [x] **Step 3: Add a bounded-work assertion**

扩充回归测试，记录删除前后旧截图数量，明确仅为 5 字节超限删除一张 10 字节截图，不得删除剩余 19 张。

- [x] **Step 4: Commit the implementation**

```powershell
git add src/auto_fishing/storage/quota.py try/tests/test_storage.py
git commit -m "fix: prune historical log frames incrementally"
```

---

### Task 3: 更新长期规则与验收证据

**Files:**
- Modify: `AGENTS.md`
- Modify: `doc/验收标准.md`
- Modify: `doc/进展记录/2026-7-14.md`

**Interfaces:**
- Consumes: 最新真实运行 `run-20260714T081942331895Z` 的时间线和修复后自动测试结果。
- Produces: 可追溯的问题—原因—解决方案、自动验收命令和实机待确认项。

- [x] **Step 1: Update project rules**

记录配额轻微超限必须增量删除旧截图，禁止在活动写入路径整目录递归删除含大量截图的旧运行。

- [x] **Step 2: Update acceptance criteria**

新增：构造 20 张旧截图、仅超限 5 字节时，必须只删除一张截图、容量恢复且旧运行仍存在；真实游戏需确认不再出现因清理引发的 `日志队列已满`。

- [x] **Step 3: Update progress record**

记录真实错误时间段、31.58 秒阻塞证据、数据目录从约 100 MiB 降到约 6.25 MB 的异常清理结果、修复文件和回退提交。

- [x] **Step 4: Commit documentation**

```powershell
git add AGENTS.md doc/验收标准.md doc/进展记录/2026-7-14.md docs/superpowers/plans/2026-07-14-incremental-log-pruning.md
git commit -m "docs: record incremental log pruning acceptance"
```

---

### Task 4: 全量验证、发布和集成

**Files:**
- Replace after successful build: `异环自动钓鱼.exe`

**Interfaces:**
- Consumes: `scripts/build.ps1`、`scripts/verify_release.py`、`try/smoke_exe.ps1`。
- Produces: 通过管理员清单校验和烟雾测试的新根目录单文件发布物。

- [x] **Step 1: Run all tests**

Run: `py -3.13 -m pytest try/tests -q`

Expected: 全部 PASS。

- [x] **Step 2: Build and verify release**

Run: `powershell -ExecutionPolicy Bypass -File scripts/build.ps1`

Expected: 测试门通过并生成 `dist/异环自动钓鱼.exe`；最终内嵌清单为 `requireAdministrator`、`uiAccess=false`。

- [x] **Step 3: Run smoke test**

Run: `& .\try\smoke_exe.ps1`

Expected: `SMOKE_OK`。

- [x] **Step 4: Replace root release and verify SHA256**

将 `dist/异环自动钓鱼.exe` 原子替换到项目根目录，核对两者 SHA256 完全一致。

- [ ] **Step 5: Merge and publish**

确认分支干净后合并到 `main`，再次运行全量测试，删除任务分支并推送 `origin/main`；最后核对本地与远端提交一致。

## Self-Review

- 规格覆盖：严格总容量、主循环不受整目录清理影响、保留关键文件、真实写盘错误仍暂停、文档与发布验收均有对应任务。
- 占位检查：无 TBD、TODO 或未定义的后续工作。
- 类型一致性：所有路径均为 `Path`，容量与返回值均为 `int`，沿用现有 `StorageQuotaManager` 接口。
