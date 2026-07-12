from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import shutil
import threading
import time
from typing import Any

import cv2
import numpy as np

from auto_fishing.model import FishingState, RuntimeSnapshot, SceneObservation


class RuntimeLogError(RuntimeError):
    """完整运行记录无法继续保存。"""


class _EventItem:
    def __init__(self, record: Mapping[str, Any]) -> None:
        self.record = dict(record)


class _FrameItem:
    def __init__(self, index: int, frame: np.ndarray, record: Mapping[str, Any]) -> None:
        self.index = index
        self.frame = frame
        self.record = dict(record)


class RuntimeLogStore:
    _STOP = object()
    _MAX_FRAME_EDGE = 480
    _JPEG_QUALITY = 50
    _KEEP_RUNS = 30

    def __init__(
        self,
        root: Path,
        *,
        queue_size: int = 300,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size 必须至少为 1")
        self.root = root.resolve()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._items: queue.Queue[_EventItem | _FrameItem | object] = queue.Queue(
            maxsize=queue_size
        )
        self._run_dir: Path | None = None
        self._events_path: Path | None = None
        self._writer: threading.Thread | None = None
        self._frame_index = 0
        self._failure: BaseException | None = None
        self._failure_lock = threading.Lock()
        self._closed = False

    def start(self) -> Path:
        if self._run_dir is not None:
            return self._run_dir
        self.root.mkdir(parents=True, exist_ok=True)
        run_dir = self._new_run_dir()
        frames_dir = run_dir / "frames"
        frames_dir.mkdir()
        events_path = run_dir / "events.jsonl"
        events_path.touch()
        self._run_dir = run_dir
        self._events_path = events_path
        self._writer = threading.Thread(
            target=self._write_loop,
            name="runtime-log-writer",
            daemon=True,
        )
        self._writer.start()
        return run_dir

    def event(self, name: str, **fields: Any) -> None:
        self._enqueue(_EventItem(self._event_record(name, fields)))

    def record_frame(
        self,
        frame: np.ndarray,
        *,
        observation: SceneObservation,
        state_before: FishingState,
        snapshot: RuntimeSnapshot,
        frame_timestamp: float,
        now_monotonic: float,
    ) -> int:
        self._ensure_started()
        self._frame_index += 1
        index = self._frame_index
        thumbnail = _thumbnail(frame, self._MAX_FRAME_EDGE)
        fields: dict[str, Any] = {
            "frame_index": index,
            "frame_timestamp": frame_timestamp,
            "frame_age": now_monotonic - frame_timestamp,
            "fps": snapshot.fps,
            "state_before": state_before.value,
            "state_after": snapshot.state.value,
            "completed": snapshot.completed,
            "target": snapshot.target,
            "error": snapshot.error,
            "bite": observation.bite,
            "reel_prompt": observation.reel_prompt,
            "ready": observation.ready,
            "result": observation.result,
            "result_candidate": observation.result_candidate,
        }
        if observation.progress is not None:
            fields.update(
                {
                    "green_left": observation.progress.green_left,
                    "green_right": observation.progress.green_right,
                    "yellow_x": observation.progress.yellow_x,
                    "confidence": observation.progress.confidence,
                }
            )
        self._enqueue(
            _FrameItem(
                index,
                thumbnail,
                self._event_record("frame.processed", fields, now_monotonic),
            )
        )
        return index

    def raise_if_failed(self) -> None:
        with self._failure_lock:
            failure = self._failure
        if failure is not None:
            raise RuntimeLogError(str(failure)) from failure

    def cleanup(self) -> None:
        if not self.root.exists():
            return
        runs: list[tuple[float, Path]] = []
        for path in self.root.iterdir():
            resolved = path.resolve()
            if not path.is_dir() or resolved.parent != self.root:
                continue
            runs.append((path.stat().st_mtime, path))
        for _, path in sorted(runs, key=lambda item: item[0], reverse=True)[
            self._KEEP_RUNS :
        ]:
            shutil.rmtree(path)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        writer = self._writer
        if writer is not None:
            try:
                self._items.put(self._STOP, timeout=5)
            except queue.Full as error:
                self._record_failure(RuntimeLogError("日志队列关闭超时"))
            writer.join(timeout=10)
            if writer.is_alive():
                self._record_failure(RuntimeLogError("日志写入线程关闭超时"))
        self.cleanup()

    def _new_run_dir(self) -> Path:
        now = self._aware_now()
        stem = now.strftime("run-%Y%m%dT%H%M%S%fZ")
        run_dir = self.root / stem
        suffix = 1
        while run_dir.exists():
            run_dir = self.root / f"{stem}-{suffix}"
            suffix += 1
        run_dir.mkdir()
        return run_dir

    def _event_record(
        self,
        name: str,
        fields: Mapping[str, Any],
        monotonic: float | None = None,
    ) -> dict[str, Any]:
        return {
            "timestamp_utc": self._aware_now().isoformat(),
            "monotonic": time.monotonic() if monotonic is None else monotonic,
            "event": name,
            **fields,
        }

    def _aware_now(self) -> datetime:
        now = self._now()
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    def _ensure_started(self) -> None:
        if self._run_dir is None:
            raise RuntimeLogError("运行日志尚未启动")
        if self._closed:
            raise RuntimeLogError("运行日志已关闭")

    def _enqueue(self, item: _EventItem | _FrameItem) -> None:
        try:
            self._ensure_started()
            self._items.put_nowait(item)
        except queue.Full:
            self._record_failure(RuntimeLogError("日志队列已满"))
        except BaseException as error:
            self._record_failure(error)

    def _record_failure(self, error: BaseException) -> None:
        with self._failure_lock:
            if self._failure is None:
                self._failure = error

    def _write_loop(self) -> None:
        while True:
            item = self._items.get()
            try:
                if item is self._STOP:
                    return
                self._write_item(item)
            except BaseException as error:
                self._record_failure(error)
            finally:
                self._items.task_done()

    def _write_item(self, item: _EventItem | _FrameItem | object) -> None:
        if isinstance(item, _FrameItem):
            encoded = cv2.imencode(
                ".jpg",
                item.frame,
                [cv2.IMWRITE_JPEG_QUALITY, self._JPEG_QUALITY],
            )
            if not encoded[0]:
                raise OSError("运行截图 JPEG 编码失败")
            assert self._run_dir is not None
            image_path = self._run_dir / "frames" / f"{item.index:08d}.jpg"
            image_path.write_bytes(encoded[1].tobytes())
            record = item.record
        elif isinstance(item, _EventItem):
            record = item.record
        else:
            raise TypeError("未知运行日志写入项")
        assert self._events_path is not None
        with self._events_path.open("a", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            output.write("\n")
            output.flush()


def _thumbnail(frame: np.ndarray, max_edge: int) -> np.ndarray:
    height, width = frame.shape[:2]
    largest_edge = max(height, width)
    if largest_edge <= max_edge:
        return np.ascontiguousarray(frame).copy()
    scale = max_edge / largest_edge
    size = (round(width * scale), round(height * scale))
    return np.ascontiguousarray(
        cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
    ).copy()
