from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from mara_agents import (
    AGENT_HERMES,
    AGENT_OPENCLAW,
    AgentSettings,
    guardrail_review,
    load_session_messages,
    record_session_turn,
)


class GuardrailTests(unittest.TestCase):
    def test_corrects_openclaw_first_person_misidentification(self) -> None:
        corrected, violations = guardrail_review(AGENT_OPENCLAW, "I am Mara and happy to help.")
        self.assertEqual(corrected, "I am Claw and happy to help.")
        self.assertIn("self-identified as Mara", violations)

    def test_corrects_hermes_first_person_misidentification(self) -> None:
        corrected, violations = guardrail_review(AGENT_HERMES, "I'm Claw, actually.")
        self.assertEqual(corrected, "I'm Mara, actually.")
        self.assertTrue(violations)

    def test_corrects_hermes_intro_as_claw(self) -> None:
        corrected, violations = guardrail_review(AGENT_HERMES, "Claw here, I can handle that.")
        self.assertEqual(corrected, "Mara here, I can handle that.")
        self.assertIn("self-identified as Claw", violations)

    def test_corrects_hermes_this_is_claw(self) -> None:
        corrected, violations = guardrail_review(AGENT_HERMES, "This is Claw. I can handle that.")
        self.assertEqual(corrected, "This is Mara. I can handle that.")
        self.assertIn("self-identified as Claw", violations)

    def test_allows_legitimate_third_person_mention(self) -> None:
        text = "I am Claw, but Mara handles the calendar."
        corrected, violations = guardrail_review(AGENT_OPENCLAW, text)
        self.assertEqual(corrected, text)
        self.assertEqual(violations, [])

    def test_flags_fabricated_relay_without_rewriting(self) -> None:
        text = "I just pinged her and she got it."
        corrected, violations = guardrail_review(AGENT_OPENCLAW, text)
        self.assertEqual(corrected, text)
        self.assertIn("fabricated cross-agent relay", violations)


class SessionHistoryTtlTests(unittest.TestCase):
    def test_expired_turns_are_dropped_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "sessions.json"
            settings = AgentSettings(
                active_agent="openclaw",
                agent_session_history_path=path,
                agent_session_history_ttl_seconds=60,
            )
            record_session_turn(settings, AGENT_OPENCLAW, "old question", "old answer")

            payload = json.loads(path.read_text(encoding="utf-8"))
            for session in payload["sessions"].values():
                for messages in session.values():
                    for message in messages:
                        message["ts"] = time.time() - 9999
            path.write_text(json.dumps(payload), encoding="utf-8")

            record_session_turn(settings, AGENT_OPENCLAW, "new question", "new answer")
            loaded = load_session_messages(settings, AGENT_OPENCLAW)

        self.assertEqual([m["content"] for m in loaded], ["new question", "new answer"])
        self.assertTrue(all("ts" not in m for m in loaded))

    def test_ttl_disabled_keeps_all_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "sessions.json"
            settings = AgentSettings(
                active_agent="openclaw",
                agent_session_history_path=path,
                agent_session_history_ttl_seconds=None,
            )
            record_session_turn(settings, AGENT_OPENCLAW, "old question", "old answer")

            payload = json.loads(path.read_text(encoding="utf-8"))
            for session in payload["sessions"].values():
                for messages in session.values():
                    for message in messages:
                        message["ts"] = time.time() - 9999
            path.write_text(json.dumps(payload), encoding="utf-8")

            loaded = load_session_messages(settings, AGENT_OPENCLAW)

        self.assertEqual([m["content"] for m in loaded], ["old question", "old answer"])


if __name__ == "__main__":
    unittest.main()
