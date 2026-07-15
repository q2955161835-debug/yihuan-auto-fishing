# 异环自动钓鱼

一个面向 Windows 的《异环》自动钓鱼工具。程序只读取屏幕画面，并通过 Windows 屏幕键盘与标准鼠标输入完成抛竿、上钩判断、进度控制、结算点击和下一轮循环。

> [!IMPORTANT]
> 本项目仅通过屏幕画面识别和 Windows 标准输入，在操作形式上模拟普通玩家的手动操作；不读取游戏内存、不注入游戏进程。这是一种相对保守的非侵入式实现，但不能保证不被反作弊系统识别，也不用于规避任何检测。使用前请自行了解并遵守游戏服务条款；使用本工具产生的账号或其他风险由使用者承担。

## 当前版本

- 最新正式发布版：[`v2.0.1`](https://github.com/q2955161835-debug/yihuan-auto-fishing/releases/tag/v2.0.1)，已完成用户真实游戏确认。
- `main` 当前候选源码：`v2.0.4`，已通过 428 项离线测试和 Windows 11 单文件构建校验。
- `v2.0.4` 仍待提升权限烟雾、真实游戏 Windows 弹窗与完整闭环复验，因此尚未创建标签或 Release。
- Windows 10 真机仍待人工确认。

## 功能

- 单文件 EXE，双击后通过 Windows UAC（用户账户控制）请求所需权限。
- 绑定游戏窗口后自动完成 F 抛竿/收竿和 A/D 进度控制。
- 支持设置 1～999 次循环，完成一轮后自动进入下一轮。
- 通过客户区比例适配不同分辨率、缩放、窗口、无边框和全屏模式。
- 控制窗口置顶，支持全局 `F8` 紧急暂停。
- 游戏失去前台、识别失败、截图异常或输入异常时安全暂停并释放输入。
- V2 正常运行只保存设置；发生异常或主动点击“报告错误”时生成最近 30 秒诊断包，最多保留 5 份。

## 下载与使用

1. 从 [Releases](https://github.com/q2955161835-debug/yihuan-auto-fishing/releases) 下载最新已发布的 `异环自动钓鱼V2.exe`。
2. 双击 EXE，并在 Windows UAC 提示中确认运行。
3. 启动《异环》，进入可以钓鱼的画面。
4. 在工具中设置目标次数，点击“绑定并开始”，并在 3 秒倒计时内切回游戏。
5. 自动化期间保持游戏在前台，不要操作鼠标和键盘。
6. 需要立即停止输入时按 `F8`；排除问题后可在控制窗口中继续或重新绑定。

程序会启动或复用 Windows 屏幕键盘，并将其定位到游戏显示器左下角。请勿在自动化期间移动或关闭屏幕键盘。

## 诊断与本地数据

V2 设置保存在：

```text
%LOCALAPPDATA%\异环自动钓鱼V2\config.json
```

诊断包只在自动异常或用户主动报告时写入：

```text
%LOCALAPPDATA%\异环自动钓鱼V2\diagnostics\
```

诊断包可能包含最近 30 秒的低分辨率游戏画面、顶部进度槽、识别轨迹和运行事件。提交诊断包前请自行检查其中是否含有不希望公开的画面信息。

## 从源码运行

要求 Windows 与 Python 3.13：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m auto_fishing
```

运行测试：

```powershell
.\.venv\Scripts\python.exe -m pytest try/tests -q
```

构建 V2 单文件 EXE：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_v2.ps1
```

更完整的架构、依赖、构建和验收约束见 [`AGENTS.md`](AGENTS.md) 与 [`doc/验收标准.md`](doc/验收标准.md)。

## 许可证

本项目采用 [PolyForm Noncommercial License 1.0.0](LICENSE)，仅授权非商业用途。允许在非商业目的下使用、研究、修改和分发；任何商业用途均不在授权范围内，需要另行取得许可。

该许可证属于“源码可见”许可证，不是 OSI（开放源代码促进会）认可的开源许可证。完整、具有约束力的条款以 [`LICENSE`](LICENSE) 英文原文为准。
