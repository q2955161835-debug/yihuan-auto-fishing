# 异环自动钓鱼项目规则

## 项目状态

- 当前阶段：离线自动测试、单文件构建和发布物启动烟雾已完成，等待《异环》实机验收；在实机验收完成前不得合并主分支或发布。
- 项目目标：提供 Windows 单文件可执行程序，通过画面识别和系统输入完成《异环》钓鱼循环。
- 使用前提：自动化期间游戏保持前台，用户不操作鼠标键盘；F8 为全局紧急暂停键。
- 范围边界：不读取游戏内存、不注入游戏进程、不规避反作弊，只使用屏幕截图和 Windows 标准输入接口。
- GitHub：私有仓库 `https://github.com/q2955161835-debug/yihuan-auto-fish`；当前推送分支为 `root/feature-auto-fishing-impl`，真实游戏验收前不得合并主分支。

## 技术栈与关键依赖

- Python 3.13.x：桌面程序与自动化逻辑；本次离线构建环境为 Python 3.13.5。
- Tkinter/ttk：小型置顶控制窗口。
- DXcam：Windows Desktop Duplication 高频截屏，目标 30 帧/秒。
- NumPy 与 OpenCV：关键区域颜色分割、连通区域和画面特征识别。
- Win32 ctypes：窗口绑定、客户区坐标、前台检查、DPI 适配、全局 F8、SendInput 和捕获排除。
- PyInstaller：生成单文件 Windows 可执行程序。
- pytest：单元、状态机和合成帧回放测试。

运行依赖固定为 DXcam 0.3.0、NumPy 2.4.1、opencv-python-headless 4.13.0.92；开发依赖固定为 pytest 9.1.0 和 PyInstaller 6.19.0。变更截屏、输入或打包依赖时，必须复测窗口、无边框和全屏模式。

## 架构与数据流

已实现模块边界如下：

- `src/auto_fishing/ui/`：窗口、状态展示和用户操作，不包含识别与按键策略。
- `src/auto_fishing/platform/`：Windows 窗口、DPI、热键和输入接口。
- `src/auto_fishing/capture/`：显示器选择、30 帧/秒截图和客户区裁剪。
- `src/auto_fishing/vision/`：上钩、进度条、黄色标记、绿色区域、结算和就绪画面识别。
- `src/auto_fishing/automation/`：状态机、超时、循环计数、暂停与恢复。
- `src/auto_fishing/storage/`：本地配置、日志和诊断文件自动清理。
- `packaging/`：`asInvoker`、`PerMonitorV2` 清单和 PyInstaller 单文件规格；不得启用管理员权限或控制台子系统。
- `scripts/build.ps1`：默认从项目 `.venv` 运行完整测试后构建单文件发布物并输出 SHA256；干净环境验收可用 `-PythonPath` 参数或 `AUTO_FISHING_PYTHON` 环境变量指定解释器。
- `try/`：测试、合成帧、回放输入和临时产物；删除后不得影响正式程序。
- `try/smoke_exe.ps1`：以启动器 PID 为根递归跟踪所属进程树，检查窗口响应并只关闭本次烟雾拥有的进程；不得按可执行文件路径批量判定或终止进程。
- `流程截图/`：用户提供的带标注流程参考图，不作为可直接匹配的干净模板。
- `doc/验收标准.md`：真实使用流程、验证证据和结论。
- `doc/进展记录/`：按日期记录阶段性修改和异常。
- `docs/superpowers/specs/`：已确认的设计规格。
- `docs/superpowers/plans/`：按测试先行拆分的实施计划。

主数据流：绑定游戏窗口 → 点击开始并进行 3 秒手动切回游戏倒计时 → 验证游戏前台 → 获取客户区与显示器 → 30 帧/秒截图 → 按当前状态裁剪关键区域 → 识别器返回结构化结果 → 状态机决定输入 → SendInput 执行 → 界面展示状态。暂停后点击继续同样进行 3 秒手动切回倒计时并复核前台。任何异常先释放 A/D，再暂停。

