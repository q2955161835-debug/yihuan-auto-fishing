from dataclasses import asdict, dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class AppSettings:
    target_count: int = 1
    window_x: int = 20
    window_y: int = 20


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
            count = min(999, max(1, int(raw.get("target_count", 1))))
            return AppSettings(
                count,
                int(raw.get("window_x", 20)),
                int(raw.get("window_y", 20)),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return AppSettings()

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(asdict(settings), ensure_ascii=False, indent=2), "utf-8"
        )
        temp.replace(self.path)
