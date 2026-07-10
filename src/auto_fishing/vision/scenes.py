from __future__ import annotations

import cv2
import numpy as np

from auto_fishing.model import NormalizedRect, SceneObservation
from auto_fishing.vision.geometry import crop_normalized
from auto_fishing.vision.progress import ProgressRecognizer


TOP_ROI = NormalizedRect(0.24, 0.00, 0.76, 0.15)
READY_ROI = NormalizedRect(0.84, 0.68, 1.00, 1.00)
RESULT_ROI = NormalizedRect(0.25, 0.05, 0.75, 0.95)


class BiteDetector:
    def __init__(self) -> None:
        self.baseline: tuple[float, float, float] | None = None
        self.consecutive = 0

    def set_baseline(self, roi: np.ndarray) -> None:
        self.baseline = self._signature(roi)
        self.consecutive = 0

    def detect(self, roi: np.ndarray) -> bool:
        if self.baseline is None:
            return False

        current = self._signature(roi)
        blue_changed = current[0] - self.baseline[0] > 0.03
        shape_changed = (
            current[1] - self.baseline[1] > 0.03
            or current[2] - self.baseline[2] > 0.02
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
        self.ready_consecutive = 0

    def set_bite_baseline(self, client_frame: np.ndarray) -> None:
        self.bite_detector.set_baseline(crop_normalized(client_frame, READY_ROI))

    def observe(self, client_frame: np.ndarray, timestamp: float) -> SceneObservation:
        top = crop_normalized(client_frame, TOP_ROI)
        ready_roi = crop_normalized(client_frame, READY_ROI)
        result_roi = crop_normalized(client_frame, RESULT_ROI)

        progress = self.progress_recognizer.detect(top, timestamp)
        bite = self.bite_detector.detect(ready_roi)
        result_candidate = (
            progress is None
            and _dark_ratio(result_roi) > 0.45
            and _blue_ratio(result_roi) > 0.03
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
        result = self.result_consecutive >= 3
        ready = self.ready_consecutive >= 3 and not result

        return SceneObservation(
            bite=bite,
            result=result,
            result_candidate=result_candidate,
            ready=ready,
            progress=progress,
        )


def _blue_ratio(image: np.ndarray) -> float:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    return float(cv2.inRange(hsv, (90, 100, 80), (135, 255, 255)).mean() / 255)


def _white_ratio(image: np.ndarray) -> float:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    return float(cv2.inRange(hsv, (0, 0, 190), (179, 80, 255)).mean() / 255)


def _dark_ratio(image: np.ndarray) -> float:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return float((gray < 60).mean())
