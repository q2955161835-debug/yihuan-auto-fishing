from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

import dxcam

from auto_fishing.model import FramePacket


class DxcamFrameSource:
    """Own one DXcam instance and expose only the newest available frame."""

    def __init__(
        self,
        camera_factory: Callable[[int, int], Any] | None = None,
    ) -> None:
        self.camera_factory = camera_factory or (
            lambda device_index, output_index: dxcam.create(
                device_idx=device_index,
                output_idx=output_index,
                output_color="BGR",
                processor_backend="cv2",
            )
        )
        self.camera: Any | None = None
        self.timestamps: deque[float] = deque(maxlen=31)
        self._last_packet: FramePacket | None = None

    def start(self, device_index: int, output_index: int) -> None:
        self.stop()
        camera = self.camera_factory(device_index, output_index)
        self.camera = camera
        try:
            camera.start(target_fps=30)
        except BaseException:
            self.camera = None
            try:
                camera.release()
            finally:
                self.timestamps.clear()
                self._last_packet = None
            raise

    def latest(self) -> FramePacket:
        if self.camera is None:
            raise RuntimeError("截屏尚未启动")

        result = self.camera.get_latest_frame(with_timestamp=True)
        if result is None:
            if self._last_packet is None:
                raise RuntimeError("暂无可用截屏帧")
            return self._last_packet

        frame, timestamp = result
        timestamp = float(timestamp)
        self.timestamps.append(timestamp)
        fps = 0.0
        if len(self.timestamps) > 1 and self.timestamps[-1] > self.timestamps[0]:
            fps = (len(self.timestamps) - 1) / (
                self.timestamps[-1] - self.timestamps[0]
            )
        self._last_packet = FramePacket(frame=frame, timestamp=timestamp, fps=fps)
        return self._last_packet

    def stop(self) -> None:
        camera = self.camera
        if camera is None:
            return

        self.camera = None
        try:
            try:
                camera.stop()
            finally:
                camera.release()
        finally:
            self.timestamps.clear()
            self._last_packet = None
