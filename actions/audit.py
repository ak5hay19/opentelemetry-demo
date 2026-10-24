"""Append-only audit log: one JSON object per line in reports/actions/audit.jsonl."""
import json
from pathlib import Path

from actions.killswitch import ACTIONS_DIR
from tools.result import iso, utc_now

AUDIT_FILE = "audit.jsonl"


def _path(directory=None) -> Path:
    return Path(directory or ACTIONS_DIR) / AUDIT_FILE


def record(event: str, *, incident_id=None, action_id=None, params=None, mode=None, outcome=None,
           directory=None) -> dict:
    """Append one event and return it. Raises OSError when the line cannot be written."""
    entry = {
        "ts": iso(utc_now()), "incident_id": incident_id, "event": event,
        "action_id": action_id, "params": params, "mode": mode, "outcome": outcome,
    }
    path = _path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return entry


def read(directory=None) -> list[dict]:
    path = _path(directory)
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue  # a damaged line must not hide the others
    return entries
