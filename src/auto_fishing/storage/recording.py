from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from auto_fishing.model import FishingState, RuntimeSnapshot, SceneObservation


def thumbnail(frame: np.ndarray, max_edge: int) -> np.ndarray:
    if max_edge < 1:
        raise ValueError("max_edge 必须至少为 1")
    height, width = frame.shape[:2]
    largest_edge = max(height, width)
    if largest_edge <= max_edge:
        return np.ascontiguousarray(frame).copy()
    scale = max_edge / largest_edge
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return np.ascontiguousarray(
        cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
    ).copy()


def encode_jpeg(frame: np.ndarray, *, max_edge: int, quality: int) -> bytes:
    encoded, payload = cv2.imencode(
        ".jpg",
        thumbnail(frame, max_edge),
        [cv2.IMWRITE_JPEG_QUALITY, quality],
    )
    if not encoded:
        raise OSError("JPEG 编码失败")
    return payload.tobytes()


def frame_event_fields(
    *,
    observation: SceneObservation,
    state_before: FishingState,
    snapshot: RuntimeSnapshot,
    frame_timestamp: float,
    now_monotonic: float,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
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
        "progress_scanlines": observation.progress_scanlines,
        "progress_candidates": observation.progress_candidates,
        "progress_rejection": observation.progress_rejection,
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
    return fields
