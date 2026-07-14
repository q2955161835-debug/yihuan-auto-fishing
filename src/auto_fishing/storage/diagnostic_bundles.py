from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import platform
import struct
import subprocess
import threading
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import numpy as np

from .memory_diagnostics import DiagnosticSnapshot, MemoryDiagnosticRecorder
from .recording import encode_jpeg


@dataclass(frozen=True)
class DiagnosticReportResult:
    path: Path | None
    error: str | None


@dataclass(frozen=True)
class _DiagnosticReportRequest:
    report_type: str
    code: str
    detail: str
    state: str
    frame: np.ndarray | None
    context: dict[str, Any]
    snapshot: DiagnosticSnapshot
    created_at: datetime


class NullDiagnosticsStore:
    def cleanup(self) -> None:
        return None

    def save(self, *_args: Any, **_kwargs: Any) -> str:
        return ""


class DiagnosticBundleService:
    _KEEP_REPORTS = 5
    _PATTERN = "yihuan-v2-*.zip"

    def __init__(
        self,
        root: Path,
        *,
        recorder: MemoryDiagnosticRecorder,
        version: str,
        now: Callable[[], datetime] | None = None,
        system_info: Callable[[], Mapping[str, Any]] | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
        executor: ThreadPoolExecutor | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.root = root.resolve()
        self.recorder = recorder
        self.version = version
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._system_info = system_info or _default_system_info
        self._popen = popen
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="diagnostic-bundle",
        )
        self._callbacks: list[Callable[[DiagnosticReportResult], None]] = []
        self._futures: set[Future[DiagnosticReportResult]] = set()
        self._lock = threading.RLock()
        self._closed = False
        self.logger = logger or logging.getLogger(__name__)

    def subscribe(
        self,
        callback: Callable[[DiagnosticReportResult], None],
    ) -> None:
        with self._lock:
            self._callbacks.append(callback)

    def request_report(
        self,
        *,
        report_type: str,
        code: str,
        detail: str,
        state: str,
        frame: np.ndarray | None,
        context: Mapping[str, Any],
    ) -> Future[DiagnosticReportResult]:
        request = _DiagnosticReportRequest(
            report_type=report_type,
            code=code,
            detail=detail,
            state=state,
            frame=None if frame is None else np.ascontiguousarray(frame).copy(),
            context=dict(context),
            snapshot=self.recorder.snapshot(),
            created_at=self._aware_now(),
        )
        with self._lock:
            if self._closed:
                future: Future[DiagnosticReportResult] = Future()
                future.set_result(
                    DiagnosticReportResult(None, "诊断服务已经关闭")
                )
                return future
            future = self._executor.submit(self._write_bundle, request)
            self._futures.add(future)
            future.add_done_callback(self._future_done)
            return future

    def open_location(self, path: Path) -> None:
        resolved = path.resolve(strict=True)
        self._popen(
            ["explorer.exe", f"/select,{resolved}"],
            close_fds=True,
        )

    def close(self, timeout: float = 2.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            futures = tuple(self._futures)
        if futures:
            wait(futures, timeout=max(0.0, timeout))
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _future_done(self, future: Future[DiagnosticReportResult]) -> None:
        with self._lock:
            self._futures.discard(future)

    def _write_bundle(
        self,
        request: _DiagnosticReportRequest,
    ) -> DiagnosticReportResult:
        self.root.mkdir(parents=True, exist_ok=True)
        final_path = self._unique_path(request.created_at, request.code)
        temp_path = final_path.with_suffix(".tmp")
        try:
            with ZipFile(temp_path, "w", ZIP_DEFLATED) as archive:
                archive.writestr(
                    "error.json",
                    json.dumps(
                        self._metadata(request),
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
                archive.writestr(
                    "events.jsonl",
                    "".join(
                        json.dumps(
                            event,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                        for event in request.snapshot.events
                    ),
                )
                for buffered in request.snapshot.frames:
                    archive.writestr(
                        f"frames/{buffered.name}",
                        buffered.jpeg,
                    )
                if request.frame is not None:
                    archive.writestr(
                        "error.jpg",
                        encode_jpeg(
                            request.frame,
                            max_edge=1280,
                            quality=75,
                        ),
                    )
            temp_path.replace(final_path)
            self._cleanup_keep_five()
            result = DiagnosticReportResult(final_path, None)
        except Exception as error:
            temp_path.unlink(missing_ok=True)
            result = DiagnosticReportResult(None, str(error))
        self._publish(result)
        return result

    def _metadata(self, request: _DiagnosticReportRequest) -> dict[str, Any]:
        return {
            **dict(self._system_info()),
            **request.context,
            "version": self.version,
            "report_type": request.report_type,
            "code": request.code,
            "detail": request.detail,
            "state": request.state,
            "created_at": request.created_at.isoformat(),
            "screenshot_available": request.frame is not None,
            "diagnostic_dropped_items": request.snapshot.dropped_items,
        }

    def _unique_path(self, created_at: datetime, code: str) -> Path:
        safe_code = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in code
        ) or "ERROR"
        base = f"yihuan-v2-{created_at.strftime('%Y%m%dT%H%M%S%fZ')}-{safe_code}"
        candidate = self.root / f"{base}.zip"
        suffix = 1
        while candidate.exists() or candidate.with_suffix(".tmp").exists():
            candidate = self.root / f"{base}-{suffix}.zip"
            suffix += 1
        return candidate

    def _cleanup_keep_five(self) -> None:
        candidates = []
        for path in self.root.glob(self._PATTERN):
            resolved = path.resolve()
            if path.is_file() and resolved.parent == self.root:
                candidates.append((path.stat().st_mtime_ns, path.name, path))
        for _mtime, _name, path in sorted(candidates, reverse=True)[
            self._KEEP_REPORTS :
        ]:
            path.unlink(missing_ok=True)

    def _publish(self, result: DiagnosticReportResult) -> None:
        with self._lock:
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            try:
                callback(result)
            except Exception as error:
                self.logger.warning("诊断结果回调失败: %s", error)

    def _aware_now(self) -> datetime:
        now = self._now()
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)


def _default_system_info() -> dict[str, Any]:
    return {
        "windows": platform.platform(),
        "windows_release": platform.release(),
        "process_bits": struct.calcsize("P") * 8,
    }
