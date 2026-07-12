from __future__ import annotations

import cv2
import numpy as np

from auto_fishing.model import Direction, ProgressObservation


class ProgressRecognizer:
    def detect(self, image: np.ndarray, timestamp: float) -> ProgressObservation | None:
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        green_mask = cv2.inRange(
            hsv,
            np.array((70, 80, 100), dtype=np.uint8),
            np.array((105, 255, 255), dtype=np.uint8),
        )
        yellow_mask = cv2.inRange(
            hsv,
            np.array((18, 120, 150), dtype=np.uint8),
            np.array((38, 255, 255), dtype=np.uint8),
        )
        green_mask = cv2.morphologyEx(
            green_mask,
            cv2.MORPH_CLOSE,
            np.ones((3, 9), dtype=np.uint8),
        )

        image_height, image_width = image.shape[:2]
        green_boxes = [
            box
            for box in _bounding_boxes(green_mask)
            if box[2] >= image_width * 0.12 and box[3] >= 4
        ]
        yellow_boxes = [
            box
            for box in _bounding_boxes(yellow_mask)
            if (
                box[3] >= box[2] * 2
                and box[3] >= image_height * 0.06
                and box[2] <= max(16, image_width * 0.04)
            )
        ]

        pairs = [
            (green_box, yellow_box)
            for green_box in green_boxes
            for yellow_box in yellow_boxes
            if _vertical_overlap(green_box, yellow_box) > 0
            and _marker_near_green_bar(green_box, yellow_box)
        ]
        if not pairs:
            return None

        green_box, yellow_box = max(
            pairs,
            key=lambda pair: (
                _vertical_overlap(*pair) / min(pair[0][3], pair[1][3]),
                pair[0][2] * pair[0][3],
            ),
        )
        green_x, _, green_width, _ = green_box
        yellow_x, _, yellow_width, yellow_height = yellow_box
        width = float(image_width)
        confidence = min(
            1.0,
            (green_width / width) * 3 + yellow_height / image_height,
        ) / 2
        return ProgressObservation(
            green_left=green_x / width,
            green_right=(green_x + green_width) / width,
            yellow_x=(yellow_x + yellow_width / 2) / width,
            confidence=confidence,
            timestamp=timestamp,
        )


class ProgressController:
    def __init__(self, center_tolerance_ratio: float = 0.10) -> None:
        if not 0 < center_tolerance_ratio < 0.5:
            raise ValueError("center_tolerance_ratio 必须在 0 与 0.5 之间")
        self.center_tolerance_ratio = center_tolerance_ratio

    def decide(self, observation: ProgressObservation | None) -> Direction:
        if observation is None:
            return Direction.RELEASE

        green_width = observation.green_right - observation.green_left
        green_center = (observation.green_left + observation.green_right) / 2
        tolerance = green_width * self.center_tolerance_ratio
        if observation.yellow_x < green_center - tolerance:
            return Direction.RIGHT
        if observation.yellow_x > green_center + tolerance:
            return Direction.LEFT
        return Direction.RELEASE


def _bounding_boxes(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    contours = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
    return [cv2.boundingRect(contour) for contour in contours]


def _vertical_overlap(
    first: tuple[int, int, int, int],
    second: tuple[int, int, int, int],
) -> int:
    first_top, first_bottom = first[1], first[1] + first[3]
    second_top, second_bottom = second[1], second[1] + second[3]
    return max(0, min(first_bottom, second_bottom) - max(first_top, second_top))


def _marker_near_green_bar(
    green_box: tuple[int, int, int, int],
    marker_box: tuple[int, int, int, int],
) -> bool:
    green_left, _, green_width, _ = green_box
    marker_left, _, marker_width, _ = marker_box
    marker_center = marker_left + marker_width / 2
    margin = max(12, green_width * 0.05)
    return (
        green_left - margin
        <= marker_center
        <= green_left + green_width + margin
    )
