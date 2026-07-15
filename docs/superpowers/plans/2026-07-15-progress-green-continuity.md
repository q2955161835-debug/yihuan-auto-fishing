# 进度条绿色连续段识别修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让识别器在真实绿区移动到左侧时持续选择长而高饱和的青绿色连续段，不再把低饱和蓝灰轨道、树叶或建筑背景当作绿区。

**Architecture:** 保留现有固定顶部槽、五扫描线、横向连续段、三线共识、短绿区最小宽度和历史跟踪结构。宽松绿色掩码只负责恢复黄标切断处的外边界；饱和度不低于 155 的实色连续段负责证明候选确为绿区，独立候选直接来自实色段。回归测试用最新诊断包第 1117 帧量化出的颜色和几何构造最小复现：真实绿区仅在三条核心扫描线上连续，中央一条会被低饱和背景粘连，另有三线一致的短背景假候选。

**Tech Stack:** Python 3.13、NumPy、OpenCV、pytest、PyInstaller。

## Global Constraints

- 不改变 `_MINIMUM_GREEN_WIDTH_RATIO = 0.012`，短绿区能力必须保留。
- 不增加黄标与绿区相邻、重叠或固定距离约束。
- 不改变最近 15 帧、`0.2^帧龄` 加权控制和 A/D 方向语义。
- 真实诊断包保持只读；临时解包和分析产物只放在 `try/output/`。
- 修改后必须通过定向测试、全量测试、真实诊断帧回放、412 帧历史控制回放、编译和单文件构建。

---

### Task 1: 锁定左移绿区被背景抢占的回归

**Files:**
- Modify: `try/tests/test_progress.py`

**Interfaces:**
- Consumes: `ProgressRecognizer.analyze(image: np.ndarray, timestamp: float) -> ProgressScanResult`
- Produces: 一项在当前实现上失败、在修复后通过的真实几何回归测试。

- [x] **Step 1: 写入失败测试**

```python
def test_prefers_long_saturated_green_run_over_blue_background_fragments() -> None:
    image = np.zeros((216, 1332, 3), dtype=np.uint8)
    progress_green = tuple(
        int(value)
        for value in cv2.cvtColor(
            np.uint8([[[83, 192, 186]]]),
            cv2.COLOR_HSV2BGR,
        )[0, 0]
    )
    blue_background = tuple(
        int(value)
        for value in cv2.cvtColor(
            np.uint8([[[102, 143, 107]]]),
            cv2.COLOR_HSV2BGR,
        )[0, 0]
    )
    cv2.rectangle(image, (264, 93), (454, 106), progress_green, -1)
    cv2.line(image, (455, 99), (775, 99), blue_background, 1)
    cv2.rectangle(image, (800, 93), (840, 106), blue_background, -1)
    cv2.rectangle(image, (1129, 86), (1138, 112), YELLOW_BGR, -1)

    result = ProgressRecognizer().analyze(image, 1.0)

    assert result.observation is not None
    assert result.observation.green_left == pytest.approx(264 / 1332, abs=0.01)
    assert result.observation.green_right == pytest.approx(455 / 1332, abs=0.01)
    assert result.observation.yellow_x == pytest.approx(1133.5 / 1332, abs=0.01)
```

- [x] **Step 2: 验证测试因错误候选而失败**

Run: `py -3.13 -m pytest try/tests/test_progress.py::test_prefers_long_saturated_green_run_over_blue_background_fragments -q`

Expected: FAIL，当前实现返回约 `green_left=800/1332`，证明测试命中了背景候选抢占，而不是测试装配错误。

### Task 2: 用实色连续段约束宽松边界候选

**Files:**
- Modify: `src/auto_fishing/vision/progress.py`
- Test: `try/tests/test_progress.py`

**Interfaces:**
- Consumes: OpenCV HSV 绿色掩码和现有 `_runs`、`_consensus`。
- Produces: 宽松掩码下界 `H=70、S=80、V=100`、实色掩码下界 `H=70、S=155、V=100`，以及“宽松候选必须累计获得现有最小宽度的实色支撑”规则；其余控制接口不变。

- [x] **Step 1: 实施最小生产修改**

```python
green_mask = cv2.inRange(
    hsv,
    np.array((70, 80, 100), dtype=np.uint8),
    np.array((105, 255, 255), dtype=np.uint8),
)
solid_green_mask = cv2.inRange(
    hsv,
    np.array((70, 155, 100), dtype=np.uint8),
    np.array((105, 255, 255), dtype=np.uint8),
)
```

- [x] **Step 2: 验证定向回归与既有短绿区**

Run: `py -3.13 -m pytest try/tests/test_progress.py -q`

Expected: PASS；真实分段夹具、高品质窄绿区、跨分辨率 9% 绿区和 1.2% 短绿区回归均保持通过。

- [x] **Step 3: 用最新诊断帧做只读回放**

Run: `py -3.13 <只读回放脚本>`

Expected: 第 579～1117 帧的 155 张 10 FPS 无损采样持续输出真实长绿区；第 1117 帧约为 `green=264～454、yellow=1133.5`，第 1121 帧以后的 21 张采样不再输出背景假绿区。

### Task 3: 版本、验收、构建与交付

**Files:**
- Modify: `src/auto_fishing/product.py`
- Modify: `try/tests/test_ui_smoke.py`
- Modify: `AGENTS.md`
- Modify: `doc/验收标准.md`
- Modify: `doc/进展记录/2026-7-15.md`
- Generated and ignored: `dist/异环自动钓鱼V2.exe`
- Replaced and ignored: `异环自动钓鱼V2.exe`

**Interfaces:**
- Consumes: `V2_VERSION`、V2 构建脚本和发布物清单验证器。
- Produces: 本地候选 `v2.0.3` 及可回退的根目录单文件 EXE。

- [x] **Step 1: 更新版本测试并确认失败**

```python
assert profile.version == "2.0.3"
```

Run: `py -3.13 -m pytest try/tests/test_ui_smoke.py::test_v2_profile_uses_local_app_data_and_explicit_version -q`

Expected: FAIL，实际版本仍为 `2.0.2`。

- [x] **Step 2: 更新生产版本标识**

```python
V2_VERSION = "2.0.3"
```

- [x] **Step 3: 运行源码级完整验证**

Run: `py -3.13 -m pytest try/tests -q`

Run: `py -3.13 -m compileall -q src try/tests scripts`

Run: `git diff --check`

Expected: 全部退出码为 0，无失败、语法错误或空白错误。

- [x] **Step 4: 更新长期规则、验收结果和当日进展记录**

记录最新包路径、帧号、HSV 量化、失败/通过命令、测试数量、真实回放结果、构建哈希、外部备份地址，以及真实游戏待确认项。

- [x] **Step 5: 备份并构建单文件候选**

先将当前根目录 `异环自动钓鱼V2.exe` 备份到 `D:\0文件夹\备份\异环自动钓鱼-v2.0.3-before-<时间>\` 并记录 SHA256；同类备份最多保留两份。随后运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_v2.ps1 -PythonPath C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe
```

Expected: 全量测试再次通过、PyInstaller 成功、清单输出 `requireAdministrator uiAccess=false dpi=PerMonitorV2 fallback=true/pm` 并给出 SHA256。

- [ ] **Step 6: 复核、替换并合并主线**

验证 `dist` 发布物后复制到根目录，比较两者大小、SHA256 和内嵌清单；若失败，从备份恢复。提交任务分支，合并回 `main`，运行最终定向测试并删除任务分支；未提升的 PowerShell 不冒用旧烟雾证据。

