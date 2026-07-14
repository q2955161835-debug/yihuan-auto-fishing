import pytest

from auto_fishing.model import NormalizedRect, Rect
from auto_fishing.vision.geometry import crop_normalized


@pytest.mark.parametrize(
    "size",
    [
        (1280, 720),
        (1600, 900),
        (1920, 1080),
        (2560, 1440),
        (3440, 1440),
        (3840, 2160),
    ],
)
def test_progress_roi_scales_with_client(size):
    width, height = size
    client = Rect(100, 200, 100 + width, 200 + height)
    roi = NormalizedRect(0.24, 0.00, 0.76, 0.15).to_pixels(client)
    assert roi == Rect(
        100 + round(width * 0.24),
        200,
        100 + round(width * 0.76),
        200 + round(height * 0.15),
    )


def test_normalized_rect_rejects_invalid_bounds():
    with pytest.raises(ValueError, match="0 <= left < right <= 1"):
        NormalizedRect(0.8, 0.1, 0.2, 0.9)


def test_crop_normalized_returns_expected_shape():
    import numpy as np

    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    crop = crop_normalized(frame, NormalizedRect(0.25, 0.20, 0.75, 0.80))
    assert crop.shape == (60, 100, 3)


@pytest.mark.parametrize("scale_percent", [100, 125, 150, 175, 200])
def test_normalized_geometry_uses_physical_pixels_at_each_scale(
    scale_percent: int,
) -> None:
    scale = scale_percent / 100
    width = round(1280 * scale)
    height = round(720 * scale)
    client = Rect(-width, 0, 0, height)

    roi = NormalizedRect(0.25, 0.20, 0.75, 0.80).to_pixels(client)

    assert roi.width == round(width * 0.75) - round(width * 0.25)
    assert roi.height == round(height * 0.80) - round(height * 0.20)
