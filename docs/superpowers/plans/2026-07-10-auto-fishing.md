# 《异环》自动钓鱼 Implementation Plan（实施计划）

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal（目标）：** 构建一个 Windows 单文件可执行程序，通过每秒 30 帧的画面识别和安全输入完成指定次数的《异环》自动钓鱼循环。

**Architecture（架构）：** Tkinter 主线程只负责置顶窗口，DXcam 截屏和自动化状态机在工作线程中运行。视觉层把客户区画面转换为结构化观测，状态机和闭环控制器决定动作，Win32 输入层集中执行并保证暂停时释放全部按键。

**Tech Stack（技术栈）：** Python 3.13.14、Tkinter/ttk、DXcam 0.3.0、NumPy 2.4.1、OpenCV Headless 4.13.0.92、ctypes/Win32、pytest 9.1.0、PyInstaller 6.19.0。

## Global Constraints（全局约束）

- 目标平台为 64 位 Windows 10/11；发布物为单个 `异环自动钓鱼.exe`。
- 自动化期间游戏必须保持前台，用户不操作鼠标键盘；F8 是必需的全局紧急暂停键。
- 截图目标频率固定为 30 帧/秒；控制线程只消费最新帧，不补处理旧帧。
- 所有坐标相对游戏客户区归一化，不允许写死 2560×1440 像素坐标。
- 不读取游戏内存、不注入游戏进程、不安装驱动、不规避反作弊。
- 一帧丢失绿色区域或黄色标记就释放 A/D；连续六帧丢失则暂停。
- 正常截图只驻留内存；诊断文件超过 7 天删除，且最多保留最近 20 组。
- 所有测试文件、合成帧与回放产物必须位于 `try/`。
- 每个任务先写失败测试、确认失败、写最小实现、确认通过，再提交 Git。

---

## 计划文件结构

```text
异环自动钓鱼/
├── pyproject.toml                  # 包元数据、pytest 配置和 Python 版本约束
├── requirements.txt               # 运行依赖固定版本
├── requirements-dev.txt           # 测试与打包依赖固定版本
├── src/auto_fishing/
│   ├── __init__.py                # 包版本
│   ├── __main__.py                # python -m 入口
│   ├── app.py                     # 依赖装配和程序生命周期
│   ├── model.py                   # 公共枚举、矩形、观测和界面快照
│   ├── automation/
│   │   ├── state_machine.py       # 纯状态转换、计数和超时
│   │   └── engine.py              # 30 帧循环与各服务编排
│   ├── capture/
│   │   └── dxcam_source.py        # DXcam 最新帧和实际帧率
│   ├── platform/
│   │   ├── input.py               # 安全输入门面和 Win32 SendInput
│   │   ├── hotkey.py              # F8 全局热键线程
│   │   └── windowing.py           # DPI、绑定、客户区、显示器和前台
│   ├── storage/
│   │   ├── diagnostics.py         # 异常截图、日志和保留清理
│   │   └── settings.py            # 非敏感 JSON 配置
│   ├── ui/
│   │   └── main_window.py         # 320×240 置顶窗口
│   └── vision/
│       ├── geometry.py            # 归一化区域与客户区裁剪
│       ├── progress.py            # 绿色区、黄色标记和 A/D 决策
│       └── scenes.py              # 上钩、结算和就绪识别
├── packaging/
│   ├── app.manifest               # 每显示器 DPI 与 asInvoker 清单
│   └── auto_fishing.spec          # 单文件无控制台打包配置
├── scripts/build.ps1              # 可重复构建脚本
└── try/
    ├── tests/                     # pytest 测试
    ├── generated/                 # 测试生成图，Git 忽略
    └── smoke_exe.ps1              # 发布物启动冒烟测试
```

---

### 任务 1：项目骨架、公共模型与归一化坐标

**文件：**

- 创建：`pyproject.toml`
- 创建：`requirements.txt`
- 创建：`requirements-dev.txt`
- 创建：`src/auto_fishing/__init__.py`
- 创建：`src/auto_fishing/model.py`
- 创建：`src/auto_fishing/vision/__init__.py`
- 创建：`src/auto_fishing/vision/geometry.py`
- 创建：`try/tests/test_geometry.py`

**接口：**

- 产出：`Rect(left: int, top: int, right: int, bottom: int)`、`NormalizedRect(left: float, top: float, right: float, bottom: float).to_pixels(client: Rect) -> Rect`。
- 产出：`ProgressObservation`、`SceneObservation`、`FramePacket`、`RuntimeSnapshot`、`FishingState`、`Direction`。
- 后续任务只从 `auto_fishing.model` 导入这些共享类型，不重复声明。

- [ ] **步骤 1：写坐标映射失败测试**

```python
# try/tests/test_geometry.py
import pytest
from auto_fishing.model import NormalizedRect, Rect
from auto_fishing.vision.geometry import crop_normalized


@pytest.mark.parametrize("size", [(1280, 720), (1920, 1080), (2560, 1440), (1600, 1000)])
def test_progress_roi_scales_with_client(size):
    width, height = size
    client = Rect(100, 200, 100 + width, 200 + height)
    roi = NormalizedRect(0.24, 0.00, 0.76, 0.15).to_pixels(client)
    assert roi == Rect(100 + round(width * 0.24), 200, 100 + round(width * 0.76), 200 + round(height * 0.15))


def test_normalized_rect_rejects_invalid_bounds():
    with pytest.raises(ValueError, match="0 <= left < right <= 1"):
        NormalizedRect(0.8, 0.1, 0.2, 0.9)


def test_crop_normalized_returns_expected_shape():
    import numpy as np
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    crop = crop_normalized(frame, NormalizedRect(0.25, 0.20, 0.75, 0.80))
    assert crop.shape == (60, 100, 3)
```

- [ ] **步骤 2：建立虚拟环境并确认测试因模块缺失而失败**

运行：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
```

先创建以下依赖文件：

```text
# requirements.txt
dxcam==0.3.0
numpy==2.4.1
opencv-python-headless==4.13.0.92
```

```text
# requirements-dev.txt
-r requirements.txt
pytest==9.1.0
pyinstaller==6.19.0
```

再运行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest try/tests/test_geometry.py -q
```

预期：收集失败，提示 `No module named 'auto_fishing'` 或缺少 `Rect`。

- [ ] **步骤 3：写最小包配置和公共模型**

```toml
# pyproject.toml
[build-system]
requires = ["setuptools>=80,<81"]
build-backend = "setuptools.build_meta"

[project]
name = "n-te-auto-fishing"
version = "0.1.0"
requires-python = ">=3.13,<3.14"
dependencies = [
  "dxcam==0.3.0",
  "numpy==2.4.1",
  "opencv-python-headless==4.13.0.92",
]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["try/tests"]
pythonpath = ["src"]
```

