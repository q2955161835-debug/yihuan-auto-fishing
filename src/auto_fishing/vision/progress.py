from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import cv2
import numpy as np

from auto_fishing.model import Direction, ProgressObservation


_SCAN_FRACTIONS = (0.40, 0.43, 0.46, 0.49, 0.52)
_SIDE_EXCLUSION = 0.08


@dataclass(frozen=True)
class _LineCandidate:
    green_left: int
    green_right: int
    yellow_center: float


@dataclass(frozen=True)
class ProgressScanResult:
    observation: ProgressObservation | None
    valid_scanlines: int = 0
    candidate_count: int = 0
    rejection_reason: str = ""


class ProgressRecognizer:
    def __init__(self) -> None:
        self._history: deque[ProgressObservation] = deque(maxlen=5)
        self._pending_jump: ProgressObservation | None = None

    def detect(
        self,
        image: np.ndarray,
        timestamp: float,
    ) -> ProgressObservation | None:
        return self.analyze(image, timestamp).observation

    def analyze(
        self,
        image: np.ndarray,
        timestamp: float,
    ) -> ProgressScanResult:
        result = self._scan_current(image, timestamp)
        observation = result.observation
        if observation is None:
            self._pending_jump = None
            return result

        if self._history and _center_jump(
            self._history[-1],
            observation,
        ) > 0.20:
            pending = self._pending_jump
            if pending is None or not _same_location(pending, observation):
                self._pending_jump = observation
                return ProgressScanResult(
                    observation=None,
                    valid_scanlines=result.valid_scanlines,
                    candidate_count=result.candidate_count,
                    rejection_reason="jump_pending",
                )

        self._pending_jump = None
        self._history.append(observation)
        return result

    def _scan_current(
        self,
        image: np.ndarray,
        timestamp: float,
    ) -> ProgressScanResult:
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
            np.ones((1, 3), dtype=np.uint8),
        )

        image_height, image_width = image.shape[:2]
        rows = [
            min(image_height - 1, round(image_height * fraction))
            for fraction in _SCAN_FRACTIONS
        ]
        band_top = min(rows)
        band_bottom = max(rows) + 1
        side = round(image_width * _SIDE_EXCLUSION)
        yellow_band = yellow_mask[band_top:band_bottom, side : image_width - side]
        yellow_columns = (yellow_band > 0).sum(axis=0) >= 3
        yellow_runs = _runs(yellow_columns, side)
        yellow_runs = [
            run
            for run in yellow_runs
            if run[1] - run[0] <= max(16, image_width * 0.04)
        ]
        if not yellow_runs:
            return ProgressScanResult(
                observation=None,
                rejection_reason="yellow_missing",
            )

        candidates_by_line = [
            _line_candidates(
                _runs(green_mask[row] > 0, 0),
                yellow_runs,
                image_width,
            )
            for row in rows
        ]
        valid_scanlines = sum(bool(candidates) for candidates in candidates_by_line)
        candidate_count = sum(len(candidates) for candidates in candidates_by_line)
        consensus = _consensus(candidates_by_line, image_width)
        if consensus is None:
            return ProgressScanResult(
                observation=None,
                valid_scanlines=valid_scanlines,
                candidate_count=candidate_count,
                rejection_reason="no_consensus",
            )
        candidate, agreeing_scanlines = consensus
        green_width = candidate.green_right - candidate.green_left
        if green_width < image_width * 0.12:
            return ProgressScanResult(
                observation=None,
                valid_scanlines=valid_scanlines,
                candidate_count=candidate_count,
                rejection_reason="bar_too_narrow",
            )

        agreement = agreeing_scanlines / len(_SCAN_FRACTIONS)
        width_score = min(
            1.0,
            green_width / (image_width * 0.12),
        )
        width = float(image_width)
        return ProgressScanResult(
            observation=ProgressObservation(
                green_left=candidate.green_left / width,
                green_right=candidate.green_right / width,
                yellow_x=candidate.yellow_center / width,
                confidence=(agreement + width_score) / 2,
                timestamp=timestamp,
            ),
            valid_scanlines=agreeing_scanlines,
            candidate_count=candidate_count,
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


def _runs(mask_row: np.ndarray, offset: int) -> list[tuple[int, int]]:
    active = mask_row.astype(bool)
    padded = np.pad(active.astype(np.int8), (1, 1))
    changes = np.diff(padded)
    starts = np.flatnonzero(changes == 1)
    ends = np.flatnonzero(changes == -1)
    return [
        (int(left + offset), int(right + offset))
        for left, right in zip(starts, ends)
    ]


def _line_candidates(
    green_runs: list[tuple[int, int]],
    yellow_runs: list[tuple[int, int]],
    image_width: int,
) -> list[_LineCandidate]:
    candidates: list[_LineCandidate] = []
    minimum_width = image_width * 0.12
    for yellow_left, yellow_right in yellow_runs:
        yellow_center = (yellow_left + yellow_right) / 2
        before = [
            run for run in green_runs if run[1] <= yellow_left + 3
        ]
        after = [
            run for run in green_runs if run[0] >= yellow_right - 3
        ]
        if before and after:
            left_run = max(before, key=lambda run: run[1])
            right_run = min(after, key=lambda run: run[0])
            if (
                yellow_left - left_run[1] <= 3
                and right_run[0] - yellow_right <= 3
                and right_run[1] - left_run[0] >= minimum_width
            ):
                candidates.append(
                    _LineCandidate(
                        left_run[0],
                        right_run[1],
                        yellow_center,
                    )
                )
        for green_left, green_right in green_runs:
            green_width = green_right - green_left
            margin = max(12.0, green_width * 0.05)
            if (
                green_width >= minimum_width
                and green_left - margin
                <= yellow_center
                <= green_right + margin
            ):
                candidates.append(
                    _LineCandidate(
                        green_left,
                        green_right,
                        yellow_center,
                    )
                )
    return candidates


def _consensus(
    candidates_by_line: list[list[_LineCandidate]],
    image_width: int,
) -> tuple[_LineCandidate, int] | None:
    tolerance = image_width * 0.02
    groups: list[list[_LineCandidate]] = []
    for line_index, line_candidates in enumerate(candidates_by_line):
        for seed in line_candidates:
            matches = [seed]
            for other_index, other_candidates in enumerate(candidates_by_line):
                if other_index == line_index:
                    continue
                compatible = [
                    candidate
                    for candidate in other_candidates
                    if (
                        abs(candidate.green_left - seed.green_left) <= tolerance
                        and abs(candidate.green_right - seed.green_right) <= tolerance
                    )
                ]
                if compatible:
                    matches.append(
                        min(
                            compatible,
                            key=lambda candidate: (
                                abs(candidate.green_left - seed.green_left)
                                + abs(candidate.green_right - seed.green_right)
                            ),
                        )
                    )
            if len(matches) >= 3:
                groups.append(matches)
    if not groups:
        return None
    selected = max(
        groups,
        key=lambda group: (
            len(group),
            np.median([item.green_right - item.green_left for item in group]),
        ),
    )
    return (
        _LineCandidate(
            green_left=round(np.median([item.green_left for item in selected])),
            green_right=round(np.median([item.green_right for item in selected])),
            yellow_center=float(
                np.median([item.yellow_center for item in selected])
            ),
        ),
        len(selected),
    )


def _center_jump(
    first: ProgressObservation,
    second: ProgressObservation,
) -> float:
    first_center = (first.green_left + first.green_right) / 2
    second_center = (second.green_left + second.green_right) / 2
    return abs(first_center - second_center)


def _same_location(
    first: ProgressObservation,
    second: ProgressObservation,
) -> bool:
    return (
        _center_jump(first, second) <= 0.02
        and abs(
            (first.green_right - first.green_left)
            - (second.green_right - second.green_left)
        )
        <= 0.02
        and abs(first.yellow_x - second.yellow_x) <= 0.02
    )
