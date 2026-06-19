from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from mara_events import DEFAULT_EVENT_LOG_PATH, read_recent_events


@dataclass(slots=True)
class MaraRuntimeState:
    current_status: str = "UNKNOWN"
    current_message: str = ""
    last_updated: str = ""
    last_user_text: str = ""
    last_mara_reply: str = ""
    last_error: str = ""
    event_count: int = 0
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    conversation_events: list[dict[str, Any]] = field(default_factory=list)


def build_state(
    events: list[dict[str, Any]],
    recent_limit: int = 50,
    conversation_limit: int = 120,
) -> MaraRuntimeState:
    # Conversation panels read this dedicated list so status/metric/timing events
    # can never push the actual dialogue out of the recent-events window.
    conversation = [event for event in events if str(event.get("type", "")) == "conversation"]
    state = MaraRuntimeState(
        event_count=len(events),
        recent_events=events[-recent_limit:],
        conversation_events=conversation[-conversation_limit:],
    )
    for event in events:
        event_type = str(event.get("type", ""))
        status = str(event.get("status", ""))
        message = str(event.get("message", ""))
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        timestamp = str(event.get("timestamp", ""))

        if event_type == "status":
            state.current_status = status
            state.current_message = message
            state.last_updated = timestamp
        elif event_type == "conversation" and status == "USER":
            state.last_user_text = str(details.get("text", ""))
            state.last_updated = timestamp
        elif event_type == "conversation" and status == "MARA":
            state.last_mara_reply = str(details.get("text", ""))
            state.last_updated = timestamp
        elif event_type == "error":
            state.last_error = message
            state.last_updated = timestamp

    return state


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the current Mara runtime state from the JSONL event log.")
    parser.add_argument("--event-log", default=str(DEFAULT_EVENT_LOG_PATH))
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--recent-limit", type=int, default=50)
    args = parser.parse_args()

    events = read_recent_events(limit=args.limit, event_log_path=Path(args.event_log))
    state = build_state(events, recent_limit=args.recent_limit)
    print(json.dumps(asdict(state), indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
