from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import mara_gui_server
from mara_events import append_event


class GuiServerTests(unittest.TestCase):
    def test_options_round_trip_and_state_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            original_config_path = mara_gui_server.CONFIG_PATH
            original_event_log_path = mara_gui_server.EVENT_LOG_PATH
            original_agent_route_state_path = mara_gui_server.AGENT_ROUTE_STATE_PATH
            original_list_output_devices = mara_gui_server.list_output_devices
            original_current_tts_health = mara_gui_server.current_tts_health

            mara_gui_server.CONFIG_PATH = tmp_path / "mara_voice.local.json"
            mara_gui_server.EVENT_LOG_PATH = tmp_path / "mara_events.jsonl"
            mara_gui_server.AGENT_ROUTE_STATE_PATH = tmp_path / "mara_agent_route.runtime.json"
            mara_gui_server.list_output_devices = lambda: []
            mara_gui_server.current_tts_health = lambda timeout_seconds=2.0: {
                "ok": True,
                "payload": {"streaming_supported": True, "voice_generation_mode": "test"},
            }
            append_event(
                "status",
                "THINKING",
                "Testing state",
                event_log_path=mara_gui_server.EVENT_LOG_PATH,
            )
            append_event(
                "conversation",
                "USER",
                "User transcription",
                {"text": "hello"},
                event_log_path=mara_gui_server.EVENT_LOG_PATH,
            )

            try:
                client = TestClient(mara_gui_server.app)
                save_response = client.post(
                    "/api/options",
                    json={
                        "options": {
                            "tts_streaming": True,
                            "spoken_reply_char_limit": 1200,
                            "tts_stream_chunk_char_limit": 180,
                        }
                    },
                )
                state_response = client.get("/api/state")
                route_response = client.post(
                    "/api/route",
                    json={"active_agent": "openclaw", "next_agent": "openclaw"},
                )
                options_after_route = client.get("/api/options")
            finally:
                mara_gui_server.CONFIG_PATH = original_config_path
                mara_gui_server.EVENT_LOG_PATH = original_event_log_path
                mara_gui_server.AGENT_ROUTE_STATE_PATH = original_agent_route_state_path
                mara_gui_server.list_output_devices = original_list_output_devices
                mara_gui_server.current_tts_health = original_current_tts_health

        self.assertEqual(save_response.status_code, 200)
        self.assertTrue(save_response.json()["options"]["tts_streaming"])
        self.assertEqual(save_response.json()["options"]["spoken_reply_char_limit"], 1200)
        self.assertEqual(save_response.json()["options"]["tts_stream_chunk_char_limit"], 180)
        self.assertEqual(state_response.status_code, 200)
        self.assertEqual(state_response.json()["current_status"], "THINKING")
        self.assertEqual(state_response.json()["last_user_text"], "hello")
        self.assertEqual(route_response.status_code, 200)
        self.assertEqual(route_response.json()["active_agent"], "openclaw")
        self.assertEqual(route_response.json()["next_agent"], "openclaw")
        self.assertEqual(options_after_route.status_code, 200)
        self.assertEqual(options_after_route.json()["options"]["active_agent"], "openclaw")


if __name__ == "__main__":
    unittest.main()
