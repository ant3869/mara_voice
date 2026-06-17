from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_EVENT_LOG_PATH = Path(
    os.getenv("MARA_EVENT_LOG", str(BASE_DIR / "logs" / "mara_events.jsonl"))
)

_EVENT_LOCK = RLock()


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def append_event(
    event_type: str,
    status: str,
    message: str,
    details: dict[str, Any] | None = None,
    event_log_path: Path | None = None,
) -> dict[str, Any]:
    event = {
        "timestamp": utc_timestamp(),
        "pid": os.getpid(),
        "type": event_type,
        "status": status,
        "message": message,
        "details": details or {},
    }
    path = event_log_path or DEFAULT_EVENT_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(event, ensure_ascii=True, sort_keys=True)
    with _EVENT_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
    return event


def read_recent_events(
    limit: int = 200,
    event_log_path: Path | None = None,
) -> list[dict[str, Any]]:
    path = event_log_path or DEFAULT_EVENT_LOG_PATH
    if not path.exists():
        return []

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    events: list[dict[str, Any]] = []
    for line in lines[-max(1, limit) :]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events
