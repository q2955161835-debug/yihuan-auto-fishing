from datetime import datetime, timedelta, timezone
import json
import os
import threading

import cv2
import numpy as np
import pytest

from auto_fishing.model import FishingState, RuntimeSnapshot, SceneObservation
from auto_fishing.storage.diagnostics import DiagnosticsStore
from auto_fishing.storage.quota import StorageQuotaError, StorageQuotaManager
from auto_fishing.storage.runtime_logging import RuntimeLogError, RuntimeLogStore
from auto_fishing.storage.settings import AppSettings, SettingsStore


def write_sized(path, size, stamp):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (stamp, stamp))
    os.utime(path.parent, (stamp, stamp))


def tree_bytes(root):
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def test_quota_deletes_old_completed_run_before_diagnostics(tmp_path):
    root = tmp_path / "data"
    write_sized(root / "config.json", 5, 1)
    write_sized(root / "runs" / "run-old" / "events.jsonl", 20, 2)
    write_sized(root / "runs" / "run-new" / "events.jsonl", 20, 4)
    write_sized(root / "diagnostics" / "incident.json", 10, 3)

    StorageQuotaManager(root, max_bytes=35).initialize()

    assert not (root / "runs" / "run-old").exists()
    assert (root / "runs" / "run-new").is_dir()
    assert (root / "diagnostics" / "incident.json").is_file()
    assert (root / "config.json").is_file()
    assert tree_bytes(root) <= 35


def test_quota_deletes_oldest_diagnostic_group_atomically(tmp_path):
    root = tmp_path / "data"
    write_sized(root / "config.json", 5, 1)
    write_sized(root / "diagnostics" / "old.png", 10, 2)
    write_sized(root / "diagnostics" / "old.json", 10, 2)
    write_sized(root / "diagnostics" / "new.png", 10, 3)

    StorageQuotaManager(root, max_bytes=15).initialize()

    assert not (root / "diagnostics" / "old.png").exists()
    assert not (root / "diagnostics" / "old.json").exists()
    assert (root / "diagnostics" / "new.png").is_file()
    assert (root / "config.json").is_file()


def test_quota_keeps_recent_frames_from_newest_run(tmp_path):
    root = tmp_path / "data"
    write_sized(root / "config.json", 5, 1)
    events = root / "runs" / "run-new" / "events.jsonl"
    write_sized(events, 5, 2)
    write_sized(events.parent / "frames" / "00000001.jpg", 10, 3)
    write_sized(events.parent / "frames" / "00000002.jpg", 10, 4)

    StorageQuotaManager(root, max_bytes=20).initialize()

    assert not (events.parent / "frames" / "00000001.jpg").exists()
    assert (events.parent / "frames" / "00000002.jpg").is_file()
    assert events.is_file()


def test_quota_trims_old_event_lines_and_keeps_latest_complete_line(tmp_path):
    root = tmp_path / "data"
    write_sized(root / "config.json", 5, 1)
    events = root / "runs" / "run-new" / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_bytes(b'{"n":1}\n{"n":2}\n{"n":3}\n')

    StorageQuotaManager(root, max_bytes=13).initialize()

    assert events.read_bytes() == b'{"n":3}\n'
    assert tree_bytes(root) <= 13


def test_quota_counts_unknown_files_but_never_deletes_them(tmp_path):
    root = tmp_path / "data"
    write_sized(root / "config.json", 5, 1)
    unknown = root / "keep.bin"
    write_sized(unknown, 20, 2)

    with pytest.raises(StorageQuotaError, match="无法清理到容量上限"):
        StorageQuotaManager(root, max_bytes=10).initialize()

    assert unknown.is_file()
    assert (root / "config.json").is_file()


def test_quota_rejects_registered_path_outside_data_root(tmp_path):
    root = tmp_path / "data"
    outside = tmp_path / "outside.log"
    outside.write_bytes(b"x")
    quota = StorageQuotaManager(root, max_bytes=100)
    quota.initialize()

    with pytest.raises(StorageQuotaError, match="超出数据根目录"):
        quota.register_write(outside, 0)


def test_settings_reports_replacement_to_shared_quota(tmp_path):
    root = tmp_path / "data"
    quota = StorageQuotaManager(root, max_bytes=100)
    quota.initialize()
    store = SettingsStore(root / "config.json", quota=quota)

    store.save(AppSettings(target_count=9))

    assert quota.total_bytes == (root / "config.json").stat().st_size


