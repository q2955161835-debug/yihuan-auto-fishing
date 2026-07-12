# 异环自动钓鱼项目规则

## 项目状态

- 当前阶段：用户已指定当前已验证版本为 `main` 基线并完成私有仓库推送；完整运行记录功能已在隔离分支完成 228 项离线自动测试、单文件构建和发布物启动烟雾，等待真实《异环》验收后合并；真实《异环》验收仍待执行。
- 项目目标：提供 Windows 单文件可执行程序，通过画面识别和系统输入完成《异环》钓鱼循环。
- 使用前提：自动化期间游戏保持前台，用户不操作鼠标键盘；F8 为全局紧急暂停键。
- 范围边界：不读取游戏内存、不注入游戏进程、不规避反作弊，只使用屏幕截图和 Windows 标准输入接口。
- GitHub：私有仓库 `https://github.com/q2955161835-debug/yihuan-auto-fish`；默认分支为 `main`，当前本地 `main` 跟踪 `origin/main`。

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
- `src/auto_fishing/platform/`：Windows 窗口、DPI、热键、鼠标输入接口和系统屏幕键盘生命周期/按键适配。
- `src/auto_fishing/capture/`：显示器选择、30 帧/秒截图和客户区裁剪。
- `src/auto_fishing/vision/`：上钩、进度条、黄色标记、绿色区域、结算和就绪画面识别。
- `src/auto_fishing/automation/`：状态机、超时、循环计数、暂停与恢复。
- `src/auto_fishing/storage/`：本地配置、异常诊断和完整运行记录的异步落盘与清理。
- `packaging/`：`requireAdministrator`、`PerMonitorV2` 清单和 PyInstaller 单文件规格；规格文件必须同时设置 `uac_admin=True`，否则 PyInstaller 会把自定义清单重写为 `asInvoker`。发布物启动时请求 UAC（用户账户控制）提升，不得启用控制台子系统。
- `scripts/build.ps1`：默认从项目 `.venv` 运行完整测试后构建单文件发布物并输出 SHA256；干净环境验收可用 `-PythonPath` 参数或 `AUTO_FISHING_PYTHON` 环境变量指定解释器。
- `scripts/verify_release.py`：读取最终 EXE 的 `RT_MANIFEST` 内嵌资源，构建后强制验证 `requireAdministrator` 与 `uiAccess=false`，不得只检查源 XML。
- `try/`：测试、合成帧、回放输入和临时产物；删除后不得影响正式程序。
- `try/smoke_exe.ps1`：以启动器 PID 为根递归跟踪所属进程树，检查窗口响应并只关闭本次烟雾拥有的进程；不得按可执行文件路径批量判定或终止进程。
- `流程截图/`：用户提供的带标注流程参考图，不作为可直接匹配的干净模板。
- `doc/验收标准.md`：真实使用流程、验证证据和结论。
- `doc/进展记录/`：按日期记录阶段性修改和异常。
- `docs/superpowers/specs/`：已确认的设计规格。
- `docs/superpowers/plans/`：按测试先行拆分的实施计划。

主数据流：绑定游戏窗口 → 启动或复用 Windows 屏幕键盘并固定到游戏显示器左下角 → 点击开始并进行 3 秒手动切回游戏倒计时 → 验证游戏前台 → 获取客户区与显示器 → 30 帧/秒截图 → 结算识别排除屏幕键盘遮挡、其他识别保持原区域 → 识别器返回结构化结果 → 状态机决定输入 → 鼠标点击/按住屏幕键盘 F/A/D → 界面展示状态。应用启动后同时建立完整运行记录目录：每个已处理客户端帧写入结构化观测、前后状态和低分辨率截图；键鼠请求与系统输入返回值写入同一事件流。暂停后点击继续同样进行 3 秒手动切回倒计时并复核前台。任何异常先抬起鼠标并释放 A/D，再暂停。

