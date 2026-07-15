from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import os
from pathlib import Path


V1_DATA_DIR = Path(r"D:\29551\异环自动钓鱼数据")
V2_VERSION = "2.0.4"


@dataclass(frozen=True)
class ProductProfile:
    version: str
    window_title: str
    data_dir: Path
    use_disk_runtime_log: bool
    use_bundle_diagnostics: bool

    def with_data_dir(self, data_dir: Path) -> ProductProfile:
        return replace(self, data_dir=data_dir)


def v1_profile(data_dir: Path = V1_DATA_DIR) -> ProductProfile:
    return ProductProfile(
        version="1.0.0",
        window_title="异环自动钓鱼",
        data_dir=data_dir,
        use_disk_runtime_log=True,
        use_bundle_diagnostics=False,
    )


def v2_profile(
    environ: Mapping[str, str] | None = None,
) -> ProductProfile:
    values = os.environ if environ is None else environ
    local_app_data = values.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA 未配置，无法确定 V2 数据目录")
    return ProductProfile(
        version=V2_VERSION,
        window_title="异环自动钓鱼 V2",
        data_dir=Path(local_app_data) / "异环自动钓鱼V2",
        use_disk_runtime_log=False,
        use_bundle_diagnostics=True,
    )
