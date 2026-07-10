from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

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
    ) -> str:
        now = now or datetime.now(timezone.utc)
        self.root.mkdir(parents=True, exist_ok=True)
        base_stem = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}_{code}"
        stem = base_stem
        sequence = 1
        while any(
            (self.root / f"{stem}{suffix}").exists()
            for suffix in (".png", ".json")
        ):
            stem = f"{base_stem}_{sequence}"
            sequence += 1
        image_path = self.root / f"{stem}.png"
        meta_path = self.root / f"{stem}.json"
        if not cv2.imwrite(str(image_path), frame):
            raise OSError("诊断截图写入失败")
        meta_path.write_text(
            json.dumps(
                {"code": code, "detail": detail, "created_at": now.isoformat()},
                ensure_ascii=False,
            ),
            "utf-8",
        )
        for path in (image_path, meta_path):
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
            if path.is_file() and path.suffix in {".png", ".json"}:
                groups.setdefault(path.stem, []).append(path)
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
