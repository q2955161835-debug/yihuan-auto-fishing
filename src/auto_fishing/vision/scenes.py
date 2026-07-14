from __future__ import annotations

import cv2
import numpy as np

from auto_fishing.model import NormalizedRect, Rect, SceneObservation
from auto_fishing.vision.geometry import crop_normalized
from auto_fishing.vision.progress import ProgressRecognizer
from auto_fishing.vision.regions import (
    BITE_ROI,
    READY_ROI,
    REEL_PROMPT_ROI,
    RESULT_CENTER_ROI,
    RESULT_HEADER_ROI,
    RESULT_ROI,
    TOP_ROI,
)



class BiteDetector:
    def __init__(self) -> None:
        self.baseline: tuple[float, float, float] | None = None
        self.consecutive = 0
        self.frames_since_baseline = 0

    def set_baseline(self, roi: np.ndarray) -> None:
        self.baseline = self._signature(roi)
        self.consecutive = 0
        self.frames_since_baseline = 0

    def detect(self, roi: np.ndarray) -> bool:
        if self.baseline is None:
            return False

        self.frames_since_baseline += 1
        blue, white, edges = self._signature(roi)
        if self.frames_since_baseline <= 45:
            self.baseline = (
                min(self.baseline[0], blue),
                self.baseline[1],
                self.baseline[2],
            )
            self.consecutive = 0
            return False

        blue_changed = blue - self.baseline[0] > 0.03
        shape_changed = (
            white - self.baseline[1] > 0.03
            or edges - self.baseline[2] > 0.02
        )
        changed = blue_changed and shape_changed
        self.consecutive = self.consecutive + 1 if changed else 0
        return self.consecutive >= 2

    @staticmethod
    def _signature(roi: np.ndarray) -> tuple[float, float, float]:
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        blue = cv2.inRange(hsv, (90, 100, 80), (135, 255, 255)).mean() / 255
        white = cv2.inRange(hsv, (0, 0, 190), (179, 80, 255)).mean() / 255
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 80, 160).mean() / 255
        return float(blue), float(white), float(edges)


class SceneRecognizer:
    def __init__(self) -> None:
        self.bite_detector = BiteDetector()
        self.progress_recognizer = ProgressRecognizer()
        self.result_consecutive = 0
        self.reel_prompt_consecutive = 0
        self.ready_consecutive = 0

    def set_bite_baseline(self, client_frame: np.ndarray) -> None:
        self.bite_detector.set_baseline(crop_normalized(client_frame, BITE_ROI))

    def reset_progress_tracking(self) -> None:
        self.progress_recognizer.reset()

    def observe(
        self,
        client_frame: np.ndarray,
        timestamp: float,
        *,
        occlusion: Rect | None = None,
    ) -> SceneObservation:
        top = crop_normalized(client_frame, TOP_ROI)
        bite_roi = crop_normalized(client_frame, BITE_ROI)
        reel_prompt_roi = crop_normalized(client_frame, REEL_PROMPT_ROI)
        ready_roi = crop_normalized(client_frame, READY_ROI)
        result_center = crop_normalized(client_frame, RESULT_CENTER_ROI)
        result_header = crop_normalized(client_frame, RESULT_HEADER_ROI)
        result_valid = _valid_mask(
            client_frame,
            result_center,
            RESULT_CENTER_ROI,
            occlusion,
        )
        result_header_valid = _valid_mask(
            client_frame,
            result_header,
            RESULT_HEADER_ROI,
            occlusion,
        )

        progress_result = self.progress_recognizer.analyze(top, timestamp)
        progress = progress_result.observation
        bite = self.bite_detector.detect(bite_roi)
        result_center_candidate = (
            progress is None
            and _blue_ratio(result_center, result_valid) > 0.40
            and _white_ratio(result_center, result_valid) > 0.05
            and _dark_ratio(result_center, result_valid) < 0.60
        )
        result_header_candidate = (
            _magenta_ratio(result_header, result_header_valid) > 0.08
            and _white_ratio(result_header, result_header_valid) > 0.008
            and 0.30
            <= _dark_ratio(result_header, result_header_valid)
            <= 0.80
        )
        result_candidate = result_center_candidate and result_header_candidate
        reel_prompt_candidate = (
            _white_ratio(reel_prompt_roi) > 0.02
            and _dark_ratio(reel_prompt_roi) > 0.60
            and _blue_ratio(reel_prompt_roi) < 0.01
        )
        ready_candidate = (
            progress is None
            and _white_ratio(ready_roi) > 0.01
            and _blue_ratio(ready_roi) < 0.03
            and not result_candidate
        )

        self.result_consecutive = (
            self.result_consecutive + 1 if result_candidate else 0
        )
        self.ready_consecutive = (
            self.ready_consecutive + 1 if ready_candidate else 0
        )
        self.reel_prompt_consecutive = (
            self.reel_prompt_consecutive + 1 if reel_prompt_candidate else 0
        )
        result = self.result_consecutive >= 3
        reel_prompt = self.reel_prompt_consecutive >= 2
        ready = self.ready_consecutive >= 3 and not result

        return SceneObservation(
            bite=bite,
            reel_prompt=reel_prompt,
            result=result,
            result_candidate=result_candidate,
            ready=ready,
            progress=progress,
            progress_scanlines=progress_result.valid_scanlines,
            progress_candidates=progress_result.candidate_count,
            progress_rejection=progress_result.rejection_reason,
            progress_diagnostics=progress_result.diagnostics,
        )


def _blue_ratio(
    image: np.ndarray,
    valid: np.ndarray | None = None,
) -> float:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (90, 100, 80), (135, 255, 255)) > 0
    return _masked_mean(mask, valid)


def _white_ratio(
    image: np.ndarray,
    valid: np.ndarray | None = None,
) -> float:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 0, 190), (179, 80, 255)) > 0
    return _masked_mean(mask, valid)


def _magenta_ratio(
    image: np.ndarray,
    valid: np.ndarray | None = None,
) -> float:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (135, 80, 80), (175, 255, 255)) > 0
    return _masked_mean(mask, valid)


def _dark_ratio(
    image: np.ndarray,
    valid: np.ndarray | None = None,
) -> float:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return _masked_mean(gray < 60, valid)


def _masked_mean(values: np.ndarray, valid: np.ndarray | None) -> float:
    if valid is None:
        return float(values.mean())
    return float(values[valid].mean())


def _valid_mask(
    client_frame: np.ndarray,
    target_image: np.ndarray,
    target_roi: NormalizedRect,
    occlusion: Rect | None,
) -> np.ndarray | None:
    if occlusion is None:
        return None
    height, width = client_frame.shape[:2]
    target_rect = target_roi.to_pixels(Rect(0, 0, width, height))
    left = max(target_rect.left, occlusion.left)
    top = max(target_rect.top, occlusion.top)
    right = min(target_rect.right, occlusion.right)
    bottom = min(target_rect.bottom, occlusion.bottom)
    valid = np.ones(target_image.shape[:2], dtype=bool)
    if left < right and top < bottom:
        valid[
            top - target_rect.top : bottom - target_rect.top,
            left - target_rect.left : right - target_rect.left,
        ] = False
    if valid.mean() < 0.20:
        raise ValueError("结算识别有效像素不足")
    return valid
