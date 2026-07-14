# 真实鱼获卡片确认与随机重试关闭实施计划

> **供自动化执行者：** 必须使用 `superpowers:executing-plans`（执行计划）逐项实施；所有行为修改遵循 `superpowers:test-driven-development`（测试驱动开发），步骤使用复选框跟踪。

**目标：** 排除紫色漩涡结算误判，在真实鱼获卡片出现后以受限随机延迟点击，并在卡片仍存在时最多重试三次。

**架构：** 场景识别器用中央卡片与顶部奖励栏联合确认真实结算；自动化核心用单调时钟安排首次点击和重试，不阻塞 30 帧循环。点击点按游戏客户区归一化坐标生成并排除屏幕键盘矩形，视觉消失和就绪画面而非 Windows 发送数决定成功。

**技术栈：** Python（派森）3.13、NumPy（数值数组库）、OpenCV（计算机视觉库）、pytest（测试框架）、PyInstaller（打包工具）。

## 全局约束

- 不修改进度控制、稳定结束切换、F/A/D 输入和完成计数语义。
- 首次延迟为 0.18～0.42 秒，重试延迟为 0.40～0.80 秒，总点击次数最多 3 次。
- 不使用真实睡眠阻塞工作线程，不新增机器学习或第二鼠标输入后端。
- 真实完整单轮未通过前不得合并 `main`（主分支）。

---

### 任务 1：真实卡片联合识别

**文件：**
- 创建：`try/fixtures/result/result_transition_vortex.jpg`
- 创建：`try/fixtures/result/result_catch_card.jpg`
- 修改：`src/auto_fishing/vision/regions.py`
- 修改：`src/auto_fishing/vision/scenes.py`
- 修改：`try/tests/test_scenes.py`

**接口：**
- 新增：`RESULT_HEADER_ROI = NormalizedRect(0.38, 0.04, 0.62, 0.13)`。
- 新增：`_magenta_ratio(image, valid=None) -> float`。
- 保持：`SceneRecognizer.observe(...) -> SceneObservation` 签名不变。

- [ ] **步骤 1：复制真实正反例夹具**

```powershell
New-Item -ItemType Directory -Force try\fixtures\result | Out-Null
Copy-Item "$env:LOCALAPPDATA\异环自动钓鱼\runs\run-20260713T055318102726Z\frames\00000442.jpg" try\fixtures\result\result_transition_vortex.jpg
Copy-Item "$env:LOCALAPPDATA\异环自动钓鱼\runs\run-20260713T055318102726Z\frames\00000600.jpg" try\fixtures\result\result_catch_card.jpg
```

- [ ] **步骤 2：写入失败测试**

在 `try/tests/test_scenes.py` 增加夹具读取辅助函数，并验证漩涡连续三帧始终 `result=False`，真实卡片前两帧为假、第三帧为真。更新 `result_frame`，在顶部奖励栏区域绘制暗色底、品红条和白色文字块，使现有合成结算测试表达新结构。

- [ ] **步骤 3：确认红灯**

```powershell
& 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe' -m pytest try/tests/test_scenes.py -q
```

预期：漩涡反例测试失败，因为旧识别器仍把它连续确认为结算。

- [ ] **步骤 4：实现最小联合识别**

在 `regions.py` 增加奖励栏区域；在 `scenes.py` 裁剪并应用 `_valid_mask`，实现：

```python
header_candidate = (
    _magenta_ratio(result_header, header_valid) > 0.08
    and _white_ratio(result_header, header_valid) > 0.008
    and 0.30 <= _dark_ratio(result_header, header_valid) <= 0.80
)
result_candidate = center_candidate and header_candidate
```

品红掩码使用 HSV `(135, 80, 80)`～`(175, 255, 255)`。

- [ ] **步骤 5：确认绿灯并提交**

```powershell
& 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe' -m pytest try/tests/test_scenes.py -q
git add src/auto_fishing/vision/regions.py src/auto_fishing/vision/scenes.py try/tests/test_scenes.py try/fixtures/result
git commit -m "fix: distinguish result card from transition"
```

预期：场景测试全部通过。

### 任务 2：随机延迟、避障与三次重试