```python
# src/auto_fishing/model.py
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
import numpy as np


class FishingState(str, Enum):
    UNBOUND = "未绑定"
    READY = "准备抛竿"
    WAIT_BITE = "等待上钩"
    WAIT_BAR = "等待进度条"
    CONTROL = "控制进度条"
    WAIT_RESULT = "等待结算"
    DISMISS_RESULT = "关闭结算"
    INTER_ROUND = "轮间等待"
    PAUSED = "已暂停"
    COMPLETE = "已完成"


class Direction(str, Enum):
    RELEASE = "release"
    LEFT = "left"
    RIGHT = "right"


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int: return self.right - self.left
    @property
    def height(self) -> int: return self.bottom - self.top


@dataclass(frozen=True)
class NormalizedRect:
    left: float
    top: float
    right: float
    bottom: float

    def __post_init__(self) -> None:
        if not (0 <= self.left < self.right <= 1 and 0 <= self.top < self.bottom <= 1):
            raise ValueError("0 <= left < right <= 1 and 0 <= top < bottom <= 1")

    def to_pixels(self, client: Rect) -> Rect:
        return Rect(
            client.left + round(client.width * self.left),
            client.top + round(client.height * self.top),
            client.left + round(client.width * self.right),
            client.top + round(client.height * self.bottom),
        )


@dataclass(frozen=True)
class ProgressObservation:
    green_left: float
    green_right: float
    yellow_x: float
    confidence: float
    timestamp: float


@dataclass(frozen=True)
class SceneObservation:
    bite: bool = False
    result: bool = False
    ready: bool = False
    progress: ProgressObservation | None = None


@dataclass(frozen=True)
class FramePacket:
    frame: np.ndarray
    timestamp: float
    fps: float


@dataclass(frozen=True)
class RuntimeSnapshot:
    state: FishingState
    completed: int
    target: int
    fps: float
    error: str = ""
```

```python
# src/auto_fishing/vision/geometry.py
import numpy as np
from auto_fishing.model import NormalizedRect


def crop_normalized(frame: np.ndarray, roi: NormalizedRect) -> np.ndarray:
    height, width = frame.shape[:2]
    x1, x2 = round(width * roi.left), round(width * roi.right)
    y1, y2 = round(height * roi.top), round(height * roi.bottom)
    return frame[y1:y2, x1:x2]
```

`src/auto_fishing/__init__.py` 写入 `__version__ = "0.1.0"`，`vision/__init__.py` 保持空文件。

- [ ] **步骤 4：运行坐标测试**

运行：`.\.venv\Scripts\python.exe -m pytest try/tests/test_geometry.py -q`

预期：`6 passed`。

- [ ] **步骤 5：提交任务 1**

```powershell
git add pyproject.toml requirements.txt requirements-dev.txt src/auto_fishing try/tests/test_geometry.py
git commit -m "feat: establish typed automation foundation"
```

---

### 任务 2：设置、诊断保存与自动删除

**文件：**

- 创建：`src/auto_fishing/storage/__init__.py`
- 创建：`src/auto_fishing/storage/settings.py`
- 创建：`src/auto_fishing/storage/diagnostics.py`
- 创建：`try/tests/test_storage.py`

**接口：**

- 产出：`SettingsStore.load() -> AppSettings`、`SettingsStore.save(settings)`。
- 产出：`DiagnosticsStore.save(frame, code, detail, now)` 和 `cleanup(now)`。
- 诊断清理只能操作构造函数传入的 `diagnostics_dir`，不得接受任意删除路径。

- [ ] **步骤 1：写存储和清理失败测试**

```python
# try/tests/test_storage.py
from datetime import datetime, timedelta, timezone
import json
import numpy as np
from auto_fishing.storage.diagnostics import DiagnosticsStore
from auto_fishing.storage.settings import AppSettings, SettingsStore


def test_settings_round_trip(tmp_path):
    store = SettingsStore(tmp_path / "config.json")
    expected = AppSettings(target_count=8, window_x=12, window_y=34)
    store.save(expected)
    assert store.load() == expected


def test_diagnostics_delete_old_and_keep_twenty_groups(tmp_path):
    store = DiagnosticsStore(tmp_path / "diagnostics")
    now = datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc)
    frame = np.zeros((20, 20, 3), dtype=np.uint8)
    for index in range(25):
        store.save(frame, "E_TEST", str(index), now - timedelta(hours=index))
    store.save(frame, "E_OLD", "old", now - timedelta(days=8))
    store.cleanup(now)
    groups = {path.stem for path in (tmp_path / "diagnostics").iterdir()}
    assert len(groups) == 20
    assert not any("E_OLD" in path.name for path in (tmp_path / "diagnostics").iterdir())
    assert not (tmp_path / "流程截图").exists()


def test_diagnostic_metadata_contains_no_frame_bytes(tmp_path):
    store = DiagnosticsStore(tmp_path / "diagnostics")
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)
    store.save(np.zeros((4, 4, 3), dtype=np.uint8), "E_INPUT", "发送失败", now)
    data = json.loads(next((tmp_path / "diagnostics").glob("*.json")).read_text("utf-8"))
    assert set(data) == {"code", "detail", "created_at"}
```

- [ ] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python.exe -m pytest try/tests/test_storage.py -q`

预期：收集失败，提示缺少 `auto_fishing.storage`。

- [ ] **步骤 3：实现设置和安全清理**

```python
# src/auto_fishing/storage/settings.py
from dataclasses import asdict, dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class AppSettings:
    target_count: int = 1
    window_x: int = 20
    window_y: int = 20


class SettingsStore:
    def __init__(self, path: Path) -> None: self.path = path

    def load(self) -> AppSettings:
        if not self.path.exists(): return AppSettings()
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            count = min(999, max(1, int(raw.get("target_count", 1))))
            return AppSettings(count, int(raw.get("window_x", 20)), int(raw.get("window_y", 20)))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return AppSettings()

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), "utf-8")
        temp.replace(self.path)
```

```python
# src/auto_fishing/storage/diagnostics.py
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import cv2
import numpy as np


