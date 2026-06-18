from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mara_events import append_event, read_recent_events
from mara_safety import REDACTION, redact_sensitive_text


class SafetyTests(unittest.TestCase):
    def test_redacts_labeled_key_and_long_hex(self) -> None:
        text = "API Key for authentication is:\n`698d671dc41db956b530af55945f9c7aeecc70df321bfb8fe5b6c2b422b86f89`"

        redacted = redact_sensitive_text(text)

        self.assertIn(REDACTION, redacted)
        self.assertNotIn("698d671dc41db956b530af55945f9c7aeecc70df321bfb8fe5b6c2b422b86f89", redacted)

    def test_append_event_redacts_nested_details_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            event_path = Path(tmp_dir) / "events.jsonl"
            append_event(
                "conversation",
                "MARA",
                "token: sk-test_secret_value_1234567890",
                {"text": "secret=0123456789abcdef0123456789abcdef"},
                event_log_path=event_path,
            )

            raw_event = json.loads(event_path.read_text(encoding="utf-8"))
            events = read_recent_events(event_log_path=event_path)

        self.assertIn(REDACTION, raw_event["message"])
        self.assertIn(REDACTION, raw_event["details"]["text"])
        self.assertIn(REDACTION, events[0]["message"])
        self.assertIn(REDACTION, events[0]["details"]["text"])


if __name__ == "__main__":
    unittest.main()