游戏不接受程序直接发送的键盘 `SendInput`，但真实验证接受实体鼠标点击 Windows 屏幕键盘生成的按键。生产输入后端必须保持屏幕键盘可见，通过客户区归一化坐标定位 F/A/D：F 轻点，A/D 以鼠标左键按住，换向先抬起再移动并按下。所有原生 `user32` 函数必须声明准确的 `argtypes/restype`，尤其 `SetWindowPos` 的两个 `HWND` 参数必须为指针宽度，避免错误 1400。`osk.exe` 必须通过 `ShellExecuteW` 的普通 `open` 动作启动；直接子进程创建会触发 Windows 错误 740。发布物经用户授权请求管理员权限，以跨越屏幕键盘 `UIAccess`（用户界面访问）完整性边界；同时保留错误 5 降级：读取并验证现有位置，安全则继续，否则要求用户手动拖到左下角。已有屏幕键盘退出时保留；本程序启动的实例在释放输入后尽力关闭，若 Windows 拒绝关闭则安全保留，不得强制终止系统辅助功能进程。屏幕键盘消失、越界或遮挡顶部进度/右下角准备区域时以 `E_OSK` 安全暂停；不得退回直接扫描码作为静默降级。

Tk 控制窗口的内部句柄必须先通过 `GetAncestor(..., GA_ROOT)` 解析为顶层 HWND；自窗口识别与 `WDA_EXCLUDEFROMCAPTURE` 必须使用同一个顶层句柄。捕获排除失败属于非致命警告，必须显示在控制窗口中。暂停且没有恢复令牌时，worker 必须在截帧、窗口前台/刷新和视觉识别前等待；调用 `latest()` 前保留 operation epoch，取得帧后保留 frame epoch，截图异常、陈旧帧释放失败和后续帧处理错误都只能在对应 epoch 仍有效时触发暂停。迟到错误不得清除新恢复令牌或退出可继续运行的 worker。开始、继续与每轮抛竿均不得调用 `SetForegroundWindow`；倒计时结束时只复核游戏前台，失焦即安全暂停。F8 必须使待开始和待继续倒计时失效，且从热键线程向 Tk 交付取消提示时必须走主线程队列。窗口失效后使用“取消当前轮 → 重新绑定”路径，不得把引擎永久关闭。

## 核心功能

- 三秒倒计时绑定当前游戏窗口。
- 三秒倒计时开始与继续；用户在倒计时内手动切回游戏，程序只验证前台。
- 设置 1～999 次钓鱼循环。
- 通过左下角 Windows 屏幕键盘自动完成抛竿、等待上钩、收杠、A/D 闭环控制；关闭结算和下一轮继续使用游戏画面坐标点击。
- 支持不同分辨率、Windows 缩放、窗口、无边框和全屏截图。
- 控制窗口始终置顶；真正独占全屏下不保证可见，但自动化与 F8 暂停仍应工作。
- 识别失败、窗口异常、截图停滞、输入失败或 F8 时安全暂停。
- 每次应用启动保存完整运行记录；异常诊断最多 20 份并清理超过 7 天的文件。

## 本地数据与环境变量

- 配置、诊断和运行记录目录使用 `%LOCALAPPDATA%\异环自动钓鱼\`，不得写入敏感信息；诊断清理只允许作用于其 `diagnostics` 子目录。
- 完整运行记录固定写入其 `runs` 子目录：每次启动一个独立目录，含 `events.jsonl` 和 `frames/`；帧截图最长边 480 像素、JPEG 质量 50，仅保留最近 30 个运行目录。异步写入队列上限为 300 项；队列满或写入失败必须以 `PAUSED/E_LOGGING` 暂停并释放输入，不能丢帧后继续自动化。
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

- 自动测试必须覆盖屏幕键盘生命周期/左下角定位/遮挡拒绝、坐标缩放、F 与 A/D 鼠标按住和释放、状态超时、F8、安全暂停、循环计数和诊断清理。
- 合成动态进度条以 30 帧/秒回放，识别与控制处理不得持续落后于最新帧。
- 实机必须分别检查窗口、无边框和全屏；无法由自动测试判断的画面识别结果标记为人工确认。
- `codex/feat-runtime-logging` 分支离线基线为 228 个 pytest 测试、单文件构建、启动/响应/所有权隔离/关闭烟雾通过；这不替代真实游戏人工验收。
- 完成一个阶段后同步更新 `doc/验收标准.md` 中的结果、问题和最终结论。

## 执行与报告要求

- 修改识别阈值时记录使用的截图或合成样本、命令和结果，不得只写“已优化”。
- 识别或控制失败时记录问题、原因、解决方案，并优先增加可重复测试。
- 完成报告需列出新增或修改文件、测试命令、实机待确认项，以及是否存在重复实现、临时补丁或新增技术债。
