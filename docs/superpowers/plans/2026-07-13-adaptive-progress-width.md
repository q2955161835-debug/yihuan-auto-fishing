# 自适应进度绿区宽度实施计划

> **供自动化执行者：** 必须使用 `superpowers:executing-plans`（执行计划）逐项实施；所有行为修改必须遵循 `superpowers:test-driven-development`（测试驱动开发），步骤使用复选框跟踪。

**目标：** 让固定槽位扫描线识别器可靠识别高品质鱼约占顶部裁剪宽度 8%～11% 的窄绿区，同时保留宽绿区识别与噪声拒绝能力。

**架构：** 保持现有五扫描线、三行共识、黄色标记配对和分段重建流程，只将每个黄色候选的宽度门槛改为 `max(image_width * 0.02, yellow_width * 4)`。门槛随 `_LineCandidate`（单行候选）进入多行共识，最终校验和置信度复用同一门槛，避免候选阶段与最终阶段规则分叉。

**技术栈：** Python（派森）3.13、NumPy（数值数组库）、OpenCV（计算机视觉库）、pytest（测试框架）、PyInstaller（打包工具）。

## 全局约束

- 不修改中心控制方向、稳定结束切换、F 输入和自动化状态机。
- 保持五条扫描线、至少三条共识、左右边界 2% 容差和时间连续性校验。
- 测试夹具只放在 `try/fixtures/progress/`，且不得包含角色、UID 或屏幕键盘区域。
- 真实单轮未完成前不得合并 `main`（主分支）。
- 发布前备份当前可执行文件到 `D:\0文件夹\备份\` 的带时间子目录。

---

### 任务 1：建立真实窄绿区回归夹具

**文件：**
- 创建：`try/fixtures/progress/progress_narrow_high_quality.png`
- 修改：`try/tests/test_progress.py`

**接口：**
- 使用：`ProgressRecognizer.detect(image: np.ndarray, timestamp: float) -> ProgressObservation | None`
- 产出：可重复证明 9.8% 窄绿区当前被固定 12% 门槛拒绝的真实回归测试。

- [ ] **步骤 1：从原始诊断图裁出顶部识别区域**

运行：

```powershell
& 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe' try\tools\extract_progress_fixture.py "$env:LOCALAPPDATA\异环自动钓鱼\diagnostics\20260712T111434429873Z_E_TIMEOUT.png" 'try\fixtures\progress\progress_narrow_high_quality.png'
```

预期：生成宽 1332 像素、高 216 像素的顶部裁剪图，不包含底部角色、UID 和屏幕键盘。

- [ ] **步骤 2：编写真实窄绿区失败测试**

在 `try/tests/test_progress.py` 增加：

```python
def test_real_high_quality_fixture_detects_narrow_green_interval() -> None:
    fixture = Path("try/fixtures/progress/progress_narrow_high_quality.png")
    image = cv2.imdecode(np.fromfile(fixture, dtype=np.uint8), cv2.IMREAD_COLOR)

    observation = ProgressRecognizer().detect(image, 1.0)

    assert observation is not None
    assert observation.green_left < observation.yellow_x < observation.green_right
    assert 0.08 <= observation.green_right - observation.green_left <= 0.11
