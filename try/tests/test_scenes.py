import cv2
import numpy as np
import pytest
from pathlib import Path

from auto_fishing.model import Rect
from auto_fishing.vision.scenes import BiteDetector, SceneRecognizer


BLUE_BGR = (255, 100, 0)
GREEN_BGR = (200, 255, 0)
YELLOW_BGR = (0, 230, 255)
RESULT_FIXTURE_OCCLUSION = Rect(0, 173, 320, 270)


def bite_prompt_frame() -> np.ndarray:
    image = np.zeros((100, 100, 3), dtype=np.uint8)
    cv2.circle(image, (50, 50), 35, BLUE_BGR, -1)
    cv2.circle(image, (50, 50), 21, (255, 255, 255), 3)
    return image


def result_frame(width: int = 1280, height: int = 720, *, ready_icon: bool = False) -> np.ndarray:
    image = np.full((height, width, 3), 25, dtype=np.uint8)
    cv2.rectangle(
        image,
        (round(width * 0.38), round(height * 0.04)),
        (round(width * 0.62), round(height * 0.13)),
        (35, 35, 35),
        -1,
    )
    cv2.rectangle(
        image,
        (round(width * 0.43), round(height * 0.07)),
        (round(width * 0.57), round(height * 0.10)),
        (180, 30, 220),
        -1,
    )
    cv2.rectangle(
        image,
        (round(width * 0.395), round(height * 0.06)),
        (round(width * 0.415), round(height * 0.08)),
        (255, 255, 255),
        -1,
    )
    radius = max(12, round(min(width, height) * 0.20))
    cv2.circle(
        image,
        (round(width * 0.50), round(height * 0.50)),
        round(radius * 1.08),
        (240, 240, 240),
        -1,
    )
    cv2.circle(
        image,
        (round(width * 0.50), round(height * 0.50)),
        radius,
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


def add_bite_prompt(image: np.ndarray) -> None:
    height, width = image.shape[:2]
    roi_short_side = min(round(width * 0.10), round(height * 0.20))
    center = (round(width * 0.94), round(height * 0.89))
    cv2.circle(image, center, round(roi_short_side * 0.40), BLUE_BGR, -1)
    cv2.circle(
        image,
        center,
        round(roi_short_side * 0.24),
        (255, 255, 255),
        max(2, round(roi_short_side * 0.07)),
    )


def reel_prompt_frame(width: int = 1280, height: int = 720) -> np.ndarray:
    image = np.full((height, width, 3), 80, dtype=np.uint8)
    x1, x2 = round(width * 0.22), round(width * 0.60)
    y1, y2 = round(height * 0.16), round(height * 0.24)
    cv2.rectangle(image, (x1, y1), (x2, y2), (20, 20, 20), -1)
    cv2.rectangle(
        image,
        (round(width * 0.31), round(height * 0.185)),
        (round(width * 0.51), round(height * 0.215)),
        (255, 255, 255),
        -1,
    )
    return image


def screen_keyboard_patch_frame() -> tuple[np.ndarray, Rect]:
    image = np.full((720, 1280, 3), 180, dtype=np.uint8)
    occlusion = Rect(0, 330, 920, 720)
    image[occlusion.top : occlusion.bottom, occlusion.left : occlusion.right] = 25
    cv2.circle(image, (620, 520), 150, BLUE_BGR, -1)
    return image, occlusion


def dark_blue_night_frame() -> np.ndarray:
    image = np.full((720, 1280, 3), 25, dtype=np.uint8)
    cv2.rectangle(image, (320, 160), (470, 650), BLUE_BGR, -1)
    cv2.rectangle(image, (810, 180), (950, 620), BLUE_BGR, -1)
    return image


def real_result_reference() -> np.ndarray:
    path = Path(__file__).resolve().parents[2] / "流程截图" / "第四步点击空白处关闭.jpg"
    return cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)


