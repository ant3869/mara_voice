from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mara_agent_state import (
    AgentRouteState,
    consume_agent_route,
    load_agent_route_state,
    save_agent_route_state,
    update_agent_route_state,
)
from mara_agents import AgentSettings, _ask_hermes_ssh, _extract_openclaw_reply, ask_agent, record_session_turn, redact_url


class FakeResponse:
    def __init__(self, text: str = "OpenClaw says hello") -> None:
        self.ok = True
        self.status_code = 200
        self.text = ""
        self._text = text

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self._text}}]}


class FakeSession:
    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.calls: list[dict] = []
        self.responses = responses or [FakeResponse()]

    def post(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


class AgentRoutingTests(unittest.TestCase):
    def test_openclaw_adapter_posts_openai_compatible_chat_completion(self) -> None:
        session = FakeSession()
        settings = AgentSettings(
            active_agent="openclaw",
            openclaw_base_url="http://192.168.0.65:8645/v1",
            openclaw_model="gemini 3.1 pro",
            openclaw_api_key="test-key",
        )

        reply = ask_agent("hello", settings, logger=_NullLogger(), requests_session=session)

        self.assertEqual(reply.agent_id, "openclaw")
        self.assertEqual(reply.text, "OpenClaw says hello")
        self.assertEqual(session.calls[0]["url"], "http://192.168.0.65:8645/v1/chat/completions")
        self.assertEqual(session.calls[0]["headers"]["authorization"], "Bearer test-key")
        self.assertIn("gemini 3.1 pro", session.calls[0]["data"])

    def test_openclaw_reuses_persistent_session_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            session = FakeSession([FakeResponse("first reply"), FakeResponse("second reply")])
            settings = AgentSettings(
                active_agent="openclaw",
                openclaw_base_url="http://192.168.0.65:8645/v1",
                openclaw_model="gemini 3.1 pro",
                openclaw_api_key="test-key",
                agent_session_id="voice-session",
                agent_session_history_path=Path(tmp_dir) / "sessions.json",
            )

            ask_agent("first question", settings, logger=_NullLogger(), requests_session=session)
            ask_agent("second question", settings, logger=_NullLogger(), requests_session=session)

        second_payload = json.loads(session.calls[1]["data"])
        self.assertEqual(second_payload["user"], "voice-session-openclaw")
        self.assertEqual(
            second_payload["messages"],
            [
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first reply"},
                {"role": "user", "content": "second question"},
            ],
        )

    def test_hermes_prompt_carries_session_id_and_history(self) -> None:
        class FakeProcess:
            returncode = 0

            def communicate(self, timeout=None):
                return "fresh reply", ""

        with tempfile.TemporaryDirectory() as tmp_dir:
            settings = AgentSettings(
                active_agent="hermes",
                ssh_target="ant@example",
                hermes_command="~/.local/bin/hermes",
                agent_session_id="voice-session",
                agent_session_history_path=Path(tmp_dir) / "sessions.json",
            )
            record_session_turn(settings, "hermes", "old question", "old answer")

            with patch("mara_agents.subprocess.Popen", return_value=FakeProcess()) as popen:
                reply = _ask_hermes_ssh("new question", settings, _NullLogger(), None, 0)

        remote_command = popen.call_args[0][0][-1]
        self.assertEqual(reply.text, "fresh reply")
        self.assertIn("MARA_AGENT_SESSION_ID=voice-session-hermes", remote_command)
        self.assertIn("old question", remote_command)
        self.assertIn("old answer", remote_command)
        self.assertIn("new question", remote_command)

    def test_custom_per_agent_session_ids_are_used_for_every_prompt(self) -> None:
        class FakeProcess:
            returncode = 0

            def communicate(self, timeout=None):
                return "hermes reply", ""

        with tempfile.TemporaryDirectory() as tmp_dir:
            history_path = Path(tmp_dir) / "sessions.json"
            openclaw_session = FakeSession([FakeResponse("openclaw reply")])
            openclaw_settings = AgentSettings(
                active_agent="openclaw",
                openclaw_base_url="http://192.168.0.65:8645/v1",
                openclaw_model="gemini 3.1 pro",
                openclaw_api_key="test-key",
                openclaw_session_id="claw-main",
                agent_session_history_path=history_path,
            )
            hermes_settings = AgentSettings(
                active_agent="hermes",
                ssh_target="ant@example",
                hermes_command="~/.local/bin/hermes",
                hermes_session_id="mara-main",
                agent_session_history_path=history_path,
            )

            ask_agent("claw question", openclaw_settings, logger=_NullLogger(), requests_session=openclaw_session)
            with patch("mara_agents.subprocess.Popen", return_value=FakeProcess()) as popen:
                _ask_hermes_ssh("mara question", hermes_settings, _NullLogger(), None, 0)

            openclaw_payload = json.loads(openclaw_session.calls[0]["data"])
            remote_command = popen.call_args[0][0][-1]
            self.assertEqual(openclaw_payload["user"], "claw-main")
            self.assertIn("MARA_AGENT_SESSION_ID=mara-main", remote_command)

            stored = json.loads(history_path.read_text(encoding="utf-8"))
            self.assertIn("claw-main", stored["sessions"])
            self.assertIn("mara-main", stored["sessions"])
            self.assertNotIn("voice-session", stored["sessions"])

    def test_openclaw_reply_parser_accepts_text_fallback(self) -> None:
        self.assertEqual(_extract_openclaw_reply({"choices": [{"text": "fallback"}]}), "fallback")

    def test_redact_url_removes_credentials_and_query(self) -> None:
        self.assertEqual(
            redact_url("http://secret@example.test:8645/v1?api_key=hidden"),
            "http://<redacted>@example.test:8645/v1",
        )

    def test_route_state_consumes_one_shot_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_path = Path(tmp_dir) / "route.json"
            save_agent_route_state(AgentRouteState(active_agent="hermes", next_agent="openclaw"), state_path)

            consumed = consume_agent_route(state_path, fallback_active_agent="hermes")
            after = load_agent_route_state(state_path, fallback_active_agent="hermes")

        self.assertEqual(consumed.agent_id, "openclaw")
        self.assertTrue(consumed.one_shot)
        self.assertEqual(after.active_agent, "hermes")
        self.assertEqual(after.next_agent, "")

    def test_route_state_updates_default_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_path = Path(tmp_dir) / "route.json"
            state = update_agent_route_state(state_path, active_agent="openclaw")

        self.assertEqual(state.active_agent, "openclaw")


class _NullLogger:
    def info(self, *_args, **_kwargs) -> None:
        pass

    def debug(self, *_args, **_kwargs) -> None:
        pass

    def warning(self, *_args, **_kwargs) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
