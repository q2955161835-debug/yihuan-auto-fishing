from pathlib import Path

import cv2
import numpy as np

from auto_fishing.model import Direction
from auto_fishing.vision.progress import ProgressController, ProgressRecognizer


GREEN_BGR = (255, 210, 30)
YELLOW_BGR = (0, 230, 255)


def frame(
    green: tuple[int, int] = (70, 170),
    yellow: int = 120,
    *,
    green_y: tuple[int, int] = (40, 70),
    yellow_y: tuple[int, int] = (34, 76),
) -> np.ndarray:
    image = np.zeros((120, 300, 3), dtype=np.uint8)
    cv2.rectangle(
        image,
        (green[0], green_y[0]),
        (green[1], green_y[1]),
        GREEN_BGR,
        -1,
    )
    cv2.rectangle(
        image,
        (yellow - 3, yellow_y[0]),
        (yellow + 3, yellow_y[1]),
        YELLOW_BGR,
        -1,
    )
    return image


def test_detects_green_interval_and_yellow_marker() -> None:
    obs = ProgressRecognizer().detect(frame(), 1.0)

    assert obs is not None
    assert 0.22 < obs.green_left < 0.26
    assert 0.55 < obs.green_right < 0.59
    assert 0.38 < obs.yellow_x < 0.42
    assert obs.timestamp == 1.0


def test_real_split_marker_fixture_reconstructs_full_green_interval() -> None:
    fixture = Path("try/fixtures/progress/progress_split_marker.png")
    image = cv2.imdecode(
        np.fromfile(fixture, dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )

    observation = ProgressRecognizer().detect(image, 1.0)

    assert observation is not None
    assert observation.green_left < observation.yellow_x < observation.green_right
    assert observation.green_right - observation.green_left > 0.12


def test_marker_sweep_never_loses_bar_when_marker_splits_green() -> None:
    recognizer = ProgressRecognizer()

    for index, yellow in enumerate(range(75, 166, 3)):
        observation = recognizer.detect(
            frame(green=(70, 170), yellow=yellow),
            index / 30,
        )

        assert observation is not None
        assert abs(observation.green_left - 70 / 300) < 0.02
        assert abs(observation.green_right - 171 / 300) < 0.02


def test_controller_moves_toward_inner_safe_band() -> None:
    recognizer = ProgressRecognizer()
    controller = ProgressController(center_tolerance_ratio=0.10)

    assert controller.decide(recognizer.detect(frame(yellow=75), 1.0)) == Direction.RIGHT
    assert controller.decide(recognizer.detect(frame(yellow=120), 1.1)) == Direction.RELEASE
    assert controller.decide(recognizer.detect(frame(yellow=165), 1.2)) == Direction.LEFT


def test_controller_moves_marker_right_when_it_is_left_of_green_center() -> None:
    observation = ProgressRecognizer().detect(frame(green=(180, 270), yellow=190), 1.0)

    assert ProgressController().decide(observation) == Direction.RIGHT


def test_controller_uses_a_narrow_deadband_around_green_center() -> None:
    recognizer = ProgressRecognizer()
    controller = ProgressController()

    assert controller.decide(recognizer.detect(frame(yellow=100), 1.0)) == Direction.RIGHT
    assert controller.decide(recognizer.detect(frame(yellow=120), 1.1)) == Direction.RELEASE
    assert controller.decide(recognizer.detect(frame(yellow=140), 1.2)) == Direction.LEFT


def test_no_color_candidates_returns_none_and_releases() -> None:
    observation = ProgressRecognizer().detect(np.zeros((120, 300, 3), dtype=np.uint8), 1.0)

    assert observation is None
    assert ProgressController().decide(observation) == Direction.RELEASE


def test_ignores_multiple_larger_noise_candidates_without_spatial_pair() -> None:
    image = frame()
    cv2.rectangle(image, (10, 94), (240, 108), GREEN_BGR, -1)
    cv2.rectangle(image, (250, 0), (256, 28), YELLOW_BGR, -1)
    cv2.rectangle(image, (185, 82), (275, 88), YELLOW_BGR, -1)

    obs = ProgressRecognizer().detect(image, 2.0)

    assert obs is not None
    assert 0.22 < obs.green_left < 0.26
    assert 0.55 < obs.green_right < 0.59
    assert 0.38 < obs.yellow_x < 0.42


def test_rejects_green_region_that_is_too_narrow() -> None:
    assert ProgressRecognizer().detect(frame(green=(100, 125), yellow=112), 1.0) is None


def test_rejects_yellow_marker_outside_progress_bar_vertical_span() -> None:
    assert ProgressRecognizer().detect(frame(yellow_y=(2, 24)), 1.0) is None


def test_detects_thin_marker_just_beyond_green_bar_edge() -> None:
    image = np.zeros((120, 300, 3), dtype=np.uint8)
    cv2.rectangle(image, (70, 40), (170, 70), GREEN_BGR, -1)
    cv2.rectangle(image, (173, 45), (175, 55), YELLOW_BGR, -1)
    observation = ProgressRecognizer().detect(image, 1.0)

    assert observation is not None
    assert 0.22 < observation.green_left < 0.26
    assert 0.57 < observation.yellow_x < 0.59


def test_rejects_tall_yellow_ui_outside_green_bar_horizontally() -> None:
    image = np.zeros((120, 300, 3), dtype=np.uint8)
    cv2.rectangle(image, (70, 40), (170, 70), GREEN_BGR, -1)
    cv2.rectangle(image, (10, 34), (16, 100), YELLOW_BGR, -1)

    assert ProgressRecognizer().detect(image, 1.0) is None


def test_detects_progress_region_touching_both_horizontal_boundaries() -> None:
    obs = ProgressRecognizer().detect(frame(green=(0, 299), yellow=150), 3.0)

    assert obs is not None
    assert obs.green_left == 0.0
    assert obs.green_right == 1.0
    assert 0.49 < obs.yellow_x < 0.51


def test_thirty_fast_frames_are_located_independently() -> None:
    recognizer = ProgressRecognizer()
    controller = ProgressController()

    for index in range(30):
        left = (index * 37) % 170
        right = left + 100
        yellow = left + 50
        obs = recognizer.detect(frame((left, right), yellow), index / 30)

        assert obs is not None
        assert abs(obs.green_left - left / 300) < 0.01
        assert abs(obs.green_right - (right + 1) / 300) < 0.01
        assert abs(obs.yellow_x - yellow / 300) < 0.01
        assert controller.decide(obs) == Direction.RELEASE


def test_missing_frame_between_fast_frames_does_not_reuse_last_observation() -> None:
    recognizer = ProgressRecognizer()

    assert recognizer.detect(frame((20, 120), 70), 0.0) is not None
    assert recognizer.detect(np.zeros((120, 300, 3), dtype=np.uint8), 1 / 30) is None
    obs = recognizer.detect(frame((170, 270), 220), 2 / 30)

    assert obs is not None
    assert obs.green_left > 0.55
