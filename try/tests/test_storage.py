from datetime import datetime, timedelta, timezone
import json

import numpy as np
import pytest

from auto_fishing.storage.diagnostics import DiagnosticsStore
from auto_fishing.storage.settings import AppSettings, SettingsStore


def test_settings_round_trip(tmp_path):
    store = SettingsStore(tmp_path / "config.json")
    expected = AppSettings(target_count=8, window_x=12, window_y=34)

    store.save(expected)

    assert store.load() == expected


@pytest.mark.parametrize("root_value", [[], "invalid", None])
def test_settings_non_object_json_returns_defaults(tmp_path, root_value):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(root_value), "utf-8")

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


def test_diagnostics_same_timestamp_and_code_create_distinct_groups(tmp_path):
    store = DiagnosticsStore(tmp_path / "diagnostics")
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)
    frame = np.zeros((4, 4, 3), dtype=np.uint8)

    first = store.save(frame, "E_INPUT", "first", now)
    second = store.save(frame, "E_INPUT", "second", now)

    assert first != second
    assert len(list((tmp_path / "diagnostics").glob("*.png"))) == 2
    assert len(list((tmp_path / "diagnostics").glob("*.json"))) == 2