```

- [ ] **步骤 3：运行测试并确认因固定门槛而失败**

运行：

```powershell
& 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe' -m pytest try/tests/test_progress.py::test_real_high_quality_fixture_detects_narrow_green_interval -q
```

预期：断言失败，`observation`（观测）为 `None`（空），证明测试命中现有 12% 固定门槛。

### 任务 2：覆盖自适应门槛的几何边界

**文件：**
- 修改：`try/tests/test_progress.py`

**接口：**
- 使用：现有 `frame(...) -> np.ndarray` 合成图生成器。
- 产出：窄分段绿区、边缘黄标、多分辨率和短噪声四类约束。

- [ ] **步骤 1：编写窄分段与边缘黄标参数化测试**

增加按 `image_width`（图像宽度）缩放的合成帧辅助函数，并在 300、600、1200 像素宽度下验证：绿区宽度约为画面 9%，黄标宽度约为画面 1%，黄标位于中央或靠近两端时，识别结果均非空且绿区包含黄标。

- [ ] **步骤 2：保留短噪声拒绝测试**

运行现有测试：

```powershell
& 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe' -m pytest try/tests/test_progress.py::test_rejects_green_region_that_is_too_narrow -q
```

预期：通过；26 像素绿区短于 7 像素黄标宽度的四倍，因此仍被拒绝。

- [ ] **步骤 3：确认新增合成测试在实现前失败**

运行：

```powershell
& 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe' -m pytest try/tests/test_progress.py -q
```

预期：新增窄绿区测试失败，现有宽绿区和短噪声测试通过。

### 任务 3：实现候选级自适应宽度

**文件：**
- 修改：`src/auto_fishing/vision/progress.py`
- 测试：`try/tests/test_progress.py`

**接口：**
- 修改：`_LineCandidate` 增加 `minimum_width: float`。
- 保持：`ProgressRecognizer.detect` 与 `ProgressRecognizer.analyze` 的公开签名不变。

- [ ] **步骤 1：在每个黄色区间计算唯一门槛**

在 `_line_candidates` 内对每个黄色区间计算：

```python
yellow_width = yellow_right - yellow_left
minimum_width = max(image_width * 0.02, yellow_width * 4)
```

左右绿色段合并候选与单一绿色区间候选都使用该 `minimum_width`，并把它写入 `_LineCandidate`。

- [ ] **步骤 2：让共识结果携带门槛中位数**

`_consensus` 构造结果时增加：

```python
minimum_width=float(np.median([item.minimum_width for item in selected]))
```

- [ ] **步骤 3：删除最终固定 12% 校验**

最终宽度校验改为：

```python
if green_width < candidate.minimum_width:
    return ProgressScanResult(..., rejection_reason="bar_too_narrow")
width_score = min(1.0, green_width / candidate.minimum_width)
```

不得保留 `image_width * 0.12` 的第二道门槛。

- [ ] **步骤 4：运行聚焦测试**

运行：

```powershell
& 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe' -m pytest try/tests/test_progress.py -q
```

预期：所有进度识别测试通过；真实窄绿区、现有宽绿区和短噪声拒绝同时成立。

- [ ] **步骤 5：提交识别修复**

```powershell
git add try/fixtures/progress/progress_narrow_high_quality.png try/tests/test_progress.py src/auto_fishing/vision/progress.py
git commit -m "fix: detect adaptive narrow progress zones"
```

### 任务 4：回归、文档与发布物

**文件：**
- 修改：`AGENTS.md`
- 修改：`doc/验收标准.md`
- 修改：`doc/进展记录/2026-7-13.md`
- 生成但不提交：`dist/异环自动钓鱼.exe`

**接口：**
- 使用：既有构建脚本和发布物启动方式。
- 产出：带自动测试证据、哈希与实机待验收项的新发布物。

- [ ] **步骤 1：运行相关场景、引擎和全量测试**

```powershell
& 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe' -m pytest try/tests -q
```

预期：全部测试通过，稳定结束切换测试无需修改。

- [ ] **步骤 2：更新长期规则、验收项和进展记录**

记录根因、公式、真实夹具路径、测试命令和结果；将真实高品质鱼从等待进度条进入控制、完成中心跟随、结束识别、结算关闭和计数标为人工确认，不提前写通过。

- [ ] **步骤 3：备份旧发布物并构建**

先复制当前 `dist/异环自动钓鱼.exe` 到 `D:\0文件夹\备份\异环自动钓鱼-adaptive-width-prebuild-<时间>\`，再运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build.ps1 -PythonPath 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe'
```

预期：测试、单文件构建和清单校验通过，输出新的 SHA256（安全散列值）。

- [ ] **步骤 4：提交文档并启动发布物**

```powershell
git add AGENTS.md doc/验收标准.md doc/进展记录/2026-7-13.md docs/superpowers/plans/2026-07-13-adaptive-progress-width.md
git commit -m "docs: record adaptive progress width release"
Start-Process -FilePath 'dist/异环自动钓鱼.exe'
```

预期：控制窗口启动；等待用户在真实高品质鱼流程中确认阶段切换与完整单轮。真实验收前不合并 `main`（主分支）。
