from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path


@dataclass(frozen=True)
class AppSettings:
    target_count: int = 1
    window_x: int = 20
    window_y: int = 20
    auto_activate_game: bool = True


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> AppSettings:
        if not self.path.exists():
            return AppSettings()
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            if not isinstance(raw, dict):
                return AppSettings()
            count = min(999, max(1, _finite_int(raw.get("target_count", 1))))
            return AppSettings(
                target_count=count,
                window_x=_finite_int(raw.get("window_x", 20)),
                window_y=_finite_int(raw.get("window_y", 20)),
                auto_activate_game=_strict_bool(
                    raw.get("auto_activate_game", True)
                ),
            )
        except (
            OSError,
            OverflowError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            return AppSettings()

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(asdict(settings), ensure_ascii=False, indent=2), "utf-8"
        )
        temp.replace(self.path)


def _finite_int(value: object) -> int:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("setting value must be finite")
    return int(value)


def _strict_bool(value: object, default: bool = True) -> bool:
    return value if isinstance(value, bool) else default
