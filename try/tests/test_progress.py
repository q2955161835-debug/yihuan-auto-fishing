from pathlib import Path

import cv2
import numpy as np
import pytest

from auto_fishing.model import Direction, ProgressObservation
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


def narrow_frame(image_width: int, marker_fraction: float) -> np.ndarray:
    image = np.zeros((120, image_width, 3), dtype=np.uint8)
    green_width = round(image_width * 0.09)
    green_left = round(image_width * 0.35)
    green_right = green_left + green_width - 1
    yellow_width = max(3, round(image_width * 0.01))
    yellow_center = green_left + round(green_width * marker_fraction)
    yellow_left = yellow_center - yellow_width // 2
    yellow_right = yellow_left + yellow_width - 1
    cv2.rectangle(image, (green_left, 40), (green_right, 70), GREEN_BGR, -1)
    cv2.rectangle(image, (yellow_left, 34), (yellow_right, 76), YELLOW_BGR, -1)
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


def test_real_high_quality_fixture_detects_narrow_green_interval() -> None:
    fixture = Path("try/fixtures/progress/progress_narrow_high_quality.png")
    image = cv2.imdecode(
        np.fromfile(fixture, dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )

    observation = ProgressRecognizer().detect(image, 1.0)

    assert observation is not None
    assert observation.green_left < observation.yellow_x < observation.green_right
    assert 0.08 <= observation.green_right - observation.green_left <= 0.11


@pytest.mark.parametrize("image_width", [300, 600, 1200])
@pytest.mark.parametrize("marker_fraction", [0.08, 0.50, 0.92])
def test_narrow_split_bar_is_detected_at_multiple_scales_and_marker_positions(
    image_width: int,
    marker_fraction: float,
) -> None:
    observation = ProgressRecognizer().detect(
        narrow_frame(image_width, marker_fraction),
        1.0,
    )

    assert observation is not None
    assert observation.green_left < observation.yellow_x < observation.green_right
    assert 0.08 <= observation.green_right - observation.green_left <= 0.10


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


def test_tracks_short_green_bar_when_marker_moves_far_outside() -> None:
    recognizer = ProgressRecognizer()

    initial = recognizer.detect(
        frame(green=(96, 136), yellow=116),
        0.0,
    )
    moved = recognizer.analyze(
        frame(green=(100, 118), yellow=180),
        1 / 30,
    )

    assert initial is not None
    observation = moved.observation
    assert observation is not None
    assert moved.rejection_reason == ""
    assert abs(observation.green_left - 100 / 300) < 0.02
    assert abs(observation.green_right - 119 / 300) < 0.02
    assert abs(observation.yellow_x - 180 / 300) < 0.02


def test_detects_short_bar_and_marker_independently_across_top_playfield() -> None:
    image = frame(green=(100, 118), yellow=180)
    cv2.rectangle(image, (29, 34), (35, 76), YELLOW_BGR, -1)

    observation = ProgressRecognizer().detect(image, 0.0)

    assert observation is not None
    assert abs(observation.green_left - 100 / 300) < 0.02
    assert abs(observation.green_right - 119 / 300) < 0.02
    assert abs(observation.yellow_x - 180 / 300) < 0.02


def test_recovery_prefers_valid_short_bar_over_nearby_tiny_noise() -> None:
    recognizer = ProgressRecognizer()
    assert recognizer.detect(
        frame(green=(96, 136), yellow=190),
        0.0,
    ) is not None
    image = frame(green=(150, 168), yellow=210)
    cv2.rectangle(image, (100, 40), (102, 70), GREEN_BGR, -1)

    result = recognizer.analyze(image, 1 / 30)

    assert result.observation is not None
    assert abs(result.observation.green_left - 150 / 300) < 0.02
    assert abs(result.observation.green_right - 169 / 300) < 0.02
    assert abs(result.observation.yellow_x - 210 / 300) < 0.02


def test_controller_moves_toward_inner_safe_band() -> None:
    controller = ProgressController(center_tolerance_ratio=0.10)
    observations = [
        ProgressObservation(70 / 300, 171 / 300, yellow / 300, 1.0, timestamp)
        for yellow, timestamp in ((75, 1.0), (120, 1.1), (165, 1.2))
    ]

    assert controller.decide(observations[0]) == Direction.RIGHT
    assert controller.decide(observations[1]) == Direction.RELEASE
    assert controller.decide(observations[2]) == Direction.LEFT


def test_controller_moves_marker_right_when_it_is_left_of_green_center() -> None:
    observation = ProgressRecognizer().detect(frame(green=(180, 270), yellow=190), 1.0)

    assert ProgressController().decide(observation) == Direction.RIGHT


def test_controller_uses_a_narrow_deadband_around_green_center() -> None:
    recognizer = ProgressRecognizer()
    controller = ProgressController()

    assert controller.decide(recognizer.detect(frame(yellow=100), 1.0)) == Direction.RIGHT
    assert controller.decide(recognizer.detect(frame(yellow=120), 1.1)) == Direction.RELEASE
    assert controller.decide(recognizer.detect(frame(yellow=140), 1.2)) == Direction.LEFT


def control_observation(
    yellow_x: float,
    *,
    confidence: float = 1.0,
    timestamp: float = 0.0,
) -> ProgressObservation:
    return ProgressObservation(
        green_left=0.4,
        green_right=0.6,
        yellow_x=yellow_x,
        confidence=confidence,
        timestamp=timestamp,
    )


def test_controller_keeps_at_most_fifteen_recent_observations() -> None:
    controller = ProgressController()

    for index in range(20):
        controller.decide(
            control_observation(0.7, timestamp=index / 30),
        )

    assert controller.sample_count == 15


def test_controller_prioritizes_newest_equally_clear_frame() -> None:
    controller = ProgressController()
    for index in range(14):
        controller.decide(
            control_observation(0.7, timestamp=index / 30),
        )

    direction = controller.decide(
        control_observation(0.3, timestamp=14 / 30),
    )

    assert direction is Direction.RIGHT
    assert controller.weighted_error < -controller.center_tolerance_ratio


def test_controller_uses_clear_previous_frame_when_latest_is_unclear() -> None:
    controller = ProgressController()
    controller.decide(control_observation(0.7, confidence=1.0))

    direction = controller.decide(
        control_observation(0.3, confidence=0.1, timestamp=1 / 30),
    )

    assert direction is Direction.LEFT
    assert controller.weighted_error > controller.center_tolerance_ratio


def test_controller_missing_frame_clears_recent_observations() -> None:
    controller = ProgressController()
    controller.decide(control_observation(0.7))
    controller.decide(control_observation(0.7, timestamp=1 / 30))

    direction = controller.decide(None)

    assert direction is Direction.RELEASE
    assert controller.sample_count == 0
    assert controller.weighted_error == 0.0


def test_controller_discards_observations_after_long_frame_gap() -> None:
    controller = ProgressController()
    controller.decide(control_observation(0.7, timestamp=0.0))

    direction = controller.decide(
        control_observation(0.3, timestamp=0.21),
    )

    assert direction is Direction.RIGHT
    assert controller.sample_count == 1


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
    result = ProgressRecognizer().analyze(frame(green=(100, 102), yellow=112), 1.0)

    assert result.observation is None
    assert result.rejection_reason == "bar_too_narrow"


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
        left = 20 + index * 4
        right = left + 100
        yellow = left + 50
        obs = recognizer.detect(frame((left, right), yellow), index / 30)

        assert obs is not None
        assert abs(obs.green_left - left / 300) < 0.01
        assert abs(obs.green_right - (right + 1) / 300) < 0.01
        assert abs(obs.yellow_x - yellow / 300) < 0.01
        assert controller.decide(obs) == Direction.RELEASE


def test_analyze_reports_scanline_consensus() -> None:
    result = ProgressRecognizer().analyze(frame(), 1.0)

    assert result.observation is not None
    assert result.valid_scanlines >= 3
    assert result.candidate_count >= 1
    assert result.rejection_reason == ""


def test_large_single_frame_jump_is_available_immediately() -> None:
    recognizer = ProgressRecognizer()

    assert recognizer.detect(frame((20, 120), 70), 0.0) is not None
    observation = recognizer.detect(frame((170, 270), 220), 1 / 30)

    assert observation is not None
    assert observation.green_left > 0.55


@pytest.mark.parametrize(
    ("green", "yellow"),
    [((50, 90), 52), ((210, 250), 248)],
)
def test_reset_accepts_extreme_marker_as_next_stage_first_frame(
    green: tuple[int, int],
    yellow: int,
) -> None:
    recognizer = ProgressRecognizer()

    assert recognizer.detect(frame((100, 200), 150), 0.0) is not None
    recognizer.reset()
    result = recognizer.analyze(frame(green, yellow), 1 / 30)

    assert result.observation is not None
    assert result.rejection_reason == ""
    assert abs(result.observation.yellow_x - yellow / 300) < 0.02


def test_yellow_only_jump_is_available_immediately() -> None:
    recognizer = ProgressRecognizer()

    assert recognizer.detect(frame((70, 170), 120), 0.0) is not None
    moved = recognizer.analyze(frame((70, 170), 156), 1 / 30)

    assert moved.observation is not None
    assert moved.rejection_reason == ""
    assert abs(moved.observation.yellow_x - 156 / 300) < 0.02


def test_missing_frame_does_not_reuse_last_observation() -> None:
    recognizer = ProgressRecognizer()

    assert recognizer.detect(frame((20, 120), 70), 0.0) is not None
    assert (
        recognizer.detect(
            np.zeros((120, 300, 3), dtype=np.uint8),
            1 / 30,
        )
        is None
    )