def test_diagnostics_write_enforces_entire_directory_quota(tmp_path):
    root = tmp_path / "data"
    write_sized(root / "runs" / "run-old" / "events.jsonl", 80, 1)
    quota = StorageQuotaManager(root, max_bytes=90)
    quota.initialize()
    store = DiagnosticsStore(root / "diagnostics", quota=quota)

    store.save(np.zeros((20, 20, 3), dtype=np.uint8), "E_TEST", "quota")

    assert not (root / "runs" / "run-old").exists()
    assert quota.total_bytes <= 90


def test_runtime_writer_prunes_oldest_active_frame_when_quota_is_full(tmp_path):
    root = tmp_path / "data"
    quota = StorageQuotaManager(root, max_bytes=5000)
    quota.initialize()
    store = RuntimeLogStore(root / "runs", queue_size=10, quota=quota)
    run_dir = store.start()
    for index in range(8):
        store.record_frame(
            np.full((120, 160, 3), index, dtype=np.uint8),
            observation=SceneObservation(),
            state_before=FishingState.READY,
            snapshot=RuntimeSnapshot(FishingState.READY, 0, 1, 30.0),
            frame_timestamp=float(index),
            now_monotonic=float(index),
        )
    store.close()
    store.raise_if_failed()

    frames = sorted((run_dir / "frames").glob("*.jpg"))
    assert frames
    assert frames[-1].name == "00000008.jpg"
    assert frames[0].name != "00000001.jpg"
    assert quota.total_bytes <= 5000


def test_settings_round_trip(tmp_path):
    store = SettingsStore(tmp_path / "config.json")
    expected = AppSettings(target_count=8, window_x=12, window_y=34)

    store.save(expected)

    assert store.load() == expected


def test_settings_auto_activate_defaults_true_and_round_trips(tmp_path) -> None:
    store = SettingsStore(tmp_path / "config.json")

    assert store.load().auto_activate_game is True

    store.save(AppSettings(auto_activate_game=False))

    assert store.load().auto_activate_game is False


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_settings_reject_non_boolean_auto_activate(tmp_path, value) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"auto_activate_game": value}), "utf-8")

    assert SettingsStore(path).load().auto_activate_game is True


@pytest.mark.parametrize("root_value", [[], "invalid", None])
def test_settings_non_object_json_returns_defaults(tmp_path, root_value):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(root_value), "utf-8")

    assert SettingsStore(path).load() == AppSettings()


@pytest.mark.parametrize("token", ["1e10000", "NaN", "Infinity"])
def test_settings_rejects_overflowing_or_non_finite_numbers(tmp_path, token):
    path = tmp_path / "config.json"
    path.write_text(f'{{"target_count": {token}}}', "utf-8")

    assert SettingsStore(path).load() == AppSettings()


def test_diagnostics_delete_old_and_keep_twenty_groups(tmp_path):
    store = DiagnosticsStore(tmp_path / "diagnostics")
    now = datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc)
    frame = np.zeros((20, 20, 3), dtype=np.uint8)
    screenshots_dir = tmp_path / "流程截图"
    screenshots_dir.mkdir()
    sentinel_contents = {
        screenshots_dir / "sentinel.png": b"not a diagnostic image",
        screenshots_dir / "sentinel.json": b'{"keep": true}',
        screenshots_dir / "sentinel.txt": b"keep this too",
    }
    for path, content in sentinel_contents.items():
        path.write_bytes(content)
    for index in range(25):
        store.save(frame, "E_TEST", str(index), now - timedelta(hours=index))
    store.save(frame, "E_OLD", "old", now - timedelta(days=8))

    store.cleanup(now)

    groups = {path.stem for path in (tmp_path / "diagnostics").iterdir()}
    assert len(groups) == 20
    assert not any(
        "E_OLD" in path.name for path in (tmp_path / "diagnostics").iterdir()
    )
    assert screenshots_dir.is_dir()
    assert {path: path.read_bytes() for path in sentinel_contents} == sentinel_contents


def test_diagnostic_metadata_contains_no_frame_bytes(tmp_path):
    store = DiagnosticsStore(tmp_path / "diagnostics")
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)

    store.save(
        np.zeros((4, 4, 3), dtype=np.uint8), "E_INPUT", "发送失败", now
    )

    data = json.loads(
        next((tmp_path / "diagnostics").glob("*.json")).read_text("utf-8")
    )
    assert set(data) == {"code", "detail", "created_at"}


def test_diagnostics_save_screenshot_under_unicode_directory(tmp_path):
    store = DiagnosticsStore(tmp_path / "异环自动钓鱼" / "diagnostics")

    store.save(np.full((4, 4, 3), 127, dtype=np.uint8), "E_VISION", "测试")

    image_path = next(store.root.glob("*.png"))
    image = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    assert image.shape == (4, 4, 3)