class DiagnosticsStore:
    def __init__(self, diagnostics_dir: Path) -> None:
        self.root = diagnostics_dir.resolve()

    def save(self, frame: np.ndarray, code: str, detail: str, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        self.root.mkdir(parents=True, exist_ok=True)
        stem = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}_{code}"
        image_path, meta_path = self.root / f"{stem}.png", self.root / f"{stem}.json"
        if not cv2.imwrite(str(image_path), frame): raise OSError("诊断截图写入失败")
        meta_path.write_text(json.dumps({"code": code, "detail": detail, "created_at": now.isoformat()}, ensure_ascii=False), "utf-8")
        for path in (image_path, meta_path): os.utime(path, (now.timestamp(), now.timestamp()))
        self.cleanup(now)
        return stem

    def cleanup(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        if not self.root.exists(): return
        groups: dict[str, list[Path]] = {}
        for path in self.root.iterdir():
            resolved = path.resolve()
            resolved.relative_to(self.root)
            if path.is_file() and path.suffix in {".png", ".json"}:
                groups.setdefault(path.stem, []).append(path)
        cutoff = now.timestamp() - timedelta(days=7).total_seconds()
        fresh = []
        for stem, files in groups.items():
            newest = max(path.stat().st_mtime for path in files)
            if newest < cutoff:
                for path in files: path.unlink(missing_ok=True)
            else: fresh.append((newest, stem, files))
        for _, _, files in sorted(fresh, reverse=True)[20:]:
            for path in files: path.unlink(missing_ok=True)
```

- [ ] **步骤 4：运行存储测试**

运行：`.\.venv\Scripts\python.exe -m pytest try/tests/test_storage.py -q`

预期：`3 passed`。

- [ ] **步骤 5：提交任务 2**

```powershell
git add src/auto_fishing/storage try/tests/test_storage.py
git commit -m "feat: add bounded diagnostic retention"
```

---

### 任务 3：安全输入门面与 Windows SendInput

**文件：**

- 创建：`src/auto_fishing/platform/__init__.py`
- 创建：`src/auto_fishing/platform/input.py`
- 创建：`try/tests/test_safe_input.py`

**接口：**

- 产出：`InputBackend` 协议的 `key_down(key)`、`key_up(key)`、`click(x, y)`。
- 产出：`SafeInput.tap_f()`、`set_direction(Direction)`、`click(x, y)`、`release_all()`。
- `SafeInput` 是自动化代码唯一允许调用的输入对象。

- [ ] **步骤 1：写按键互斥和释放失败测试**

```python
# try/tests/test_safe_input.py
from auto_fishing.model import Direction
from auto_fishing.platform.input import SafeInput


class FakeBackend:
    def __init__(self): self.events = []
    def key_down(self, key): self.events.append(("down", key))
    def key_up(self, key): self.events.append(("up", key))
    def click(self, x, y): self.events.append(("click", x, y))


def test_direction_switch_releases_old_key_first():
    backend = FakeBackend(); safe = SafeInput(backend, sleep=lambda _: None)
    safe.set_direction(Direction.LEFT); safe.set_direction(Direction.RIGHT)
    assert backend.events == [("down", "A"), ("up", "A"), ("down", "D")]


def test_release_all_is_idempotent():
    backend = FakeBackend(); safe = SafeInput(backend, sleep=lambda _: None)
    safe.set_direction(Direction.RIGHT); safe.release_all(); safe.release_all()
    assert backend.events == [("down", "D"), ("up", "D")]


def test_tap_f_and_click_are_balanced():
    backend = FakeBackend(); safe = SafeInput(backend, sleep=lambda _: None)
    safe.tap_f(); safe.click(200, 300)
    assert backend.events == [("down", "F"), ("up", "F"), ("click", 200, 300)]
```

- [ ] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python.exe -m pytest try/tests/test_safe_input.py -q`

预期：收集失败，提示缺少 `SafeInput`。

- [ ] **步骤 3：实现安全门面与 Win32 后端**

`SafeInput` 必须完全按以下状态规则实现：

```python
# src/auto_fishing/platform/input.py（核心公共部分）
from __future__ import annotations
import ctypes
from ctypes import wintypes
from time import sleep as real_sleep
from typing import Callable, Protocol
from auto_fishing.model import Direction


class InputFailure(RuntimeError): pass


class InputBackend(Protocol):
    def key_down(self, key: str) -> None: raise NotImplementedError
    def key_up(self, key: str) -> None: raise NotImplementedError
    def click(self, x: int, y: int) -> None: raise NotImplementedError


class SafeInput:
    def __init__(self, backend: InputBackend, sleep: Callable[[float], None] = real_sleep) -> None:
        self.backend, self.sleep, self.held = backend, sleep, set()

    def _down(self, key: str) -> None:
        if key not in self.held: self.backend.key_down(key); self.held.add(key)

    def _up(self, key: str) -> None:
        if key in self.held: self.backend.key_up(key); self.held.remove(key)

    def tap_f(self) -> None:
        self._down("F")
        try: self.sleep(0.05)
        finally: self._up("F")

    def set_direction(self, direction: Direction) -> None:
        desired = {Direction.LEFT: "A", Direction.RIGHT: "D"}.get(direction)
        for key in ("A", "D"):
            if key != desired: self._up(key)
        if desired: self._down(desired)

    def click(self, x: int, y: int) -> None: self.backend.click(x, y)

    def release_all(self) -> None:
        for key in tuple(self.held):
            try: self.backend.key_up(key)
            finally: self.held.discard(key)
```

同一文件内实现 `Win32InputBackend`：键盘使用 `SendInput` 的扫描码标志，A/F/D 扫描码分别为 `0x1E/0x21/0x20`；鼠标先检查 `SetCursorPos` 返回值，再发送左键按下和抬起。每次 `SendInput` 返回数量不等于请求数量时抛出 `InputFailure`。结构体使用 `ctypes.Structure` 明确定义 `KEYBDINPUT`、`MOUSEINPUT`、`INPUT_UNION` 和 `INPUT`，不得调用已弃用的 `keybd_event` 或 `mouse_event`。

- [ ] **步骤 4：运行输入测试并做 Win32 结构烟雾检查**

运行：

```powershell
.\.venv\Scripts\python.exe -m pytest try/tests/test_safe_input.py -q
.\.venv\Scripts\python.exe -c "from auto_fishing.platform.input import Win32InputBackend; print(Win32InputBackend.__name__)"
```

预期：`3 passed`，第二条输出 `Win32InputBackend`；烟雾检查不得真的发送输入。

- [ ] **步骤 5：提交任务 3**

```powershell
git add src/auto_fishing/platform try/tests/test_safe_input.py
git commit -m "feat: add fail-safe Windows input"
```

---

### 任务 4：窗口绑定、DPI、F8 与 30 帧截屏

**文件：**

- 创建：`src/auto_fishing/platform/windowing.py`
- 创建：`src/auto_fishing/platform/hotkey.py`
- 创建：`src/auto_fishing/capture/__init__.py`
- 创建：`src/auto_fishing/capture/dxcam_source.py`
- 创建：`try/tests/test_capture_window.py`

**接口：**

- 产出：`BoundWindow(hwnd, title, client_rect, monitor_rect, output_index)`。
- 产出：`WindowService.bind_foreground()`、`refresh(bound)`、`activate(bound)`、`is_foreground(bound)`。
- 产出：`GlobalHotkey.start(callback) -> bool`、`stop()`。
- 产出：`DxcamFrameSource.start(output_index)`、`latest() -> FramePacket`、`stop()`。

- [ ] **步骤 1：写窗口和最新帧失败测试**

```python
# try/tests/test_capture_window.py
import numpy as np
from auto_fishing.capture.dxcam_source import DxcamFrameSource


class FakeCamera:
    def __init__(self): self.frames = []
    def start(self, target_fps): assert target_fps == 30
    def get_latest_frame(self, with_timestamp=True): return self.frames[-1]
    def stop(self): pass
    def release(self): pass


def test_capture_uses_latest_frame_and_reports_rate():
    camera = FakeCamera()
    camera.frames.extend([(np.zeros((10, 10, 3), dtype=np.uint8), 1.0), (np.ones((10, 10, 3), dtype=np.uint8), 1.04)])
    source = DxcamFrameSource(camera_factory=lambda _: camera, clock=lambda: 1.04)
    source.start(0); packet = source.latest()
    assert packet.frame.mean() == 1
    assert packet.timestamp == 1.04
    assert packet.fps >= 0


def test_capture_stop_is_idempotent():
    camera = FakeCamera(); source = DxcamFrameSource(camera_factory=lambda _: camera)
    source.start(0); source.stop(); source.stop()
```

- [ ] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python.exe -m pytest try/tests/test_capture_window.py -q`

预期：收集失败，提示缺少 `DxcamFrameSource`。

- [ ] **步骤 3：实现 DXcam 最新帧源**

```python
# src/auto_fishing/capture/dxcam_source.py
from collections import deque
from time import monotonic
from typing import Callable
import dxcam
from auto_fishing.model import FramePacket


class DxcamFrameSource:
    def __init__(self, camera_factory: Callable | None = None, clock: Callable[[], float] = monotonic) -> None:
        self.camera_factory = camera_factory or (lambda index: dxcam.create(output_idx=index, output_color="BGR", processor_backend="cv2"))
        self.clock, self.camera, self.timestamps = clock, None, deque(maxlen=31)

    def start(self, output_index: int) -> None:
        self.stop(); self.camera = self.camera_factory(output_index)
        self.camera.start(target_fps=30)

    def latest(self) -> FramePacket:
        if self.camera is None: raise RuntimeError("截屏尚未启动")
        frame, timestamp = self.camera.get_latest_frame(with_timestamp=True)
        self.timestamps.append(float(timestamp))
        fps = 0.0
        if len(self.timestamps) > 1 and self.timestamps[-1] > self.timestamps[0]:
            fps = (len(self.timestamps) - 1) / (self.timestamps[-1] - self.timestamps[0])
        return FramePacket(frame=frame, timestamp=float(timestamp), fps=fps)

    def stop(self) -> None:
        if self.camera is not None:
            try: self.camera.stop()
            finally: self.camera.release(); self.camera = None; self.timestamps.clear()
```

- [ ] **步骤 4：实现窗口服务与 F8 热键**

`windowing.py` 使用 `ctypes.windll.user32`，公共类型和签名固定为：

```python
@dataclass(frozen=True)
class BoundWindow:
    hwnd: int
    title: str
    client_rect: Rect
    monitor_rect: Rect
    output_index: int


class WindowService:
    def enable_dpi_awareness(self) -> None:
        if not user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            user32.SetProcessDPIAware()

    def is_foreground(self, bound: BoundWindow) -> bool:
        return int(user32.GetForegroundWindow()) == bound.hwnd

    def activate(self, bound: BoundWindow) -> bool:
        user32.ShowWindow(bound.hwnd, 9)
        user32.SetForegroundWindow(bound.hwnd)
        return self.is_foreground(bound)

    def exclude_from_capture(self, hwnd: int) -> bool:
        return bool(user32.SetWindowDisplayAffinity(hwnd, 0x11))
```

`bind_foreground()` 依次执行 `GetForegroundWindow → IsIconic → GetWindowTextW → GetClientRect → ClientToScreen → MonitorFromWindow`，排除本程序 hwnd、空标题、最小化窗口和小于 960×540 的客户区，再返回 `BoundWindow`。`refresh(bound)` 对原 hwnd 重做客户区与显示器计算，hwnd 失效或尺寸不合格时抛 `WindowBindingError`。

实现时用 `EnumDisplayMonitors` 生成稳定的 `output_index`，并验证 DXcam 输出分辨率与 `monitor_rect` 一致；找不到唯一输出时抛出“无法映射游戏所在显示器”，不得默认为主显示器。

`hotkey.py` 在专用线程内调用 `RegisterHotKey(None, 1, 0, VK_F8)` 并运行 `GetMessageW` 消息循环；收到 `WM_HOTKEY` 时只设置线程安全事件或调用无阻塞回调。`start()` 等待注册结果，冲突返回 `False`；`stop()` 调用 `PostThreadMessageW(thread_id, WM_QUIT, 0, 0)`，在线程退出前注销热键。

- [ ] **步骤 5：运行测试和只读 Windows 冒烟检查**

运行：

```powershell
.\.venv\Scripts\python.exe -m pytest try/tests/test_capture_window.py -q
.\.venv\Scripts\python.exe -c "from auto_fishing.platform.windowing import WindowService; WindowService().enable_dpi_awareness(); print('dpi-ok')"
```

预期：`2 passed` 和 `dpi-ok`。此步骤不绑定窗口、不注册 F8、不发送输入。

- [ ] **步骤 6：提交任务 4**

```powershell
git add src/auto_fishing/platform src/auto_fishing/capture try/tests/test_capture_window.py
git commit -m "feat: bind game window and capture latest frames"
```

---

### 任务 5：顶部进度条识别与 A/D 控制策略

**文件：**

- 创建：`src/auto_fishing/vision/progress.py`
- 创建：`try/tests/test_progress.py`

**接口：**

- 产出：`ProgressRecognizer.detect(top_roi, timestamp) -> ProgressObservation | None`。
- 产出：`ProgressController.decide(observation) -> Direction`。
- 所有返回坐标归一化到传入顶部区域的宽度。

- [ ] **步骤 1：写快速移动和安全带失败测试**

```python
# try/tests/test_progress.py
import cv2
import numpy as np
from auto_fishing.model import Direction
from auto_fishing.vision.progress import ProgressController, ProgressRecognizer


def frame(green=(70, 170), yellow=120):
    image = np.zeros((120, 300, 3), dtype=np.uint8)
    cv2.rectangle(image, (green[0], 40), (green[1], 70), (255, 210, 30), -1)
    cv2.rectangle(image, (yellow - 3, 34), (yellow + 3, 76), (0, 230, 255), -1)
    return image


def test_detects_green_interval_and_yellow_marker():
    obs = ProgressRecognizer().detect(frame(), 1.0)
    assert obs is not None
    assert 0.22 < obs.green_left < 0.26
    assert 0.55 < obs.green_right < 0.59
    assert 0.38 < obs.yellow_x < 0.42


def test_controller_moves_toward_inner_safe_band():
    recognizer, controller = ProgressRecognizer(), ProgressController(margin_ratio=0.15)
    assert controller.decide(recognizer.detect(frame(yellow=75), 1.0)) == Direction.RIGHT
    assert controller.decide(recognizer.detect(frame(yellow=120), 1.1)) == Direction.RELEASE
    assert controller.decide(recognizer.detect(frame(yellow=165), 1.2)) == Direction.LEFT


def test_thirty_fast_frames_never_reverse_direction_definition():
    recognizer, controller = ProgressRecognizer(), ProgressController()
    decisions = [controller.decide(recognizer.detect(frame((40+i*3, 140+i*3), 90+i), i/30)) for i in range(30)]
    assert set(decisions) <= {Direction.LEFT, Direction.RIGHT, Direction.RELEASE}
```

- [ ] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python.exe -m pytest try/tests/test_progress.py -q`

预期：收集失败，提示缺少 `ProgressRecognizer`。

- [ ] **步骤 3：实现颜色分割、连通区域和决策**

```python
# src/auto_fishing/vision/progress.py
import cv2
import numpy as np
from auto_fishing.model import Direction, ProgressObservation


class ProgressRecognizer:
    def detect(self, image: np.ndarray, timestamp: float) -> ProgressObservation | None:
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        green = cv2.inRange(hsv, np.array((70, 80, 100)), np.array((105, 255, 255)))
        yellow = cv2.inRange(hsv, np.array((18, 120, 150)), np.array((38, 255, 255)))
        green = cv2.morphologyEx(green, cv2.MORPH_CLOSE, np.ones((3, 9), np.uint8))
        green_boxes = [cv2.boundingRect(c) for c in cv2.findContours(green, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]]
        yellow_boxes = [cv2.boundingRect(c) for c in cv2.findContours(yellow, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]]
        green_boxes = [b for b in green_boxes if b[2] >= image.shape[1] * 0.12 and b[3] >= 4]
        yellow_boxes = [b for b in yellow_boxes if b[3] >= b[2] * 2 and b[3] >= image.shape[0] * 0.12]
        if not green_boxes or not yellow_boxes: return None
        gx, gy, gw, gh = max(green_boxes, key=lambda b: b[2] * b[3])
        candidates = [b for b in yellow_boxes if gy - gh <= b[1] <= gy + gh]
        if not candidates: return None
        yx, yy, yw, yh = max(candidates, key=lambda b: b[2] * b[3])
        width = float(image.shape[1])
        confidence = min(1.0, (gw / width) * 3 + (yh / image.shape[0])) / 2
        return ProgressObservation(gx/width, (gx+gw)/width, (yx+yw/2)/width, confidence, timestamp)


class ProgressController:
    def __init__(self, margin_ratio: float = 0.15) -> None: self.margin_ratio = margin_ratio

    def decide(self, observation: ProgressObservation | None) -> Direction:
        if observation is None: return Direction.RELEASE
        width = observation.green_right - observation.green_left
        safe_left = observation.green_left + width * self.margin_ratio
        safe_right = observation.green_right - width * self.margin_ratio
        if observation.yellow_x < safe_left: return Direction.RIGHT
        if observation.yellow_x > safe_right: return Direction.LEFT
        return Direction.RELEASE
```

- [ ] **步骤 4：运行进度条测试**

运行：`.\.venv\Scripts\python.exe -m pytest try/tests/test_progress.py -q`

预期：`3 passed`。若阈值与合成 BGR 色不匹配，只允许调整测试色或 HSV 阈值并记录实际 HSV；不得改成固定像素位置。

- [ ] **步骤 5：提交任务 5**

```powershell
git add src/auto_fishing/vision/progress.py try/tests/test_progress.py
git commit -m "feat: track fishing progress at thirty fps"
```

---

### 任务 6：上钩、结算和就绪画面识别

**文件：**

- 创建：`src/auto_fishing/vision/scenes.py`
- 创建：`try/tests/test_scenes.py`

**接口：**

- 产出：`BiteDetector.set_baseline(roi)`、`detect(roi) -> bool`，连续帧计数由检测器内部维护。
- 产出：`SceneRecognizer.observe(client_frame, timestamp) -> SceneObservation`。
- 固定初始 ROI：顶部 `(0.24,0.00,0.76,0.15)`、右下 `(0.84,0.68,1.00,1.00)`、结算 `(0.25,0.05,0.75,0.95)`。

- [ ] **步骤 1：写交叉特征和连续帧失败测试**

```python
# try/tests/test_scenes.py
import cv2
import numpy as np
from auto_fishing.vision.scenes import BiteDetector, SceneRecognizer


def test_bite_requires_two_changed_frames_and_two_features():
    base = np.zeros((100, 100, 3), dtype=np.uint8)
    changed = base.copy()
    cv2.circle(changed, (50, 50), 35, (255, 100, 0), 8)
    cv2.circle(changed, (50, 50), 15, (255, 255, 255), 5)
    detector = BiteDetector(); detector.set_baseline(base)
    assert detector.detect(changed) is False
    assert detector.detect(changed) is True


def test_single_white_flash_does_not_trigger_bite():
    base = np.zeros((100, 100, 3), dtype=np.uint8)
    flash = base.copy(); flash[20:80, 20:80] = 255
    detector = BiteDetector(); detector.set_baseline(base)
    assert detector.detect(flash) is False
    assert detector.detect(flash) is False


def test_result_requires_dark_overlay_and_blue_card_without_progress():
    frame = np.full((720, 1280, 3), 25, dtype=np.uint8)
    cv2.circle(frame, (640, 360), 150, (220, 120, 20), -1)
    recognizer = SceneRecognizer()
    obs = recognizer.observe(frame, 1.0)
    assert obs.result is True
    assert obs.progress is None
```

- [ ] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python.exe -m pytest try/tests/test_scenes.py -q`

预期：收集失败，提示缺少 `BiteDetector`。

- [ ] **步骤 3：实现场景识别器**

`scenes.py` 必须实现以下算法，不使用 OCR 或带红色标注截图模板：

```python
class BiteDetector:
    def __init__(self): self.baseline = None; self.consecutive = 0
    def set_baseline(self, roi): self.baseline = self._signature(roi); self.consecutive = 0
    def _signature(self, roi):
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        blue = cv2.inRange(hsv, (90, 100, 80), (135, 255, 255)).mean() / 255
        white = cv2.inRange(hsv, (0, 0, 190), (179, 80, 255)).mean() / 255
        edges = cv2.Canny(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY), 80, 160).mean() / 255
        return blue, white, edges
    def detect(self, roi):
        if self.baseline is None: return False
        current = self._signature(roi)
        blue_changed = current[0] - self.baseline[0] > 0.03
        shape_changed = current[1] - self.baseline[1] > 0.03 or current[2] - self.baseline[2] > 0.02
        changed = blue_changed and shape_changed
        self.consecutive = self.consecutive + 1 if changed else 0
        return self.consecutive >= 2
