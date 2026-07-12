from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from collections.abc import Sequence

import cv2
import numpy as np


class DiagnosticsStore:
    def __init__(self, diagnostics_dir: Path) -> None:
        self.root = diagnostics_dir.resolve()

    def save(
        self,
        frame: np.ndarray,
        code: str,
        detail: str,
        now: datetime | None = None,
        *,
        progress_frames: Sequence[np.ndarray] = (),
    ) -> str:
        now = now or datetime.now(timezone.utc)
        self.root.mkdir(parents=True, exist_ok=True)
        base_stem = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}_{code}"
        stem = base_stem
        sequence = 1
        while any(
            (self.root / f"{stem}{suffix}").exists()
            for suffix in (".png", ".json", "_progress.jpg")
        ):
            stem = f"{base_stem}_{sequence}"
            sequence += 1
        image_path = self.root / f"{stem}.png"
        meta_path = self.root / f"{stem}.json"
        progress_path = self.root / f"{stem}_progress.jpg"
        encoded, payload = cv2.imencode(".png", frame)
        if not encoded:
            raise OSError("诊断截图写入失败")
        payload.tofile(image_path)
        meta_path.write_text(
            json.dumps(
                {"code": code, "detail": detail, "created_at": now.isoformat()},
                ensure_ascii=False,
            ),
            "utf-8",
        )
        written_paths = [image_path, meta_path]
        if progress_frames:
            contact_sheet = _progress_contact_sheet(progress_frames)
            progress_encoded, progress_payload = cv2.imencode(
                ".jpg",
                contact_sheet,
                [cv2.IMWRITE_JPEG_QUALITY, 50],
            )
            if not progress_encoded:
                raise OSError("进度槽诊断序列写入失败")
            progress_payload.tofile(progress_path)
            written_paths.append(progress_path)
        for path in written_paths:
            os.utime(path, (now.timestamp(), now.timestamp()))
        self.cleanup(now)
        return stem

    def cleanup(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        if not self.root.exists():
            return
        groups: dict[str, list[Path]] = {}
        for path in self.root.iterdir():
            resolved = path.resolve()
            resolved.relative_to(self.root)
            if path.is_file() and path.suffix in {".png", ".json", ".jpg"}:
                stem = path.stem
                if path.suffix == ".jpg" and stem.endswith("_progress"):
                    stem = stem[: -len("_progress")]
                groups.setdefault(stem, []).append(path)
        cutoff = now.timestamp() - timedelta(days=7).total_seconds()
        fresh: list[tuple[float, str, list[Path]]] = []
        for stem, files in groups.items():
            newest = max(path.stat().st_mtime for path in files)
            if newest < cutoff:
                for path in files:
                    path.unlink(missing_ok=True)
            else:
                fresh.append((newest, stem, files))
        for _, _, files in sorted(fresh, reverse=True)[20:]:
            for path in files:
                path.unlink(missing_ok=True)


def _progress_contact_sheet(
    frames: Sequence[np.ndarray],
) -> np.ndarray:
    selected = [np.ascontiguousarray(frame).copy() for frame in frames[-12:]]
    if not selected:
        raise ValueError("进度槽诊断序列不能为空")
    target_height, target_width = selected[-1].shape[:2]
    normalized = [
        frame
        if frame.shape[:2] == (target_height, target_width)
        else cv2.resize(
            frame,
            (target_width, target_height),
            interpolation=cv2.INTER_AREA,
        )
        for frame in selected
    ]
    while len(normalized) < 12:
        normalized.append(
            np.zeros((target_height, target_width, 3), dtype=np.uint8)
        )
    rows = [
        np.hstack(normalized[index : index + 4])
        for index in range(0, 12, 4)
    ]
    return np.vstack(rows)