def result_fixture(name: str) -> np.ndarray:
    path = Path(__file__).resolve().parents[1] / "fixtures" / "result" / name
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def test_bite_prompt_requires_two_consecutive_frames_after_cast_cooldown() -> None:
    base = np.zeros((100, 100, 3), dtype=np.uint8)
    detector = BiteDetector()
    detector.set_baseline(base)

    for _ in range(45):
        assert detector.detect(bite_prompt_frame()) is False
    assert detector.detect(bite_prompt_frame()) is False
    assert detector.detect(bite_prompt_frame()) is True


def test_bite_confirmation_resets_after_an_unchanged_frame() -> None:
    base = np.zeros((100, 100, 3), dtype=np.uint8)
    detector = BiteDetector()
    detector.set_baseline(base)

    for _ in range(45):
        detector.detect(base)
    assert detector.detect(bite_prompt_frame()) is False
    assert detector.detect(base) is False
    assert detector.detect(bite_prompt_frame()) is False
    assert detector.detect(bite_prompt_frame()) is True


def test_single_white_flash_does_not_trigger_bite() -> None:
    base = np.zeros((100, 100, 3), dtype=np.uint8)
    flash = base.copy()
    flash[20:80, 20:80] = 255
    detector = BiteDetector()
    detector.set_baseline(base)

    for _ in range(45):
        detector.detect(base)
    assert detector.detect(flash) is False
    assert detector.detect(flash) is False


def test_cast_animation_does_not_trigger_bite_during_cooldown() -> None:
    base = np.zeros((100, 100, 3), dtype=np.uint8)
    detector = BiteDetector()
    detector.set_baseline(base)

    for _ in range(45):
        assert detector.detect(bite_prompt_frame()) is False


@pytest.mark.parametrize("size", [(1280, 720), (1920, 1080), (1600, 1000)])
def test_scene_right_bottom_bite_prompt_scales_with_client_resolution(
    size: tuple[int, int],
) -> None:
    width, height = size
    baseline = np.zeros((height, width, 3), dtype=np.uint8)
    changed = baseline.copy()
    add_bite_prompt(changed)
    recognizer = SceneRecognizer()

    recognizer.set_bite_baseline(baseline)

    for index in range(45):
        assert recognizer.observe(baseline, index / 30).bite is False
    assert recognizer.observe(changed, 2.0).bite is False
    assert recognizer.observe(changed, 2.1).bite is True


def test_result_requires_three_consecutive_frames() -> None:
    recognizer = SceneRecognizer()
    frame = result_frame()

    first = recognizer.observe(frame, 1.0)
    second = recognizer.observe(frame, 1.1)
    observation = recognizer.observe(frame, 1.2)

    assert first.result_candidate is True
    assert first.result is False
    assert second.result_candidate is True
    assert second.result is False
    assert observation.result_candidate is True
    assert observation.result is True
    assert observation.progress is None


def test_real_transition_vortex_never_confirms_result() -> None:
    recognizer = SceneRecognizer()
    frame = result_fixture("result_transition_vortex.jpg")

    for index in range(3):
        observation = recognizer.observe(
            frame,
            1.0 + index / 30,
            occlusion=RESULT_FIXTURE_OCCLUSION,
        )

        assert observation.result_candidate is False
        assert observation.result is False


def test_real_green_transition_never_confirms_result() -> None:
    recognizer = SceneRecognizer()
    frame = result_fixture("result_transition_green.jpg")

    for index in range(3):
        observation = recognizer.observe(
            frame,
            1.0 + index / 30,
            occlusion=RESULT_FIXTURE_OCCLUSION,
        )

        assert observation.result_candidate is False
        assert observation.result is False


def test_real_catch_card_confirms_after_three_frames() -> None:
    recognizer = SceneRecognizer()
    frame = result_fixture("result_catch_card.jpg")

    first = recognizer.observe(
        frame,
        1.0,
        occlusion=RESULT_FIXTURE_OCCLUSION,
    )
    second = recognizer.observe(
        frame,
        1.1,
        occlusion=RESULT_FIXTURE_OCCLUSION,
    )
    third = recognizer.observe(
        frame,
        1.2,
        occlusion=RESULT_FIXTURE_OCCLUSION,
    )

    assert first.result_candidate is True
    assert first.result is False
    assert second.result_candidate is True
    assert second.result is False
    assert third.result_candidate is True
    assert third.result is True