```

`SceneRecognizer` 组合任务 5 的 `ProgressRecognizer`，按三块归一化 ROI 裁剪。结算判定为：进度条不存在、结算 ROI 暗像素比例大于 0.45、蓝色高饱和像素比例大于 0.03，并连续三帧成立。就绪判定为：右下 ROI 白色图标比例大于 0.01、蓝色上钩环比例低于 0.03、进度条不存在，并连续三帧成立。`observe()` 返回 `SceneObservation`；另提供 `set_bite_baseline(client_frame)`。

- [ ] **步骤 4：运行场景测试并检查参考图只读性**

运行：

```powershell
.\.venv\Scripts\python.exe -m pytest try/tests/test_scenes.py -q
git status --short -- '流程截图'
```

预期：`3 passed`；第二条无输出，证明未修改用户参考图。

- [ ] **步骤 5：提交任务 6**

```powershell
git add src/auto_fishing/vision/scenes.py try/tests/test_scenes.py
git commit -m "feat: recognize fishing scene transitions"
```

---

### 任务 7：纯状态机、超时与成功计数

**文件：**

- 创建：`src/auto_fishing/automation/__init__.py`
- 创建：`src/auto_fishing/automation/state_machine.py`
- 创建：`try/tests/test_state_machine.py`

**接口：**

- 产出：`Event` 枚举和 `FishingStateMachine.start(target, now)`、`handle(event, now)`、`check_timeout(now)`。
- 产出：`snapshot(fps=0, error="") -> RuntimeSnapshot`。
- 成功计数只在 `DISMISS_RESULT` 收到 `READY_DETECTED` 后增加。

- [ ] **步骤 1：写完整单轮、暂停和超时失败测试**

```python
# try/tests/test_state_machine.py
from auto_fishing.automation.state_machine import Event, FishingStateMachine
from auto_fishing.model import FishingState


