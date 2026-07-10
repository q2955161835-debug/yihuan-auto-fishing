import numpy as np

from auto_fishing.model import NormalizedRect


def crop_normalized(frame: np.ndarray, roi: NormalizedRect) -> np.ndarray:
    height, width = frame.shape[:2]
    x1, x2 = round(width * roi.left), round(width * roi.right)
    y1, y2 = round(height * roi.top), round(height * roi.bottom)
    return frame[y1:y2, x1:x2]