**文件：**
- 修改：`src/auto_fishing/automation/engine.py`
- 修改：`src/auto_fishing/app.py`
- 修改：`try/tests/test_engine.py`

**接口：**
- `AutomationCore.__init__` 新增可选 `random_uniform: Callable[[float, float], float] = random.uniform` 与 `event_recorder: Any | None = None`。
- 新增内部字段：`result_click_attempts: int`、`result_next_click_at: float | None`、`result_waiting_logged: bool`。

- [ ] **步骤 1：写入确定性失败测试**

用固定随机采样器验证：进入关闭结算后 0.18 秒前零点击；到时点击客户区 `(0.80, 0.55)`；卡片持续时按 0.40 秒间隔最多点击三次；卡片消失时不重试；就绪后只计数一次；三次后仍存在以 `E_RESULT_DISMISS` 暂停。另设屏幕键盘覆盖首选点，验证选择备用点；三个点全被覆盖则 `E_OSK` 暂停。

- [ ] **步骤 2：确认红灯**

```powershell
& 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe' -m pytest try/tests/test_engine.py -q
```

预期：新增测试因旧代码立即单击 `(0.15, 0.55)` 且不重试而失败。

- [ ] **步骤 3：实现非阻塞计划器**

在等待结算确认时调用 `_schedule_result_click(now, 0.18, 0.42)`；关闭结算阶段先处理 `ready`，再处理当前卡片和计划时间。每次点击后递增次数并安排 `0.40～0.80` 秒复核；第 3 次后卡片仍存在时调用：

```python
self.pause(
    "真实鱼获卡片连续三次点击后仍未关闭",
    now,
    code="E_RESULT_DISMISS",
)
```

首次点击调用一次 `Event.RESULT_CLICKED`，重试不重置状态机进入时间。

- [ ] **步骤 4：实现点击点避障与日志**

按 `(0.80, 0.55)`、`(0.85, 0.45)`、`(0.70, 0.35)` 顺序换算屏幕坐标，排除 `input_service.occlusion_rect()`；全被覆盖时以 `E_OSK` 暂停。通过 `event_recorder.event(...)` 记录 `result.dismiss_scheduled`、`result.dismiss_attempt`、`result.dismiss_waiting` 和 `result.dismiss_failed`；在 `app.py` 将现有运行记录器传给核心。

- [ ] **步骤 5：确认绿灯并提交**

```powershell
& 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe' -m pytest try/tests/test_engine.py try/tests/test_state_machine.py try/tests/test_safe_input.py -q
git add src/auto_fishing/automation/engine.py src/auto_fishing/app.py try/tests/test_engine.py
git commit -m "fix: retry result dismissal after random delays"
```

### 任务 3：全量验证、文档与发布物

**文件：**
- 修改：`AGENTS.md`
- 修改：`doc/验收标准.md`
- 修改：`doc/进展记录/2026-7-13.md`
- 生成但不提交：`dist/异环自动钓鱼.exe`

- [ ] **步骤 1：运行全量测试**

```powershell
& 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe' -m pytest try/tests -q
```

预期：全部测试通过，既有进度、输入和状态机测试无回归。

- [ ] **步骤 2：同步长期规则与验收证据**

记录真实运行根因、夹具、随机范围、重试上限、测试命令和结果；真实卡片关闭与计数仍标记人工确认。

- [ ] **步骤 3：备份并构建**

将当前发布物备份到 `D:\0文件夹\备份\异环自动钓鱼-result-dismiss-prebuild-<时间戳>\`，校验 SHA256 一致后运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build.ps1 -PythonPath 'C:\Users\29551\AppData\Local\Programs\Python\Python313\python.exe'
```

预期：全量测试、单文件构建和 `requireAdministrator uiAccess=false` 清单校验通过。

- [ ] **步骤 4：提交文档并启动发布物**

```powershell
git add AGENTS.md doc/验收标准.md doc/进展记录/2026-7-13.md docs/superpowers/plans/2026-07-13-result-dismiss-retry.md
git commit -m "docs: record reliable result dismissal release"
Start-Process -FilePath 'dist/异环自动钓鱼.exe'
```

真实验收必须证明漩涡期间零点击、真实卡片出现后点击、关闭、就绪和计数 1/1；未通过前不合并 `main`（主分支）。