def test_one_round_counts_only_after_ready_returns():
    sm = FishingStateMachine(); sm.start(1, 0)
    for event, now in [(Event.CAST_SENT, .1), (Event.REEL_SENT, 2), (Event.BAR_DETECTED, 2.1), (Event.BAR_GONE, 4), (Event.RESULT_DETECTED, 4.1), (Event.RESULT_CLICKED, 4.2)]:
        sm.handle(event, now)
    assert sm.completed == 0
    sm.handle(Event.READY_DETECTED, 5)
    assert sm.completed == 1 and sm.state == FishingState.COMPLETE


def test_two_rounds_enter_inter_round_then_ready():
    sm = FishingStateMachine(); sm.start(2, 0)
    for event in [Event.CAST_SENT, Event.REEL_SENT, Event.BAR_DETECTED, Event.BAR_GONE, Event.RESULT_DETECTED, Event.RESULT_CLICKED, Event.READY_DETECTED]: sm.handle(event, 1)
    assert sm.state == FishingState.INTER_ROUND and sm.completed == 1
    sm.handle(Event.INTERVAL_ELAPSED, 2)
    assert sm.state == FishingState.READY


def test_wait_bar_times_out_after_eight_seconds():
    sm = FishingStateMachine(); sm.start(1, 0); sm.handle(Event.CAST_SENT, 0); sm.handle(Event.REEL_SENT, 1)
    assert sm.check_timeout(9.01) is True
    assert sm.state == FishingState.PAUSED


