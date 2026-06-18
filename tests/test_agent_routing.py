from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mara_agent_state import (
    AgentRouteState,
    consume_agent_route,
    load_agent_route_state,
    save_agent_route_state,
    update_agent_route_state,
)
from mara_agents import AgentSettings, _extract_openclaw_reply, ask_agent, redact_url


class FakeResponse:
    ok = True
    status_code = 200
    text = ""

    def json(self) -> dict:
        return {"choices": [{"message": {"content": "OpenClaw says hello"}}]}


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return FakeResponse()


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


if __name__ == "__main__":
    unittest.main()
