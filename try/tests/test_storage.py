from datetime import datetime, timedelta, timezone
import json

import numpy as np

from auto_fishing.storage.diagnostics import DiagnosticsStore
from auto_fishing.storage.settings import AppSettings, SettingsStore


def test_settings_round_trip(tmp_path):
    store = SettingsStore(tmp_path / "config.json")
    expected = AppSettings(target_count=8, window_x=12, window_y=34)

    store.save(expected)

    assert store.load() == expected


def test_diagnostics_delete_old_and_keep_twenty_groups(tmp_path):
    store = DiagnosticsStore(tmp_path / "diagnostics")
    now = datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc)
    frame = np.zeros((20, 20, 3), dtype=np.uint8)
    for index in range(25):
        store.save(frame, "E_TEST", str(index), now - timedelta(hours=index))
    store.save(frame, "E_OLD", "old", now - timedelta(days=8))

    store.cleanup(now)

    groups = {path.stem for path in (tmp_path / "diagnostics").iterdir()}
    assert len(groups) == 20
    assert not any(
        "E_OLD" in path.name for path in (tmp_path / "diagnostics").iterdir()
    )
    assert not (tmp_path / "流程截图").exists()


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