def test_diagnostics_same_timestamp_and_code_create_distinct_groups(tmp_path):
    store = DiagnosticsStore(tmp_path / "diagnostics")
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)
    frame = np.zeros((4, 4, 3), dtype=np.uint8)

    first = store.save(frame, "E_INPUT", "first", now)
    second = store.save(frame, "E_INPUT", "second", now)

    assert first != second
    assert len(list((tmp_path / "diagnostics").glob("*.png"))) == 2
    assert len(list((tmp_path / "diagnostics").glob("*.json"))) == 2


def test_diagnostics_save_twelve_progress_frames_as_contact_sheet(
    tmp_path,
) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics")
    frames = [
        np.full((24, 120, 3), index, dtype=np.uint8)
        for index in range(12)
    ]

    stem = store.save(
        np.zeros((60, 80, 3), dtype=np.uint8),
        "E_PROGRESS_LOST",
        "lost",
        progress_frames=frames,
    )

    path = tmp_path / "diagnostics" / f"{stem}_progress.jpg"
    sheet = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert sheet.shape[:2] == (24 * 3, 120 * 4)


def test_diagnostics_cleanup_removes_progress_contact_sheet_with_incident(
    tmp_path,
) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics")
    old = datetime(2026, 7, 1, tzinfo=timezone.utc)
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    store.save(
        np.zeros((60, 80, 3), dtype=np.uint8),
        "E_PROGRESS_LOST",
        "lost",
        old,
        progress_frames=[np.zeros((24, 120, 3), dtype=np.uint8)],
    )

    store.cleanup(now)

    assert list((tmp_path / "diagnostics").iterdir()) == []


def test_runtime_log_writes_jsonl_and_480px_jpeg(tmp_path):
    store = RuntimeLogStore(tmp_path / "runs", queue_size=3)
    run_dir = store.start()
    store.event("application.started", pid=123)
    store.record_frame(
        np.zeros((1080, 1920, 3), dtype=np.uint8),
        observation=SceneObservation(
            ready=True,
            progress_scanlines=2,
            progress_candidates=1,
            progress_rejection="jump_pending",
        ),
        state_before=FishingState.READY,
        snapshot=RuntimeSnapshot(FishingState.WAIT_BITE, 0, 1, 30.0),
        frame_timestamp=10.0,
        now_monotonic=10.01,
    )
    store.close()

    entries = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text("utf-8").splitlines()
    ]
    image = cv2.imread(str(run_dir / "frames" / "00000001.jpg"))
    assert entries[0]["event"] == "application.started"
    assert entries[-1]["event"] == "frame.processed"
    assert entries[-1]["progress_scanlines"] == 2
    assert entries[-1]["progress_candidates"] == 1
    assert entries[-1]["progress_rejection"] == "jump_pending"
    assert max(image.shape[:2]) == 480


def test_runtime_log_cleanup_keeps_newest_thirty_runs(tmp_path):
    root = tmp_path / "runs"
    base = datetime(2026, 7, 11, tzinfo=timezone.utc)
    for index in range(31):
        run = root / f"run-{index:02d}"
        run.mkdir(parents=True)
        (run / "events.jsonl").write_text("", "utf-8")
        stamp = (base + timedelta(seconds=index)).timestamp()
        os.utime(run, (stamp, stamp))

    RuntimeLogStore(root).cleanup()

    assert sorted(path.name for path in root.iterdir()) == [
        f"run-{index:02d}" for index in range(1, 31)
    ]


def test_runtime_log_cleanup_never_traverses_outside_root(tmp_path):
    root = tmp_path / "runs"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", "utf-8")
    (root / "not-a-run.txt").write_text("ignore", "utf-8")

    RuntimeLogStore(root).cleanup()

    assert sentinel.read_text("utf-8") == "keep"
    assert (root / "not-a-run.txt").is_file()


def test_runtime_log_queue_full_surfaces_runtime_log_error(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingStore(RuntimeLogStore):
        def _write_item(self, item):
            entered.set()
            assert release.wait(timeout=1)
            super()._write_item(item)

    store = BlockingStore(tmp_path / "runs", queue_size=1)
    store.start()
    store.event("first")
    assert entered.wait(timeout=1)
    store.event("second")
    store.event("third")
    with pytest.raises(RuntimeLogError, match="日志队列已满"):
        store.raise_if_failed()
    release.set()
    store.close()
