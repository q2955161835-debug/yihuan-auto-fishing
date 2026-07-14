from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import time
from typing import Any

import numpy as np

from auto_fishing.model import FishingState, RuntimeSnapshot, SceneObservation

from .recording import encode_jpeg, frame_event_fields


@dataclass(frozen=True)
class BufferedDiagnosticFrame:
    name: str
    monotonic: float
    jpeg: bytes


@dataclass(frozen=True)
class DiagnosticSnapshot:
    events: tuple[dict[str, Any], ...]
    frames: tuple[BufferedDiagnosticFrame, ...]
    dropped_items: int


class MemoryDiagnosticRecorder:
    _EVENT_WINDOW_SECONDS = 20.0
    _FRAME_WINDOW_SECONDS = 10.0
    _FRAME_INTERVAL = 0.1

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._clock = clock
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._events: deque[dict[str, Any]] = deque()
        self._frames: deque[BufferedDiagnosticFrame] = deque()
        self._lock = threading.RLock()
        self._sequence = 0
        self._last_frame_sample = float("-inf")
        self._dropped_items = 0

    def start(self) -> None:
        return None

    def event(self, name: str, **fields: Any) -> None:
        self._append_event(name, fields, self._clock())

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
        fields = frame_event_fields(
            observation=observation,
            state_before=state_before,
            snapshot=snapshot,
            frame_timestamp=frame_timestamp,
            now_monotonic=now_monotonic,
        )
        with self._lock:
            self._sequence += 1
            index = self._sequence
        fields["frame_index"] = index
        self._append_event("frame.processed", fields, now_monotonic)

        with self._lock:
            should_sample = (
                now_monotonic + 1e-9
                >= self._last_frame_sample + self._FRAME_INTERVAL
            )
        if not should_sample:
            return index
        try:
            jpeg = encode_jpeg(frame, max_edge=480, quality=50)
        except Exception:
            with self._lock:
                self._dropped_items += 1
            return index
        with self._lock:
            self._last_frame_sample = now_monotonic
            self._frames.append(
                BufferedDiagnosticFrame(
                    name=f"{index:08d}.jpg",
                    monotonic=now_monotonic,
                    jpeg=jpeg,
                )
            )
            self._prune_locked(now_monotonic)
        return index

    def snapshot(self) -> DiagnosticSnapshot:
        with self._lock:
            return DiagnosticSnapshot(
                events=tuple(dict(event) for event in self._events),
                frames=tuple(self._frames),
                dropped_items=self._dropped_items,
            )

    def raise_if_failed(self) -> None:
        return None

    def cleanup(self) -> None:
        return None

    def close(self) -> None:
        with self._lock:
            self._events.clear()
            self._frames.clear()

    def _append_event(
        self,
        name: str,
        fields: Mapping[str, Any],
        monotonic: float,
    ) -> None:
        record = {
            "timestamp_utc": self._aware_now().isoformat(),
            "monotonic": monotonic,
            "event": name,
            **fields,
        }
        with self._lock:
            self._events.append(record)
            self._prune_locked(monotonic)

    def _prune_locked(self, monotonic: float) -> None:
        event_cutoff = monotonic - self._EVENT_WINDOW_SECONDS
        while (
            self._events
            and self._events[0]["monotonic"] < event_cutoff - 1e-9
        ):
            self._events.popleft()
        frame_cutoff = monotonic - self._FRAME_WINDOW_SECONDS
        while self._frames and self._frames[0].monotonic < frame_cutoff - 1e-9:
            self._frames.popleft()

    def _aware_now(self) -> datetime:
        now = self._now()
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)