def test_pause_remembers_state_and_resume_requires_classification():
    sm = FishingStateMachine(); sm.start(1, 0); sm.handle(Event.CAST_SENT, 0); sm.handle(Event.REEL_SENT, 1); sm.handle(Event.BAR_DETECTED, 2)
    sm.pause("F8", 3)
    assert sm.state == FishingState.PAUSED
    sm.handle(Event.RESUME_CONTROL, 4)
    assert sm.state == FishingState.CONTROL
```

- [ ] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python.exe -m pytest try/tests/test_state_machine.py -q`

预期：收集失败，提示缺少 `FishingStateMachine`。

- [ ] **步骤 3：实现显式转换表和超时表**

```python
# src/auto_fishing/automation/state_machine.py（完整转换定义）
from enum import Enum, auto
from auto_fishing.model import FishingState, RuntimeSnapshot


class Event(Enum):
    CAST_SENT = auto(); REEL_SENT = auto(); BAR_DETECTED = auto(); BAR_GONE = auto()
    RESULT_DETECTED = auto(); RESULT_CLICKED = auto(); READY_DETECTED = auto()
    INTERVAL_ELAPSED = auto(); RESUME_CONTROL = auto(); RESUME_READY = auto()


TRANSITIONS = {
    (FishingState.READY, Event.CAST_SENT): FishingState.WAIT_BITE,
    (FishingState.WAIT_BITE, Event.REEL_SENT): FishingState.WAIT_BAR,
    (FishingState.WAIT_BAR, Event.BAR_DETECTED): FishingState.CONTROL,
    (FishingState.CONTROL, Event.BAR_GONE): FishingState.WAIT_RESULT,
    (FishingState.WAIT_RESULT, Event.RESULT_DETECTED): FishingState.DISMISS_RESULT,
    (FishingState.DISMISS_RESULT, Event.RESULT_CLICKED): FishingState.DISMISS_RESULT,
    (FishingState.INTER_ROUND, Event.INTERVAL_ELAPSED): FishingState.READY,
    (FishingState.PAUSED, Event.RESUME_CONTROL): FishingState.CONTROL,
    (FishingState.PAUSED, Event.RESUME_READY): FishingState.READY,
}
TIMEOUTS = {
    FishingState.READY: 3, FishingState.WAIT_BITE: 120, FishingState.WAIT_BAR: 8,
    FishingState.CONTROL: 120, FishingState.WAIT_RESULT: 10,
    FishingState.DISMISS_RESULT: 8, FishingState.INTER_ROUND: 1,
}
```

`FishingStateMachine` 保存 `state/target/completed/entered_at/pause_reason/paused_from/result_clicked`。`handle(READY_DETECTED)` 只在 `DISMISS_RESULT` 且 `result_clicked=True` 时增加计数；达到目标转 `COMPLETE`，否则转 `INTER_ROUND`。非法事件抛 `ValueError`。`check_timeout(now)` 在 `now-entered_at > TIMEOUTS[state]` 时调用 `pause(f"{state.value}超时", now)` 并返回 `True`。`start()` 校验 1～999 并将状态设为 `READY`。

- [ ] **步骤 4：运行状态机测试**

运行：`.\.venv\Scripts\python.exe -m pytest try/tests/test_state_machine.py -q`

预期：`4 passed`。

- [ ] **步骤 5：提交任务 7**

```powershell
git add src/auto_fishing/automation try/tests/test_state_machine.py
git commit -m "feat: add explicit fishing state machine"
```

---

### 任务 8：自动化引擎、最新帧看门狗与安全暂停

**文件：**

- 创建：`src/auto_fishing/automation/engine.py`
- 创建：`try/tests/test_engine.py`

**接口：**

- 产出：`AutomationEngine.bind(bound)`、`start(target)`、`pause(reason)`、`resume()`、`shutdown()`。
- 产出：`subscribe(callback: Callable[[RuntimeSnapshot], None])`。
- 依赖任务 2～7 的 `DiagnosticsStore/SafeInput/WindowService/DxcamFrameSource/SceneRecognizer/ProgressController/FishingStateMachine`。

- [ ] **步骤 1：写端到端假服务失败测试**

```python
# try/tests/test_engine.py
import numpy as np
from auto_fishing.automation.engine import AutomationEngine
from auto_fishing.model import FramePacket, ProgressObservation, SceneObservation, FishingState


class FakeInput:
    def __init__(self): self.events=[]
    def tap_f(self): self.events.append("F")
    def set_direction(self, d): self.events.append(d.value)
    def click(self, x, y): self.events.append(("click", x, y))
    def release_all(self): self.events.append("release")


def test_engine_drives_single_round_and_counts_success(fake_services):
    engine, input = fake_services()
    sequence = [
        SceneObservation(), SceneObservation(bite=True),
        SceneObservation(progress=ProgressObservation(.3,.7,.2,1,2)),
        SceneObservation(progress=ProgressObservation(.3,.7,.5,1,3)),
        SceneObservation(result=True), SceneObservation(result=True),
        SceneObservation(result=True), SceneObservation(result=True), SceneObservation(ready=True),
    ]
    engine.start_for_test(1)
    for index, observation in enumerate(sequence): engine.tick_for_test(observation, index + 1)
    assert engine.snapshot.state == FishingState.COMPLETE
    assert engine.snapshot.completed == 1
    assert input.events.count("F") == 2


def test_missing_progress_releases_immediately_and_pauses_at_six_frames(fake_services):
    engine, input = fake_services(initial_state=FishingState.CONTROL)
    for index in range(6): engine.tick_for_test(SceneObservation(), index / 30)
    assert "release" in input.events
    assert engine.snapshot.state == FishingState.PAUSED


def test_stale_frame_and_foreground_loss_pause(fake_services):
    engine, input = fake_services(initial_state=FishingState.CONTROL)
    engine.handle_frame_for_test(FramePacket(np.zeros((10,10,3), np.uint8), 1.0, 30), now=1.6, foreground=True)
    assert engine.snapshot.state == FishingState.PAUSED
    assert input.events[-1] == "release"
```

测试夹具 `fake_services` 在同一文件中创建确定性的假窗口、假截屏、假识别器、假诊断和假时钟，不启动真实线程或发送真实输入。

- [ ] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python.exe -m pytest try/tests/test_engine.py -q`

预期：收集失败，提示缺少 `AutomationEngine`。

- [ ] **步骤 3：实现状态驱动的单帧处理**

`AutomationEngine._process(observation, packet, now)` 必须按下表执行，并在每次动作后调用状态机事件：

```python
if state is READY: activate_game(); input.tap_f(); scenes.set_bite_baseline(frame); handle(CAST_SENT)
elif state is WAIT_BITE and observation.bite: input.tap_f(); handle(REEL_SENT)
elif state is WAIT_BAR and observation.progress: handle(BAR_DETECTED)
elif state is CONTROL:
    if observation.progress:
        missing = 0; input.set_direction(controller.decide(observation.progress))
    else:
        missing += 1; input.release_all()
        if missing >= 2: handle(BAR_GONE) if observation.result else None
        if missing >= 6 and not observation.result: pause("连续六帧未识别进度条")
