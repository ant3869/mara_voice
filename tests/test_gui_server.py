from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import mara.agents as mara_agents
import mara.gui_server as mara_gui_server
from mara.events import append_event


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
                            "async_agent_replies": True,
                            "async_ack_text": "One second.",
                            "async_followup_enabled": True,
                            "async_followup_initial_delay_seconds": 2.0,
                            "async_followup_poll_seconds": 4.0,
                            "async_followup_max_attempts": 5,
                            "agent_session_id": "voice-session",
                            "hermes_session_id": "voice-session-hermes",
                            "openclaw_session_id": "voice-session-openclaw",
                            "agent_session_history_messages": 12,
                            "agent_session_persistence": True,
                            "voice_inbox_poll_seconds": 3.0,
                            "spoken_reply_char_limit": 1200,
                            "tts_stream_chunk_char_limit": 1200,
                            "hermes_voice_profile": "mara_professional",
                            "hermes_voice_style": "A crisp professional adult female voice.",
                            "openclaw_voice_profile": "openclaw_technical",
                            "openclaw_voice_style": "A precise technical adult male voice.",
                        }
                    },
                )
                state_response = client.get("/api/state")
                route_response = client.post(
                    "/api/route",
                    json={"active_agent": "openclaw", "next_agent": "openclaw"},
                )
                options_after_route = client.get("/api/options")
                health_response = client.get("/api/health")
            finally:
                mara_gui_server.CONFIG_PATH = original_config_path
                mara_gui_server.EVENT_LOG_PATH = original_event_log_path
                mara_gui_server.AGENT_ROUTE_STATE_PATH = original_agent_route_state_path
                mara_gui_server.list_output_devices = original_list_output_devices
                mara_gui_server.current_tts_health = original_current_tts_health

        self.assertEqual(save_response.status_code, 200)
        self.assertTrue(save_response.json()["options"]["tts_streaming"])
        self.assertTrue(save_response.json()["options"]["async_agent_replies"])
        self.assertEqual(save_response.json()["options"]["async_ack_text"], "One second.")
        self.assertTrue(save_response.json()["options"]["async_followup_enabled"])
        self.assertEqual(save_response.json()["options"]["async_followup_initial_delay_seconds"], 2.0)
        self.assertEqual(save_response.json()["options"]["async_followup_poll_seconds"], 4.0)
        self.assertEqual(save_response.json()["options"]["async_followup_max_attempts"], 5)
        self.assertEqual(save_response.json()["options"]["agent_session_id"], "voice-session")
        self.assertEqual(save_response.json()["options"]["hermes_session_id"], "voice-session-hermes")
        self.assertEqual(save_response.json()["options"]["openclaw_session_id"], "voice-session-openclaw")
        self.assertEqual(save_response.json()["options"]["agent_session_history_messages"], 12)
        self.assertTrue(save_response.json()["options"]["agent_session_persistence"])
        self.assertEqual(save_response.json()["options"]["voice_inbox_poll_seconds"], 3.0)
        self.assertEqual(save_response.json()["options"]["spoken_reply_char_limit"], 1200)
        self.assertEqual(save_response.json()["options"]["tts_stream_chunk_char_limit"], 1200)
        self.assertEqual(save_response.json()["options"]["hermes_voice_profile"], "mara_professional")
        self.assertEqual(save_response.json()["options"]["hermes_voice_style"], "A crisp professional adult female voice.")
        self.assertEqual(save_response.json()["options"]["openclaw_voice_profile"], "openclaw_technical")
        self.assertEqual(save_response.json()["options"]["openclaw_voice_style"], "A precise technical adult male voice.")
        self.assertEqual(state_response.status_code, 200)
        self.assertEqual(state_response.json()["current_status"], "THINKING")
        self.assertEqual(state_response.json()["last_user_text"], "hello")
        self.assertEqual(route_response.status_code, 200)
        self.assertEqual(route_response.json()["active_agent"], "openclaw")
        self.assertEqual(route_response.json()["next_agent"], "openclaw")
        self.assertEqual(options_after_route.status_code, 200)
        self.assertEqual(options_after_route.json()["options"]["active_agent"], "openclaw")
        self.assertEqual(health_response.status_code, 200)
        self.assertIn("hermes_voice_profile", health_response.json()["option_schema_keys"])
        self.assertIn("hermes_voice_style", health_response.json()["option_schema_keys"])
        self.assertIn("openclaw_voice_profile", health_response.json()["option_schema_keys"])
        self.assertIn("openclaw_voice_style", health_response.json()["option_schema_keys"])

    def test_chat_endpoint_logs_typed_turns_for_selected_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            original_config_path = mara_gui_server.CONFIG_PATH
            original_event_log_path = mara_gui_server.EVENT_LOG_PATH
            original_agent_route_state_path = mara_gui_server.AGENT_ROUTE_STATE_PATH
            original_ask_agent = mara_gui_server.ask_agent
            original_list_output_devices = mara_gui_server.list_output_devices
            original_current_tts_health = mara_gui_server.current_tts_health
            original_synthesize_speech = mara_gui_server.synthesize_speech
            original_play_audio_bytes = mara_gui_server.play_audio_bytes

            seen: dict[str, object] = {}

            def fake_ask_agent(
                input_text,
                settings,
                logger,
                status_callback=None,
                status_interval_seconds=10.0,
                requests_session=None,
            ):
                seen["input_text"] = input_text
                seen["settings"] = settings
                seen["status_interval_seconds"] = status_interval_seconds
                if status_callback:
                    status_callback("THINKING", "Fake agent is thinking", {"agent": settings.active_agent})
                return mara_agents.AgentReply(
                    settings.active_agent,
                    mara_agents.agent_display_name(settings.active_agent),
                    f"reply to {input_text}",
                    1.234,
                )

            def fake_synthesize_speech(text, config, logger, voice_style=None, voice_id=None):
                seen["tts_text"] = text
                seen["tts_config"] = config
                seen["voice_style"] = voice_style
                seen["voice_id"] = voice_id
                return b"fake-wav"

            def fake_play_audio_bytes(wav_bytes, config, logger):
                seen["played_wav_bytes"] = wav_bytes
                seen["play_config"] = config
                return 0.456

            mara_gui_server.CONFIG_PATH = tmp_path / "mara_voice.local.json"
            mara_gui_server.EVENT_LOG_PATH = tmp_path / "mara_events.jsonl"
            mara_gui_server.AGENT_ROUTE_STATE_PATH = tmp_path / "mara_agent_route.runtime.json"
            mara_gui_server.ask_agent = fake_ask_agent
            mara_gui_server.synthesize_speech = fake_synthesize_speech
            mara_gui_server.play_audio_bytes = fake_play_audio_bytes
            mara_gui_server.list_output_devices = lambda: []
            mara_gui_server.current_tts_health = lambda timeout_seconds=2.0: {
                "ok": True,
                "payload": {"streaming_supported": True, "voice_generation_mode": "test"},
            }

            try:
                client = TestClient(mara_gui_server.app)
                client.post(
                    "/api/options",
                    json={
                        "options": {
                            "agent_session_id": "typed-session",
                            "hermes_session_id": "typed-hermes",
                            "openclaw_session_id": "typed-openclaw",
                            "agent_session_history_path": str(tmp_path / "sessions.json"),
                            "agent_session_history_messages": 7,
                            "agent_session_persistence": True,
                            "status_interval_seconds": 0,
                            "tts_timeout_seconds": 42,
                            "audio_device": "test-speaker",
                            "openclaw_voice_style": "A test Claw voice.",
                        }
                    },
                )
                chat_response = client.post(
                    "/api/chat",
                    json={"agent": "openclaw", "text": "  typed hello  "},
                )
                events_response = client.get("/api/events?limit=20")
            finally:
                mara_gui_server.CONFIG_PATH = original_config_path
                mara_gui_server.EVENT_LOG_PATH = original_event_log_path
                mara_gui_server.AGENT_ROUTE_STATE_PATH = original_agent_route_state_path
                mara_gui_server.ask_agent = original_ask_agent
                mara_gui_server.list_output_devices = original_list_output_devices
                mara_gui_server.current_tts_health = original_current_tts_health
                mara_gui_server.synthesize_speech = original_synthesize_speech
                mara_gui_server.play_audio_bytes = original_play_audio_bytes

        self.assertEqual(chat_response.status_code, 200)
        self.assertEqual(chat_response.json()["agent"], "openclaw")
        self.assertEqual(chat_response.json()["text"], "reply to typed hello")
        self.assertTrue(chat_response.json()["voice_ok"])
        self.assertEqual(chat_response.json()["tts_seconds"], 0.456)
        self.assertEqual(seen["input_text"], "typed hello")
        settings = seen["settings"]
        self.assertEqual(settings.active_agent, "openclaw")
        self.assertEqual(settings.agent_session_id, "typed-session")
        self.assertEqual(settings.openclaw_session_id, "typed-openclaw")
        self.assertEqual(settings.agent_session_history_messages, 7)
        self.assertTrue(settings.agent_session_persistence)
        self.assertEqual(seen["status_interval_seconds"], 0.0)
        self.assertEqual(seen["tts_text"], "reply to typed hello")
        self.assertEqual(seen["voice_id"], "openclaw")
        self.assertEqual(seen["voice_style"], "A test Claw voice.")
        self.assertEqual(seen["played_wav_bytes"], b"fake-wav")
        tts_config = seen["tts_config"]
        play_config = seen["play_config"]
        self.assertEqual(tts_config.active_agent, "openclaw")
        self.assertEqual(tts_config.tts_timeout_seconds, 42.0)
        self.assertEqual(play_config.audio_device, "test-speaker")

        self.assertEqual(events_response.status_code, 200)
        events = events_response.json()["events"]
        user_events = [event for event in events if event["type"] == "conversation" and event["status"] == "USER"]
        reply_events = [event for event in events if event["type"] == "conversation" and event["status"] == "MARA"]
        metric_events = [event for event in events if event["type"] == "metric" and event["status"] == "TIMING"]
        self.assertEqual(user_events[-1]["details"]["text"], "typed hello")
        self.assertEqual(user_events[-1]["details"]["agent"], "openclaw")
        self.assertEqual(user_events[-1]["details"]["source"], "gui_text")
        self.assertEqual(reply_events[-1]["details"]["text"], "reply to typed hello")
        self.assertEqual(reply_events[-1]["details"]["agent"], "openclaw")
        self.assertEqual(reply_events[-1]["details"]["source"], "gui_text")
        self.assertEqual(metric_events[-1]["details"]["tts_seconds"], 0.456)


if __name__ == "__main__":
    unittest.main()
