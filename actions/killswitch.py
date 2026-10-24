"""The kill switch is a file. While reports/actions/KILL exists nothing may be executed or approved."""
from pathlib import Path

from tools.result import iso, utc_now

ACTIONS_DIR = Path(__file__).resolve().parent.parent / "reports" / "actions"
KILL_FILE = "KILL"


def _path(directory=None) -> Path:
    return Path(directory or ACTIONS_DIR) / KILL_FILE


def is_engaged(directory=None) -> bool:
    return _path(directory).exists()


def engage(directory=None) -> None:
    path = _path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"engaged {iso(utc_now())}\n", encoding="utf-8")


def clear(directory=None) -> None:
    _path(directory).unlink(missing_ok=True)
