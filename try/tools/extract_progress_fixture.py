from pathlib import Path
import sys

import cv2
import numpy as np


def main(source: Path, output: Path) -> None:
    frame = cv2.imdecode(
        np.fromfile(source, dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    if frame is None:
        raise SystemExit(f"无法读取原始诊断帧: {source}")
    height, width = frame.shape[:2]
    top = frame[
        0 : round(height * 0.15),
        round(width * 0.24) : round(width * 0.76),
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded, payload = cv2.imencode(".png", top)
    if not encoded:
        raise SystemExit("测试夹具编码失败")
    payload.tofile(output)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
