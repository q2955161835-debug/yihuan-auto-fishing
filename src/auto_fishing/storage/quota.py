from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import shutil
import threading


DEFAULT_MAX_BYTES = 100 * 1024 * 1024


class StorageQuotaError(RuntimeError):
    """数据目录无法恢复到配置的容量上限。"""


class StorageQuotaManager:
    def __init__(self, root: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes 必须至少为 1")
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self._known_total: int | None = None
        self._active_run: Path | None = None
        self._active_events: Path | None = None

    @property
    def total_bytes(self) -> int:
        with self._lock:
            self._known_total = self._tree_bytes(self.root)
            return self._known_total

    def initialize(self) -> None:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            self._known_total = self._tree_bytes(self.root)
            self._enforce()

    def register_write(
        self,
        path: Path,
        previous_size: int,
        *,
        active_run: Path | None = None,
        active_events: Path | None = None,
    ) -> None:
        with self._lock:
            resolved = self._inside(path)
            if active_run is not None:
                self._active_run = self._inside(active_run)
            if active_events is not None:
                self._active_events = self._inside(active_events)
            if self._known_total is None:
                self.root.mkdir(parents=True, exist_ok=True)
                self._known_total = self._tree_bytes(self.root)
            current_size = resolved.stat().st_size if resolved.is_file() else 0
            self._known_total += current_size - max(0, previous_size)
            if self._known_total > self.max_bytes:
                self._enforce()

    def _enforce(self) -> None:
        total = self._tree_bytes(self.root)
        if total <= self.max_bytes:
            self._known_total = total
            return
        active_run = self._effective_active_run()
        active_events = self._effective_events(active_run)
        for run in self._completed_runs(active_run):
            shutil.rmtree(self._inside(run))
            total = self._tree_bytes(self.root)
            if total <= self.max_bytes:
                self._known_total = total
                return
        for files in self._diagnostic_groups():
            for path in files:
                self._inside(path).unlink(missing_ok=True)
            total = self._tree_bytes(self.root)
            if total <= self.max_bytes:
                self._known_total = total
                return
        if active_run is not None:
            frames = active_run / "frames"
            if frames.is_dir():
                for path in sorted(frames.glob("*.jpg")):
                    self._inside(path).unlink(missing_ok=True)
                    total = self._tree_bytes(self.root)
                    if total <= self.max_bytes:
                        self._known_total = total
                        return
        if active_events is not None and active_events.is_file():
            other_bytes = total - active_events.stat().st_size
            self._trim_events(active_events, self.max_bytes - other_bytes)
            total = self._tree_bytes(self.root)
        self._known_total = total
        if total > self.max_bytes:
            raise StorageQuotaError(
                f"数据目录无法清理到容量上限：{total}>{self.max_bytes}"
            )

    def _effective_active_run(self) -> Path | None:
        if self._active_run is not None and self._active_run.is_dir():
            return self._active_run
        runs_root = self.root / "runs"
        runs = (
            [path for path in runs_root.iterdir() if path.is_dir()]
            if runs_root.is_dir()
            else []
        )
        return max(runs, key=lambda path: path.stat().st_mtime, default=None)

    def _effective_events(self, active_run: Path | None) -> Path | None:
        if self._active_events is not None and self._active_events.is_file():
            return self._active_events
        return None if active_run is None else active_run / "events.jsonl"

    def _completed_runs(self, active_run: Path | None) -> list[Path]:
        runs_root = self.root / "runs"
        if not runs_root.is_dir():
            return []
        runs = [
            path
            for path in runs_root.iterdir()
            if path.is_dir() and path.resolve() != active_run
        ]
        return sorted(runs, key=lambda path: path.stat().st_mtime)

    def _diagnostic_groups(self) -> list[list[Path]]:
        diagnostics = self.root / "diagnostics"
        groups: dict[str, list[Path]] = defaultdict(list)
        if not diagnostics.is_dir():
            return []
        for path in diagnostics.iterdir():
            if not path.is_file() or path.suffix not in {".png", ".json", ".jpg"}:
                continue
            stem = path.stem
            if path.suffix == ".jpg" and stem.endswith("_progress"):
                stem = stem[: -len("_progress")]
            groups[stem].append(path)
        return sorted(
            groups.values(),
            key=lambda files: max(path.stat().st_mtime for path in files),
        )

    def _trim_events(self, path: Path, budget: int) -> None:
        lines = path.read_bytes().splitlines(keepends=True)
        kept: list[bytes] = []
        used = 0
        for line in reversed(lines):
            if used + len(line) > budget:
                break
            kept.append(line)
            used += len(line)
        if lines and not kept:
            raise StorageQuotaError("最新事件行超过剩余容量预算")
        payload = b"".join(reversed(kept))
        temp = path.with_suffix(".quota.tmp")
        temp.write_bytes(payload)
        temp.replace(path)

    def _inside(self, path: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise StorageQuotaError(f"路径超出数据根目录：{resolved}") from error
        return resolved

    @staticmethod
    def _tree_bytes(root: Path) -> int:
        if not root.exists():
            return 0
        return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
