from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from zipfile import ZipFile

import cv2
import numpy as np

from auto_fishing.model import (
    FishingState,
    RuntimeSnapshot,
    SceneObservation,
)
from auto_fishing.storage.memory_diagnostics import MemoryDiagnosticRecorder
from auto_fishing.storage.diagnostic_bundles import (
    DiagnosticBundleService,
    NullDiagnosticsStore,
)


def test_memory_recorder_samples_ten_fps_for_the_latest_ten_seconds(
    tmp_path,
) -> None:
    clock = [0.0]
    recorder = MemoryDiagnosticRecorder(
        clock=lambda: clock[0],
        now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    recorder.start()

    for index in range(121):
        clock[0] = index / 10
        recorder.event("tick", index=index)
        recorder.record_frame(
            np.zeros((720, 1280, 3), dtype=np.uint8),
            observation=SceneObservation(),
            state_before=FishingState.CONTROL,
            snapshot=RuntimeSnapshot(FishingState.CONTROL, 0, 1, 30.0),
            frame_timestamp=clock[0],
            now_monotonic=clock[0],
        )

    snapshot = recorder.snapshot()
    assert len(snapshot.frames) == 101
    assert snapshot.frames[0].monotonic >= 2.0
    decoded = cv2.imdecode(
        np.frombuffer(snapshot.frames[-1].jpeg, dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    assert decoded is not None
    assert max(decoded.shape[:2]) == 480
    assert list(tmp_path.iterdir()) == []


def test_memory_recorder_keeps_twenty_seconds_of_events() -> None:
    clock = [0.0]
    recorder = MemoryDiagnosticRecorder(clock=lambda: clock[0])
    recorder.event("progress.control", direction="left")

    clock[0] = 15.0
    recorder.event("manual.report")

    assert [event["event"] for event in recorder.snapshot().events] == [
        "progress.control",
        "manual.report",
    ]

    clock[0] = 21.0
    recorder.event("later.event")

    assert [event["event"] for event in recorder.snapshot().events] == [
        "manual.report",
        "later.event",
    ]


def _populated_recorder() -> MemoryDiagnosticRecorder:
    recorder = MemoryDiagnosticRecorder(
        clock=lambda: 1.0,
        now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    recorder.event("test.event", value=1)
    recorder.record_frame(
        np.zeros((720, 1280, 3), dtype=np.uint8),
        observation=SceneObservation(),
        state_before=FishingState.CONTROL,
        snapshot=RuntimeSnapshot(FishingState.CONTROL, 0, 1, 30.0),
        frame_timestamp=1.0,
        now_monotonic=1.0,
    )
    return recorder


def test_bundle_contains_metadata_events_frames_and_error_image(tmp_path) -> None:
    service = DiagnosticBundleService(
        tmp_path / "诊断",
        recorder=_populated_recorder(),
        version="2.0.0",
        now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
        system_info=lambda: {"windows": "test"},
    )

    result = service.request_report(
        report_type="automatic",
        code="E_VISION",
        detail="识别失败",
        state="已暂停",
        frame=np.zeros((1080, 1920, 3), dtype=np.uint8),
        context={"client_rect": [0, 0, 1920, 1080]},
    ).result(timeout=5)
    service.close()

    assert result.error is None
    assert result.path is not None
    with ZipFile(result.path) as archive:
        names = set(archive.namelist())
        assert {"error.json", "events.jsonl", "error.jpg"} <= names
        assert any(name.startswith("frames/") for name in names)
        metadata = json.loads(archive.read("error.json"))
        assert metadata["version"] == "2.0.0"
        assert metadata["code"] == "E_VISION"
        assert metadata["screenshot_available"] is True
        assert metadata["client_rect"] == [0, 0, 1920, 1080]
        assert b'"event":"test.event"' in archive.read("events.jsonl")


def test_report_snapshot_and_frame_are_frozen_when_requested(tmp_path) -> None:
    recorder = _populated_recorder()
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    service = DiagnosticBundleService(
        tmp_path / "diagnostics",
        recorder=recorder,
        version="2.0.0",
        now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
        system_info=lambda: {},
    )
    future = service.request_report(
        report_type="manual_report",
        code="MANUAL_REPORT",
        detail="用户主动报告错误",
        state="未绑定",
        frame=frame,
        context={},
    )
    frame[:] = 255
    recorder.event("late.event")
    result = future.result(timeout=5)
    service.close()

    assert result.path is not None
    with ZipFile(result.path) as archive:
        assert b"late.event" not in archive.read("events.jsonl")
        image = cv2.imdecode(
            np.frombuffer(archive.read("error.jpg"), dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        assert image is not None
        assert float(image.mean()) < 5


def test_report_without_frame_records_unavailable_screenshot(tmp_path) -> None:
    service = DiagnosticBundleService(
        tmp_path / "diagnostics",
        recorder=MemoryDiagnosticRecorder(),
        version="2.0.0",
        system_info=lambda: {},
    )
    result = service.request_report(
        report_type="manual_report",
        code="MANUAL_REPORT",
        detail="用户主动报告错误",
        state="未绑定",
        frame=None,
        context={},
    ).result(timeout=5)
    service.close()

    assert result.path is not None
    with ZipFile(result.path) as archive:
        metadata = json.loads(archive.read("error.json"))
        assert metadata["screenshot_available"] is False
        assert "error.jpg" not in archive.namelist()


def test_bundle_retains_only_five_matching_zip_files(tmp_path) -> None:
    root = tmp_path / "diagnostics"
    root.mkdir()
    unrelated = root / "unrelated.zip"
    unrelated.write_bytes(b"unrelated")
    tick = [0]

    def increasing_now() -> datetime:
        tick[0] += 1
        return datetime(2026, 7, 14, 0, 0, tick[0], tzinfo=timezone.utc)

    service = DiagnosticBundleService(
        root,
        recorder=MemoryDiagnosticRecorder(),
        version="2.0.0",
        now=increasing_now,
        system_info=lambda: {},
    )
    paths: list[Path] = []
    for _ in range(6):
        result = service.request_report(
            report_type="manual_report",
            code="MANUAL_REPORT",
            detail="用户主动报告错误",
            state="未绑定",
            frame=None,
            context={},
        ).result(timeout=5)
        assert result.path is not None
        paths.append(result.path)
        time.sleep(0.01)
    service.close()

    assert not paths[0].exists()
    assert len(list(root.glob("yihuan-v2-*.zip"))) == 5
    assert unrelated.exists()
    assert list(root.glob("*.tmp")) == []


def test_open_location_selects_existing_zip_and_rejects_missing(tmp_path) -> None:
    calls: list[list[str]] = []
    service = DiagnosticBundleService(
        tmp_path,
        recorder=MemoryDiagnosticRecorder(),
        version="2.0.0",
        system_info=lambda: {},
        popen=lambda arguments, **_kwargs: calls.append(arguments),
    )
    report = tmp_path / "报告.zip"
    report.write_bytes(b"zip")

    service.open_location(report)

    assert calls == [["explorer.exe", f"/select,{report.resolve()}"]]
    missing = tmp_path / "missing.zip"
    try:
        service.open_location(missing)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("缺失报告必须拒绝打开")


def test_null_diagnostics_store_never_creates_files(tmp_path) -> None:
    store = NullDiagnosticsStore()
    store.cleanup()
    assert store.save(np.zeros((1, 1, 3), dtype=np.uint8), "E", "x") == ""
    assert list(tmp_path.iterdir()) == []
