from __future__ import annotations

from datetime import datetime, timezone

import cv2
import numpy as np

from auto_fishing.model import (
    FishingState,
    RuntimeSnapshot,
    SceneObservation,
)
from auto_fishing.storage.memory_diagnostics import MemoryDiagnosticRecorder


def test_memory_recorder_keeps_ten_seconds_and_samples_ten_fps(
    tmp_path,
) -> None:
    clock = [0.0]
    recorder = MemoryDiagnosticRecorder(
        clock=lambda: clock[0],
        now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    recorder.start()

    for index in range(121):
        clock[0] = index / 10
        recorder.event("tick", index=index)
        recorder.record_frame(
            np.zeros((720, 1280, 3), dtype=np.uint8),
            observation=SceneObservation(),
            state_before=FishingState.CONTROL,
            snapshot=RuntimeSnapshot(FishingState.CONTROL, 0, 1, 30.0),
            frame_timestamp=clock[0],
            now_monotonic=clock[0],
        )

    snapshot = recorder.snapshot()
    assert snapshot.events[0]["monotonic"] >= 2.0
    assert len(snapshot.frames) == 101
    decoded = cv2.imdecode(
        np.frombuffer(snapshot.frames[-1].jpeg, dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    assert decoded is not None
    assert max(decoded.shape[:2]) == 480
    assert list(tmp_path.iterdir()) == []