elif state is WAIT_RESULT and observation.result: input.release_all(); handle(RESULT_DETECTED)
elif state is DISMISS_RESULT and not result_clicked:
    assert observation.result; input.click(client.left + round(client.width*.15), client.top + round(client.height*.55)); handle(RESULT_CLICKED)
elif state is DISMISS_RESULT and observation.ready: handle(READY_DETECTED)
elif state is INTER_ROUND and state_machine.check_interval(now): handle(INTERVAL_ELAPSED)
```

为避免“进度条正常消失”与“识别丢失”冲突，实现 `bar_missing_frames` 和 `result_candidate_frames`：有结算候选时两帧进度条消失进入 `WAIT_RESULT`；没有结算候选时第一帧释放、连续六帧暂停。不得在普通画面仅凭进度条消失点击。

- [ ] **步骤 4：实现工作线程、看门狗和统一暂停**

工作循环在守护线程中：开始时激活绑定窗口并启动对应 DXcam 输出；循环读取最新帧、裁剪游戏客户区、检查前台和客户区每 0.5 秒刷新、调用识别与 `_process`。以下条件统一调用 `_pause(code, detail, frame)`：

- 帧时间戳超过 0.2 秒：立即 `release_all()`；超过 0.5 秒：`E_STALE_FRAME`。
- 游戏不在前台、最小化、关闭、尺寸小于 960×540：`E_WINDOW`。
- `SendInput` 或识别器异常：分别 `E_INPUT`、`E_VISION`。
- 状态机超时：`E_TIMEOUT`。
- 用户按钮或 F8：`E_USER_PAUSE`，F8 不必保存截图。

`_pause` 首先设置停止输入事件，再 `release_all()`，再保存至多一次诊断，最后发布 `RuntimeSnapshot`。`shutdown()` 设置退出事件、停止截屏、释放输入、等待线程最多 2 秒；超时写日志但不得阻塞界面退出。

- [ ] **步骤 5：运行引擎与全套自动测试**

运行：

```powershell
.\.venv\Scripts\python.exe -m pytest try/tests/test_engine.py -q
.\.venv\Scripts\python.exe -m pytest try/tests -q
```

预期：引擎测试 `3 passed`，全套全部通过，无真实键盘或鼠标输入。

- [ ] **步骤 6：提交任务 8**

```powershell
git add src/auto_fishing/automation/engine.py try/tests/test_engine.py
git commit -m "feat: orchestrate safe fishing automation"
```

---

### 任务 9：置顶界面、三秒绑定、F8 接线与程序入口

**文件：**

- 创建：`src/auto_fishing/ui/__init__.py`
- 创建：`src/auto_fishing/ui/main_window.py`
- 创建：`src/auto_fishing/app.py`
- 创建：`src/auto_fishing/__main__.py`
- 创建：`try/tests/test_ui_smoke.py`

**接口：**

- 产出：`MainWindow(root, controller, settings_store)`。
- 产出：`Application.run()` 和 `python -m auto_fishing`。
- UI 只通过控制器调用 `bind/start/pause/resume/rebind/shutdown`，不导入 OpenCV 或 DXcam。

- [ ] **步骤 1：写 UI 冒烟和输入校验失败测试**

```python
# try/tests/test_ui_smoke.py
import tkinter as tk
from auto_fishing.storage.settings import AppSettings
from auto_fishing.ui.main_window import MainWindow


class FakeController:
    def __init__(self): self.calls=[]
    def bind_after_countdown(self, on_tick, on_done): self.calls.append("bind")
    def start(self, target): self.calls.append(("start", target))
    def pause(self, reason="按钮暂停"): self.calls.append("pause")
    def resume(self): self.calls.append("resume")
    def shutdown(self): self.calls.append("shutdown")
    def subscribe(self, callback): self.callback=callback


class FakeSettings:
    def load(self): return AppSettings()
    def save(self, settings): self.saved=settings


def test_window_is_topmost_and_validates_count():
    root = tk.Tk(); root.withdraw()
    controller = FakeController(); window = MainWindow(root, controller, FakeSettings())
    root.update_idletasks()
    assert root.attributes("-topmost") == 1
    window.count_var.set("0"); window.on_start()
    assert not controller.calls
    window.count_var.set("3"); window.on_start()
    assert controller.calls == [("start", 3)]
    window.close(); root.destroy()
```

- [ ] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python.exe -m pytest try/tests/test_ui_smoke.py -q`

预期：收集失败，提示缺少 `MainWindow`。

- [ ] **步骤 3：实现 320×240 主窗口**

`MainWindow` 使用 ttk，固定以下控件与行为：

```text
标题：异环自动钓鱼
绑定状态：未绑定 / 已绑定：<窗口标题>
数量：Spinbox(from_=1, to=999)
阶段：<RuntimeSnapshot.state.value>
进度：<completed>/<target>
帧率：<fps:.1f> FPS
最近错误：<error 或 无>
按钮：绑定游戏、开始、暂停/继续、重新绑定、退出
提示：F8 紧急暂停
```

根窗口设置 `320x240+window_x+window_y`、`attributes('-topmost', True)`、禁止缩小到 320×240。绑定按钮调用控制器的三秒异步倒计时；倒计时期间状态文字依次显示 3、2、1，结束时程序读取当前前台游戏窗口。收到运行快照时通过 `root.after(0, lambda: self.apply_snapshot(snapshot))` 更新控件。运行时锁定 Spinbox 和绑定按钮。关闭时保存窗口位置，调用 `controller.shutdown()` 后销毁窗口。

- [ ] **步骤 4：实现依赖装配和入口**

`Application` 按以下顺序装配：

```python
def run(self):
    window_service.enable_dpi_awareness()
    root = tkinter.Tk()
    data_dir = Path(os.environ["LOCALAPPDATA"]) / "异环自动钓鱼"
    diagnostics.cleanup()
    main_window = MainWindow(root, controller, settings)
    root.update_idletasks()
    window_service.exclude_from_capture(root.winfo_id())
    if not hotkey.start(lambda: engine.pause("F8 紧急暂停")):
        main_window.block_start("F8 注册失败，请关闭占用 F8 的程序")
    try: root.mainloop()
    finally: hotkey.stop(); engine.shutdown(); safe_input.release_all()
```

`__main__.py` 只执行 `from auto_fishing.app import Application; Application().run()`。控制器负责三秒绑定回调、引擎命令和 UI 订阅桥接，禁止 UI 直接访问工作线程对象。

- [ ] **步骤 5：运行 UI 和全套测试**

运行：

```powershell
.\.venv\Scripts\python.exe -m pytest try/tests/test_ui_smoke.py -q
.\.venv\Scripts\python.exe -m pytest try/tests -q
```

预期：UI 测试 `1 passed`，全套全部通过。随后运行 `.\.venv\Scripts\python.exe -m auto_fishing`，人工确认窗口出现、置顶、可拖动，关闭后命令退出；此烟雾检查不绑定游戏、不点击开始。

- [ ] **步骤 6：提交任务 9**

