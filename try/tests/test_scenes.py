import cv2
import numpy as np
import pytest

from auto_fishing.vision.scenes import BiteDetector, SceneRecognizer


BLUE_BGR = (255, 100, 0)
GREEN_BGR = (200, 255, 0)
YELLOW_BGR = (0, 230, 255)


def bite_frame() -> np.ndarray:
    image = np.zeros((100, 100, 3), dtype=np.uint8)
    cv2.circle(image, (50, 50), 35, BLUE_BGR, 8)
    cv2.circle(image, (50, 50), 15, (255, 255, 255), 5)
    return image


def result_frame(width: int = 1280, height: int = 720, *, ready_icon: bool = False) -> np.ndarray:
    image = np.full((height, width, 3), 25, dtype=np.uint8)
    cv2.circle(
        image,
        (round(width * 0.50), round(height * 0.50)),
        max(12, round(min(width, height) * 0.20)),
        BLUE_BGR,
        -1,
    )
    if ready_icon:
        draw_ready_icon(image)
    return image


def ready_frame(width: int = 1280, height: int = 720) -> np.ndarray:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    draw_ready_icon(image)
    return image


def draw_ready_icon(image: np.ndarray) -> None:
    height, width = image.shape[:2]
    x1, x2 = round(width * 0.89), round(width * 0.94)
    y1, y2 = round(height * 0.80), round(height * 0.85)
    cv2.rectangle(image, (x1, y1), (x2, y2), (255, 255, 255), -1)


def add_progress(image: np.ndarray) -> None:
    height, width = image.shape[:2]
    cv2.rectangle(
        image,
        (round(width * 0.38), round(height * 0.05)),
        (round(width * 0.62), round(height * 0.09)),
        GREEN_BGR,
        -1,
    )
    cv2.rectangle(
        image,
        (round(width * 0.50) - 3, round(height * 0.04)),
        (round(width * 0.50) + 3, round(height * 0.10)),
        YELLOW_BGR,
        -1,
    )


def add_bite_to_top_roi(image: np.ndarray) -> None:
    height, width = image.shape[:2]
    top_height = round(height * 0.15)
    center = (round(width * 0.50), round(top_height * 0.50))
    cv2.circle(image, center, round(top_height * 0.42), BLUE_BGR, -1)
    cv2.circle(image, center, round(top_height * 0.25), (255, 255, 255), -1)


def test_bite_requires_two_changed_frames_and_blue_shape_cross_feature() -> None:
    base = np.zeros((100, 100, 3), dtype=np.uint8)
    detector = BiteDetector()
    detector.set_baseline(base)

    assert detector.detect(bite_frame()) is False
    assert detector.detect(bite_frame()) is True


def test_bite_confirmation_resets_after_an_unchanged_frame() -> None:
    base = np.zeros((100, 100, 3), dtype=np.uint8)
    detector = BiteDetector()
    detector.set_baseline(base)

    assert detector.detect(bite_frame()) is False
    assert detector.detect(base) is False
    assert detector.detect(bite_frame()) is False
    assert detector.detect(bite_frame()) is True


def test_single_white_flash_does_not_trigger_bite() -> None:
    base = np.zeros((100, 100, 3), dtype=np.uint8)
    flash = base.copy()
    flash[20:80, 20:80] = 255
    detector = BiteDetector()
    detector.set_baseline(base)

    assert detector.detect(flash) is False
    assert detector.detect(flash) is False


def test_blue_change_without_shape_change_does_not_trigger_bite() -> None:
    base = np.full((100, 100, 3), BLUE_BGR, dtype=np.uint8)
    changed = base.copy()
    changed[20:80, 20:80] = BLUE_BGR
    detector = BiteDetector()
    detector.set_baseline(np.zeros_like(base))

    assert detector.detect(changed) is False
    assert detector.detect(changed) is False


@pytest.mark.parametrize("size", [(1280, 720), (1920, 1080), (1600, 1000)])
def test_scene_bite_baseline_and_top_roi_scale_with_client_resolution(
    size: tuple[int, int],
) -> None:
    width, height = size
    baseline = np.zeros((height, width, 3), dtype=np.uint8)
    changed = baseline.copy()
    add_bite_to_top_roi(changed)
    recognizer = SceneRecognizer()

    recognizer.set_bite_baseline(baseline)

    assert recognizer.observe(changed, 1.0).bite is False
    assert recognizer.observe(changed, 1.1).bite is True


def test_result_requires_three_consecutive_frames() -> None:
    recognizer = SceneRecognizer()
    frame = result_frame()

    assert recognizer.observe(frame, 1.0).result is False
    assert recognizer.observe(frame, 1.1).result is False
    observation = recognizer.observe(frame, 1.2)

    assert observation.result is True
    assert observation.progress is None


def test_result_confirmation_resets_when_candidate_disappears() -> None:
    recognizer = SceneRecognizer()
    candidate = result_frame()
    neutral = np.zeros_like(candidate)

    assert recognizer.observe(candidate, 1.0).result is False
    assert recognizer.observe(candidate, 1.1).result is False
    assert recognizer.observe(neutral, 1.2).result is False
    assert recognizer.observe(candidate, 1.3).result is False
    assert recognizer.observe(candidate, 1.4).result is False
    assert recognizer.observe(candidate, 1.5).result is True


def test_ready_requires_three_consecutive_frames() -> None:
    recognizer = SceneRecognizer()
    frame = ready_frame()

    assert recognizer.observe(frame, 1.0).ready is False
    assert recognizer.observe(frame, 1.1).ready is False
    assert recognizer.observe(frame, 1.2).ready is True


def test_ready_confirmation_resets_when_candidate_disappears() -> None:
    recognizer = SceneRecognizer()
    candidate = ready_frame()
    neutral = np.zeros_like(candidate)

    assert recognizer.observe(candidate, 1.0).ready is False
    assert recognizer.observe(candidate, 1.1).ready is False
    assert recognizer.observe(neutral, 1.2).ready is False
    assert recognizer.observe(candidate, 1.3).ready is False
    assert recognizer.observe(candidate, 1.4).ready is False
    assert recognizer.observe(candidate, 1.5).ready is True


def test_result_and_ready_are_mutually_exclusive() -> None:
    recognizer = SceneRecognizer()
    frame = result_frame(ready_icon=True)

    recognizer.observe(frame, 1.0)
    recognizer.observe(frame, 1.1)
    observation = recognizer.observe(frame, 1.2)

    assert observation.result is True
    assert observation.ready is False


def test_progress_suppresses_result_confirmation() -> None:
    recognizer = SceneRecognizer()
    frame = result_frame()
    add_progress(frame)

    for index in range(3):
        observation = recognizer.observe(frame, 1.0 + index / 30)

    assert observation.progress is not None
    assert observation.result is False


def test_progress_suppresses_ready_confirmation() -> None:
    recognizer = SceneRecognizer()
    frame = ready_frame()
    add_progress(frame)

    for index in range(3):
        observation = recognizer.observe(frame, 1.0 + index / 30)

    assert observation.progress is not None
    assert observation.ready is False


@pytest.mark.parametrize("size", [(1280, 720), (1920, 1080), (1600, 1000)])
def test_result_and_ready_rois_scale_with_client_resolution(size: tuple[int, int]) -> None:
    width, height = size
    result_recognizer = SceneRecognizer()
    ready_recognizer = SceneRecognizer()

    for index in range(3):
        result = result_recognizer.observe(result_frame(width, height), index / 30)
        ready = ready_recognizer.observe(ready_frame(width, height), index / 30)

    assert result.result is True
    assert result.ready is False
    assert ready.ready is True
    assert ready.result is False