def test_real_green_catch_card_confirms_after_three_frames() -> None:
    recognizer = SceneRecognizer()
    frame = result_fixture("result_catch_card_green.jpg")

    first = recognizer.observe(
        frame,
        1.0,
        occlusion=RESULT_FIXTURE_OCCLUSION,
    )
    second = recognizer.observe(
        frame,
        1.1,
        occlusion=RESULT_FIXTURE_OCCLUSION,
    )
    third = recognizer.observe(
        frame,
        1.2,
        occlusion=RESULT_FIXTURE_OCCLUSION,
    )

    assert first.result_candidate is True
    assert first.result is False
    assert second.result_candidate is True
    assert second.result is False
    assert third.result_candidate is True
    assert third.result is True


def test_reel_prompt_requires_two_consecutive_frames() -> None:
    recognizer = SceneRecognizer()
    prompt = reel_prompt_frame()
    neutral = np.full_like(prompt, 80)

    assert recognizer.observe(prompt, 1.0).reel_prompt is False
    assert recognizer.observe(neutral, 1.1).reel_prompt is False
    assert recognizer.observe(prompt, 1.2).reel_prompt is False
    assert recognizer.observe(prompt, 1.3).reel_prompt is True


def test_dark_blue_night_scene_is_not_result_candidate() -> None:
    recognizer = SceneRecognizer()

    observation = recognizer.observe(dark_blue_night_frame(), 1.0)

    assert observation.result_candidate is False


def test_real_result_reference_is_detected_after_three_frames() -> None:
    recognizer = SceneRecognizer()
    frame = real_result_reference()

    recognizer.observe(frame, 1.0)
    recognizer.observe(frame, 1.1)
    observation = recognizer.observe(frame, 1.2)

    assert observation.result is True


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


def test_ready_rejects_right_bottom_bite_prompt() -> None:
    recognizer = SceneRecognizer()
    frame = ready_frame()
    add_bite_prompt(frame)

    assert recognizer.observe(frame, 1.0).ready is False
    assert recognizer.observe(frame, 1.1).ready is False
    assert recognizer.observe(frame, 1.2).ready is False


def test_real_closed_result_hook_confirms_ready_after_three_frames() -> None:
    recognizer = SceneRecognizer()
    frame = result_fixture("result_closed_hook.jpg")

    assert recognizer.observe(frame, 1.0).ready is False
    assert recognizer.observe(frame, 1.1).ready is False
    assert recognizer.observe(frame, 1.2).ready is True


def test_real_bite_prompt_is_not_initial_ready_hook() -> None:
    recognizer = SceneRecognizer()
    frame = result_fixture("bite_prompt_real.jpg")

    assert recognizer.observe(frame, 1.0).ready is False
    assert recognizer.observe(frame, 1.1).ready is False
    assert recognizer.observe(frame, 1.2).ready is False


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


def test_scene_observation_copies_progress_scan_diagnostics() -> None:
    recognizer = SceneRecognizer()
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    add_progress(frame)

    observation = recognizer.observe(frame, 1.0)

    assert observation.progress is not None
    assert observation.progress_scanlines >= 3
    assert observation.progress_candidates >= 1
    assert observation.progress_rejection == ""


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


def test_result_ratios_ignore_screen_keyboard_occlusion() -> None:
    frame, occlusion = screen_keyboard_patch_frame()
    recognizer = SceneRecognizer()

    observation = recognizer.observe(frame, 1.0, occlusion=occlusion)

    assert observation.result_candidate is False


def test_result_rejects_too_few_unoccluded_pixels() -> None:
    frame = result_frame()
    recognizer = SceneRecognizer()

    with pytest.raises(ValueError, match="结算识别有效像素不足"):
        recognizer.observe(
            frame,
            1.0,
            occlusion=Rect(0, 0, frame.shape[1], frame.shape[0]),
        )