```powershell
git add src/auto_fishing try/tests/test_ui_smoke.py
git commit -m "feat: add always-on-top control window"
```

---

### 任务 10：单文件打包、离线验收、文档与分支收尾

**文件：**

- 创建：`packaging/app.manifest`
- 创建：`packaging/auto_fishing.spec`
- 创建：`scripts/build.ps1`
- 创建：`try/smoke_exe.ps1`
- 修改：`AGENTS.md`
- 修改：`doc/验收标准.md`
- 修改：`doc/进展记录/2026-7-10.md`，若跨日则改为当天文件

**接口：**

- 产出：`dist/异环自动钓鱼.exe`。
- 构建必须从干净虚拟环境可重复执行，且发布物不显示控制台窗口。

- [ ] **步骤 1：写构建与冒烟脚本**

```powershell
# scripts/build.ps1
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw '缺少 .venv，请先建立 Python 3.13 虚拟环境' }
& $Python -m pytest (Join-Path $Root 'try\tests') -q
if ($LASTEXITCODE -ne 0) { throw '自动测试失败，停止构建' }
& $Python -m PyInstaller --clean --noconfirm (Join-Path $Root 'packaging\auto_fishing.spec')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 构建失败' }
$Exe = Join-Path $Root 'dist\异环自动钓鱼.exe'
if (-not (Test-Path -LiteralPath $Exe)) { throw '未生成预期发布物' }
Get-FileHash -Algorithm SHA256 -LiteralPath $Exe
```

```powershell
# try/smoke_exe.ps1
$ErrorActionPreference = 'Stop'
$Exe = Resolve-Path "$PSScriptRoot\..\dist\异环自动钓鱼.exe"
$Process = Start-Process -FilePath $Exe -PassThru
try {
    Start-Sleep -Seconds 3
    if ($Process.HasExited) { throw "发布物提前退出，退出码 $($Process.ExitCode)" }
    if (-not $Process.Responding) { throw '发布物窗口无响应' }
} finally {
    if (-not $Process.HasExited) { Stop-Process -Id $Process.Id }
}
'SMOKE_OK'
```

- [ ] **步骤 2：实现清单和 PyInstaller 配置**

`app.manifest` 指定 `requestedExecutionLevel level="asInvoker"`，并声明 `dpiAwareness` 为 `PerMonitorV2`。`auto_fishing.spec` 使用 `collect_all('dxcam')` 收集其二进制和隐藏导入，入口为 `src/auto_fishing/__main__.py`，`pathex=['src']`，名称为 `异环自动钓鱼`，`console=False`、`upx=False`，并传入上述清单。不得使用 `--uac-admin`。

- [ ] **步骤 3：运行完整自动测试和构建**

运行：

```powershell
.\.venv\Scripts\python.exe -m pytest try/tests -q
powershell -ExecutionPolicy Bypass -File scripts/build.ps1
powershell -ExecutionPolicy Bypass -File try/smoke_exe.ps1
```

预期：全部 pytest 测试通过；构建输出 SHA256；冒烟输出 `SMOKE_OK`。记录测试数、哈希、可执行文件大小和命令时间。

- [ ] **步骤 4：执行结构和安全复核**

运行：

```powershell
rg -n "pyautogui|keybd_event|mouse_event|2560|1440|TODO|TBD" src try packaging scripts
git diff --check
git status --short
```

预期：源码中没有 `pyautogui`、弃用输入接口、固定 2560×1440 控制坐标、TODO 或 TBD；`git diff --check` 无输出。测试中出现分辨率参数属于预期，人工复核后记录。检查并报告是否存在重复状态机、临时阈值补丁或新增技术债。

- [ ] **步骤 5：更新长期文档与离线验收结果**

在 `AGENTS.md` 把项目状态更新为“离线自动测试与构建完成，等待实机验收”，并把实际依赖、命令和目录与实现对齐。在 `doc/验收标准.md` 逐项填写自动测试、构建、冒烟的命令与结果；所有真实游戏项目保持“人工确认”，禁止写成通过。进展记录写明精确时间段、文件清单、错误及解决方案、发布物路径和外部 `%LOCALAPPDATA%\异环自动钓鱼\` 行为。

- [ ] **步骤 6：提交构建与文档**

```powershell
git add packaging scripts try/smoke_exe.ps1 AGENTS.md doc
git commit -m "build: package auto-fishing executable"
```

`dist/` 保持 Git 忽略，不提交可执行文件；在会话中提供本地绝对路径和 SHA256。

- [ ] **步骤 7：执行真实游戏人工验收**

按 `doc/验收标准.md` 顺序，在《异环》中依次验证：

1. 窗口模式绑定并完成 1 轮。
2. 无边框模式绑定并完成 2 轮。
3. 全屏模式确认自动化与 F8；若独占全屏遮挡置顶窗，按已确认限制记录。
4. 快速变化进度条下连续 5 轮，记录实际帧率、成功数和每次暂停原因。
5. 控制阶段按 F8，确认 A/D 立即释放；再验证切出前台和关闭游戏的暂停。
6. 生成超过 20 组及超过 7 天的隔离诊断样本，运行清理测试，确认 `流程截图` 未变化。

真实识别失败时先保存诊断并暂停，不允许用固定延时绕过；调整阈值后必须增加回放测试并重新执行步骤 3。

- [ ] **步骤 8：仅在全部验收通过后合并主分支并删除任务分支**

```powershell
git status --short --branch
git switch main
git merge --no-ff root/feature-auto-fishing -m "merge: deliver auto-fishing executable"
git branch -d root/feature-auto-fishing
git status --short --branch
```

预期：最终位于 `main`，工作区干净，任务分支已删除。若真实游戏人工验收尚未完成或出现失败，停止在任务分支，明确报告原因，不合并、不删除分支。当前没有 GitHub 地址；以后发现远程地址时再询问用户是否推送。

---

## 实施完成检查

- [ ] 所有自动测试通过，测试数和命令已记录。
- [ ] `dist/异环自动钓鱼.exe` 构建成功并通过启动冒烟。
- [ ] 30 帧/秒控制只使用最新帧，A/D 不同时按住。
- [ ] F8、异常、退出均释放全部输入。
- [ ] 诊断清理不越出 `%LOCALAPPDATA%\异环自动钓鱼\diagnostics`。
- [ ] 真实游戏窗口、无边框和全屏结果已逐项记录。
- [ ] `AGENTS.md`、验收标准和进展记录与实现一致。
- [ ] 没有重复实现、固定分辨率补丁或未记录技术债。
- [ ] 验收通过后已合并 `main` 并删除任务分支。

## 规格覆盖自查

| 规格要求 | 实施任务 |
| --- | --- |
| 单文件置顶窗口、1～999 次数、三秒绑定 | 任务 9、10 |
| 多分辨率、DPI、窗口/无边框/全屏 | 任务 1、4、10 的实机验收 |
| 30 帧最新帧与快速绿色/黄色区域 | 任务 4、5、8 |
| 两次 F、A/D 闭环、结算点击、成功计数 | 任务 3、5、6、7、8 |
| F8、失前台、超时、旧帧和输入失败保护 | 任务 3、4、7、8、9 |
| 正常帧不落盘、7 天和 20 组清理 | 任务 2、8 |
| 自动测试、构建、实机人工验收 | 任务 1～10，集中记录于任务 10 |
| 项目规则、验收标准、进展记录、主分支清理 | 任务 10 |
