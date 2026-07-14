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
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


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
    reel_prompt: bool = False
    result: bool = False
    result_candidate: bool = False
    ready: bool = False
    progress: ProgressObservation | None = None
    progress_scanlines: int = 0
    progress_candidates: int = 0
    progress_rejection: str = ""


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
