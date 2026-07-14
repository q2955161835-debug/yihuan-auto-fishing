from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import threading
import time
from typing import Any

import numpy as np

from auto_fishing.model import FishingState, RuntimeSnapshot, SceneObservation
from auto_fishing.vision.geometry import crop_normalized
from auto_fishing.vision.regions import TOP_ROI

from .recording import encode_jpeg, encode_png, frame_event_fields


@dataclass(frozen=True)
class BufferedDiagnosticFrame:
    name: str
    monotonic: float
    jpeg: bytes


@dataclass(frozen=True)
class BufferedProgressFrame:
    name: str
    monotonic: float
    png: bytes


@dataclass(frozen=True)
class DiagnosticSnapshot:
    events: tuple[dict[str, Any], ...]
    progress_traces: tuple[dict[str, Any], ...]
    frames: tuple[BufferedDiagnosticFrame, ...]
    progress_frames: tuple[BufferedProgressFrame, ...]
    dropped_items: int
    drop_counts: dict[str, int]
    captured_monotonic: float


class MemoryDiagnosticRecorder:
    _WINDOW_SECONDS = 30.0
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
        self._progress_traces: deque[dict[str, Any]] = deque()
        self._frames: deque[BufferedDiagnosticFrame] = deque()
        self._progress_frames: deque[BufferedProgressFrame] = deque()
        self._lock = threading.RLock()
        self._event_sequence = 0
        self._frame_sequence = 0
        self._last_context_sample = float("-inf")
        self._last_progress_sample = float("-inf")
        self._drop_counts = {
            "context_frames": 0,
            "progress_frames": 0,
            "progress_traces": 0,
        }

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
            self._frame_sequence += 1
            index = self._frame_sequence
        fields["frame_index"] = index
        self._append_event("frame.processed", fields, now_monotonic)

        diagnostics = observation.progress_diagnostics
        if (
            state_before in {FishingState.WAIT_BAR, FishingState.CONTROL}
            and diagnostics is not None
        ):
            try:
                progress = asdict(diagnostics)
                trace = {
                    "monotonic": now_monotonic,
                    "frame_index": index,
                    "frame_timestamp": frame_timestamp,
                    "state_before": state_before.value,
                    "progress": progress,
                }
            except Exception:
                with self._lock:
                    self._drop_counts["progress_traces"] += 1
            else:
                with self._lock:
                    self._progress_traces.append(trace)
                    if diagnostics.truncated:
                        self._drop_counts["progress_traces"] += 1
                    self._prune_locked(now_monotonic)

        with self._lock:
            should_sample_context = (
                now_monotonic + 1e-9
                >= self._last_context_sample + self._FRAME_INTERVAL
            )
        if should_sample_context:
            try:
                jpeg = encode_jpeg(frame, max_edge=480, quality=50)
            except Exception:
                with self._lock:
                    self._drop_counts["context_frames"] += 1
            else:
                with self._lock:
                    self._last_context_sample = now_monotonic
                    self._frames.append(
                        BufferedDiagnosticFrame(
                            name=f"{index:08d}.jpg",
                            monotonic=now_monotonic,
                            jpeg=jpeg,
                        )
                    )
                    self._prune_locked(now_monotonic)

        progress_state = state_before in {
            FishingState.WAIT_BAR,
            FishingState.CONTROL,
        }
        with self._lock:
            should_sample_progress = (
                progress_state
                and diagnostics is not None
                and now_monotonic + 1e-9
                >= self._last_progress_sample + self._FRAME_INTERVAL
            )
        if not should_sample_progress:
            return index
        try:
            top = crop_normalized(frame, TOP_ROI)
            height = top.shape[0]
            band = top[round(height * 0.40) : round(height * 0.52)]
            png = encode_png(band, compression=3)
        except Exception:
            with self._lock:
                self._drop_counts["progress_frames"] += 1
            return index
        with self._lock:
            self._last_progress_sample = now_monotonic
            self._progress_frames.append(
                BufferedProgressFrame(
                    name=f"{index:08d}.png",
                    monotonic=now_monotonic,
                    png=png,
                )
            )
            self._prune_locked(now_monotonic)
        return index

    def snapshot(self) -> DiagnosticSnapshot:
        captured_monotonic = self._clock()
        with self._lock:
            self._prune_locked(captured_monotonic)
            drop_counts = dict(self._drop_counts)
            return DiagnosticSnapshot(
                events=tuple(deepcopy(event) for event in self._events),
                progress_traces=tuple(
                    deepcopy(trace) for trace in self._progress_traces
                ),
                frames=tuple(self._frames),
                progress_frames=tuple(self._progress_frames),
                dropped_items=sum(drop_counts.values()),
                drop_counts=drop_counts,
                captured_monotonic=captured_monotonic,
            )

    def raise_if_failed(self) -> None:
        return None

    def cleanup(self) -> None:
        return None

    def close(self) -> None:
        with self._lock:
            self._events.clear()
            self._progress_traces.clear()
            self._frames.clear()
            self._progress_frames.clear()

    def _append_event(
        self,
        name: str,
        fields: Mapping[str, Any],
        monotonic: float,
    ) -> None:
        with self._lock:
            self._event_sequence += 1
            record = {
                "timestamp_utc": self._aware_now().isoformat(),
                "monotonic": monotonic,
                "event": name,
                **fields,
                "sequence": self._event_sequence,
            }
            self._events.append(record)
            self._prune_locked(monotonic)

    def _prune_locked(self, monotonic: float) -> None:
        cutoff = monotonic - self._WINDOW_SECONDS
        while (
            self._events
            and self._events[0]["monotonic"] < cutoff - 1e-9
        ):
            self._events.popleft()
        while (
            self._progress_traces
            and self._progress_traces[0]["monotonic"] < cutoff - 1e-9
        ):
            self._progress_traces.popleft()
        while self._frames and self._frames[0].monotonic < cutoff - 1e-9:
            self._frames.popleft()
        while (
            self._progress_frames
            and self._progress_frames[0].monotonic < cutoff - 1e-9
        ):
            self._progress_frames.popleft()

    def _aware_now(self) -> datetime:
        now = self._now()
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)
