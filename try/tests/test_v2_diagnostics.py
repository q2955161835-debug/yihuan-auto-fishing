from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from zipfile import ZipFile

import cv2
import numpy as np

import auto_fishing.storage.memory_diagnostics as memory_diagnostics
from auto_fishing.model import (
    FishingState,
    Rect,
    RuntimeSnapshot,
    SceneObservation,
)
from auto_fishing.vision.geometry import crop_normalized
from auto_fishing.vision.progress import ProgressRecognizer
from auto_fishing.vision.regions import TOP_ROI
from auto_fishing.storage.memory_diagnostics import MemoryDiagnosticRecorder
from auto_fishing.storage.diagnostic_bundles import (
    DiagnosticBundleService,
    NullDiagnosticsStore,
)


def test_memory_recorder_samples_ten_fps_for_the_latest_thirty_seconds(
    tmp_path,
) -> None:
    clock = [0.0]
    recorder = MemoryDiagnosticRecorder(
        clock=lambda: clock[0],
        now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    recorder.start()

    for index in range(321):
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
    assert len(snapshot.frames) == 301
    assert snapshot.frames[0].monotonic >= 2.0
    decoded = cv2.imdecode(
        np.frombuffer(snapshot.frames[-1].jpeg, dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    assert decoded is not None
    assert max(decoded.shape[:2]) == 480
    assert list(tmp_path.iterdir()) == []


def test_memory_recorder_keeps_thirty_seconds_of_events_with_sequence() -> None:
    clock = [0.0]
    recorder = MemoryDiagnosticRecorder(clock=lambda: clock[0])
    recorder.event("progress.control", direction="left")

    clock[0] = 25.0
    recorder.event("manual.report")

    assert [event["event"] for event in recorder.snapshot().events] == [
        "progress.control",
        "manual.report",
    ]
    assert [event["sequence"] for event in recorder.snapshot().events] == [1, 2]

    clock[0] = 30.001
    recorder.event("later.event")

    assert [event["event"] for event in recorder.snapshot().events] == [
        "manual.report",
        "later.event",
    ]
    assert [event["sequence"] for event in recorder.snapshot().events] == [2, 3]


def test_progress_band_is_lossless_native_width_and_sampled_at_ten_fps() -> None:
    clock = [0.0]
    recorder = MemoryDiagnosticRecorder(clock=lambda: clock[0])
    source = np.zeros((720, 1280, 3), dtype=np.uint8)
    top_rect = TOP_ROI.to_pixels(Rect(0, 0, 1280, 720))
    source[top_rect.top : top_rect.bottom, top_rect.left : top_rect.right] = (
        17,
        123,
        231,
    )
    diagnostics = ProgressRecognizer().analyze(
        np.zeros((120, 300, 3), dtype=np.uint8),
        0.0,
    ).diagnostics
    observation = SceneObservation(progress_diagnostics=diagnostics)

    for index in range(31):
        clock[0] = index / 30
        recorder.record_frame(
            source,
            observation=observation,
            state_before=FishingState.CONTROL,
            snapshot=RuntimeSnapshot(FishingState.CONTROL, 0, 1, 30.0),
            frame_timestamp=clock[0],
            now_monotonic=clock[0],
        )

    snapshot = recorder.snapshot()
    assert 10 <= len(snapshot.progress_frames) <= 11
    assert len(snapshot.progress_traces) == 31
    decoded = cv2.imdecode(
        np.frombuffer(snapshot.progress_frames[-1].png, dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    top = crop_normalized(source, TOP_ROI)
    expected = top[round(top.shape[0] * 0.40) : round(top.shape[0] * 0.52)]
    assert decoded is not None
    assert decoded.shape == expected.shape
    assert np.array_equal(decoded, expected)
    assert snapshot.progress_traces[-1]["frame_index"] == 31
    assert snapshot.progress_traces[-1]["progress"]["image_width"] == 300


def test_progress_png_encoding_failure_is_counted_without_escaping(
    monkeypatch,
) -> None:
    recorder = MemoryDiagnosticRecorder(clock=lambda: 1.0)
    diagnostics = ProgressRecognizer().analyze(
        np.zeros((120, 300, 3), dtype=np.uint8),
        1.0,
    ).diagnostics

    def fail_encode(_image, *, compression=3):
        raise OSError("PNG 编码失败")

    monkeypatch.setattr(memory_diagnostics, "encode_png", fail_encode)

    frame_index = recorder.record_frame(
        np.zeros((720, 1280, 3), dtype=np.uint8),
        observation=SceneObservation(progress_diagnostics=diagnostics),
        state_before=FishingState.CONTROL,
        snapshot=RuntimeSnapshot(FishingState.CONTROL, 0, 1, 30.0),
        frame_timestamp=1.0,
        now_monotonic=1.0,
    )

    snapshot = recorder.snapshot()
    assert frame_index == 1
    assert len(snapshot.events) == 1
    assert len(snapshot.progress_traces) == 1
    assert snapshot.progress_frames == ()
    assert snapshot.drop_counts["progress_frames"] == 1


def test_progress_trace_snapshot_is_deeply_frozen() -> None:
    recorder = MemoryDiagnosticRecorder(clock=lambda: 1.0)
    diagnostics = ProgressRecognizer().analyze(
        np.zeros((120, 300, 3), dtype=np.uint8),
        1.0,
    ).diagnostics
    recorder.record_frame(
        np.zeros((720, 1280, 3), dtype=np.uint8),
        observation=SceneObservation(progress_diagnostics=diagnostics),
        state_before=FishingState.CONTROL,
        snapshot=RuntimeSnapshot(FishingState.CONTROL, 0, 1, 30.0),
        frame_timestamp=1.0,
        now_monotonic=1.0,
    )

    first = recorder.snapshot()
    first.progress_traces[0]["progress"]["image_width"] = 999

    assert recorder.snapshot().progress_traces[0]["progress"]["image_width"] == 300


def _populated_recorder() -> MemoryDiagnosticRecorder:
    recorder = MemoryDiagnosticRecorder(
        clock=lambda: 1.0,
        now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    recorder.event("test.event", value=1)
    recorder.event(
        "progress.control",
        frame_timestamp=1.0,
        direction="left",
        instantaneous_error=0.5,
        weighted_error=0.4,
    )
    diagnostics = ProgressRecognizer().analyze(
        np.zeros((120, 300, 3), dtype=np.uint8),
        1.0,
    ).diagnostics
    recorder.record_frame(
        np.zeros((720, 1280, 3), dtype=np.uint8),
        observation=SceneObservation(progress_diagnostics=diagnostics),
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
        executable_info=lambda: {
            "frozen": True,
            "executable_name": "app.exe",
            "executable_size": 1,
            "executable_sha256": "abc123",
        },
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
        assert {
            "error.json",
            "events.jsonl",
            "progress/trace.jsonl",
            "error.jpg",
        } <= names
        assert any(name.startswith("frames/") for name in names)
        assert any(name.startswith("progress/frames/") for name in names)
        metadata = json.loads(archive.read("error.json"))
        assert metadata["diagnostic_schema_version"] == 2
        assert metadata["version"] == "2.0.0"
        assert metadata["code"] == "E_VISION"
        assert metadata["screenshot_available"] is True
        assert metadata["client_rect"] == [0, 0, 1920, 1080]
        assert metadata["coverage"]["events"]["count"] == 3
        assert metadata["coverage"]["progress_traces"]["count"] == 1
        assert metadata["diagnostic_drop_counts"] == {
            "context_frames": 0,
            "progress_frames": 0,
            "progress_traces": 0,
        }
        assert metadata["frozen"] is True
        assert metadata["executable_name"] == "app.exe"
        assert metadata["executable_size"] == 1
        assert metadata["executable_sha256"] == "abc123"
        assert metadata["recent_control"]["frame_timestamp"] == 1.0
        assert metadata["recent_control"]["direction"] == "left"
        assert b'"event":"test.event"' in archive.read("events.jsonl")
        assert b'"frame_index":1' in archive.read("progress/trace.jsonl")


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


def test_executable_hash_failure_is_metadata_not_bundle_failure(tmp_path) -> None:
    def fail_executable_info():
        raise OSError("无法读取发布物")

    service = DiagnosticBundleService(
        tmp_path / "diagnostics",
        recorder=MemoryDiagnosticRecorder(),
        version="2.0.0",
        system_info=lambda: {},
        executable_info=fail_executable_info,
    )

    result = service.request_report(
        report_type="automatic",
        code="E_VISION",
        detail="识别失败",
        state="已暂停",
        frame=None,
        context={},
    ).result(timeout=5)
    service.close()

    assert result.error is None
    assert result.path is not None
    with ZipFile(result.path) as archive:
        metadata = json.loads(archive.read("error.json"))
        assert metadata["executable_hash_error"] == "无法读取发布物"


def test_manual_report_coalesces_adjacent_automatic_window_report(tmp_path) -> None:
    clock = [0.0]
    root = tmp_path / "diagnostics"
    service = DiagnosticBundleService(
        root,
        recorder=MemoryDiagnosticRecorder(clock=lambda: clock[0]),
        version="2.0.0",
        system_info=lambda: {},
        executable_info=lambda: {"frozen": False},
        clock=lambda: clock[0],
    )
    automatic = service.request_report(
        report_type="automatic",
        code="E_WINDOW",
        detail="窗口失去前台",
        state="已暂停",
        frame=None,
        context={},
    ).result(timeout=5)
    clock[0] = 0.5
    manual = service.request_report(
        report_type="manual_report",
        code="MANUAL_REPORT",
        detail="用户主动报告错误",
        state="已暂停",
        frame=None,
        context={},
    ).result(timeout=5)
    service.close()

    assert automatic.path is not None
    assert manual.path is not None
    assert not automatic.path.exists()
    assert manual.path.exists()
    assert list(root.glob("*.zip")) == [manual.path]


def test_failed_manual_report_preserves_adjacent_automatic_window_report(
    tmp_path,
) -> None:
    clock = [0.0]
    root = tmp_path / "diagnostics"
    service = DiagnosticBundleService(
        root,
        recorder=MemoryDiagnosticRecorder(clock=lambda: clock[0]),
        version="2.0.0",
        system_info=lambda: {},
        executable_info=lambda: {"frozen": False},
        clock=lambda: clock[0],
    )
    automatic = service.request_report(
        report_type="automatic",
        code="E_WINDOW",
        detail="窗口失去前台",
        state="已暂停",
        frame=None,
        context={},
    ).result(timeout=5)
    clock[0] = 0.5
    manual = service.request_report(
        report_type="manual_report",
        code="MANUAL_REPORT",
        detail="用户主动报告错误",
        state="已暂停",
        frame=np.empty((0, 0, 3), dtype=np.uint8),
        context={},
    ).result(timeout=5)
    service.close()

    assert automatic.path is not None
    assert automatic.path.exists()
    assert manual.path is None
    assert manual.error
    assert list(root.glob("*.zip")) == [automatic.path]


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