Tk 控制窗口的内部句柄必须先通过 `GetAncestor(..., GA_ROOT)` 解析为顶层 HWND；自窗口识别与 `WDA_EXCLUDEFROMCAPTURE` 必须使用同一个顶层句柄。捕获排除失败属于非致命警告，必须显示在控制窗口中。暂停且没有恢复令牌时，worker 必须在截帧、窗口前台/刷新和视觉识别前等待；调用 `latest()` 前保留 operation epoch，取得帧后保留 frame epoch，截图异常、陈旧帧释放失败和后续帧处理错误都只能在对应 epoch 仍有效时触发暂停。迟到错误不得清除新恢复令牌或退出可继续运行的 worker。开始、继续与每轮抛竿均不得调用 `SetForegroundWindow`；倒计时结束时只复核游戏前台，失焦即安全暂停。F8 必须使待开始和待继续倒计时失效，且从热键线程向 Tk 交付取消提示时必须走主线程队列。窗口失效后使用“取消当前轮 → 重新绑定”路径，不得把引擎永久关闭。

## 核心功能

- 三秒倒计时绑定当前游戏窗口。
- 三秒倒计时开始与继续；用户在倒计时内手动切回游戏，程序只验证前台。
- 设置 1～999 次钓鱼循环。
- 自动完成抛竿、等待上钩、收杠、A/D 闭环控制、关闭结算和下一轮。
- 支持不同分辨率、Windows 缩放、窗口、无边框和全屏截图。
- 控制窗口始终置顶；真正独占全屏下不保证可见，但自动化与 F8 暂停仍应工作。
- 识别失败、窗口异常、截图停滞、输入失败或 F8 时安全暂停。
- 正常帧只保存在内存；异常诊断最多 20 份并清理超过 7 天的文件。

## 本地数据与环境变量

- 配置和诊断目录使用 `%LOCALAPPDATA%\异环自动钓鱼\`，不得写入敏感信息；诊断清理只允许作用于其 `diagnostics` 子目录。
- 设置文件固定为 `%LOCALAPPDATA%\异环自动钓鱼\config.json`；读取设置时拒绝溢出及 `NaN`、`Infinity` 等非有限数字。
- `.env` 是真实环境变量账本，已被 Git 忽略；当前项目不需要环境变量。
- `.env.example` 是可提交的变量名账本；新增、删除或改名变量时同步更新读取逻辑与文档。

## 运行、测试与构建

在项目根目录建立 Python 3.13 `.venv` 并安装固定依赖后，保持以下命令可用；若命令变化，必须同步更新本文件：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m auto_fishing
.\.venv\Scripts\python.exe -m pytest try/tests -q
powershell -ExecutionPolicy Bypass -File scripts/build.ps1
powershell -ExecutionPolicy Bypass -File try/smoke_exe.ps1
```

干净虚拟环境验收可将解释器传给构建脚本，例如：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build.ps1 -PythonPath try/output/clean-venv/Scripts/python.exe
```

发布物为 `dist/异环自动钓鱼.exe`，`dist/` 保持 Git 忽略。构建脚本必须可重复执行；发布前除自动烟雾外，必须在无开发环境依赖的 Windows 会话中完成启动人工复核。

## 验收标准

- 自动测试必须覆盖坐标缩放、状态超时、A/D 方向与释放、F8、安全暂停、循环计数和诊断清理。
- 合成动态进度条以 30 帧/秒回放，识别与控制处理不得持续落后于最新帧。
- 实机必须分别检查窗口、无边框和全屏；无法由自动测试判断的画面识别结果标记为人工确认。
- 当前离线基线为 214 个 pytest 测试、单文件构建、启动/响应/所有权隔离/关闭烟雾通过；这不替代真实游戏人工验收。
- 完成一个阶段后同步更新 `doc/验收标准.md` 中的结果、问题和最终结论。

## 执行与报告要求

- 修改识别阈值时记录使用的截图或合成样本、命令和结果，不得只写“已优化”。
- 识别或控制失败时记录问题、原因、解决方案，并优先增加可重复测试。
- 完成报告需列出新增或修改文件、测试命令、实机待确认项，以及是否存在重复实现、临时补丁或新增技术债。
