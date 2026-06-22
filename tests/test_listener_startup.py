from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
import os
from unittest.mock import patch

from jarvis_listener import (
    VoiceInboxWatcher,
    WavCaptureProcessor,
    find_existing_capture_files,
    is_pending_followup_reply,
)
from mara.agents import AgentReply, AgentSettings


class ListenerStartupTests(unittest.TestCase):
    def test_find_existing_capture_files_returns_recent_wavs_oldest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            capture_dir = Path(tmp_dir)
            old_wav = capture_dir / "old.wav"
            first_wav = capture_dir / "first.wav"
            second_wav = capture_dir / "second.wav"
            ignored_txt = capture_dir / "ignored.txt"
            for file_path in (old_wav, first_wav, second_wav, ignored_txt):
                file_path.write_bytes(b"data")

            now = 1_000.0
            os.utime(old_wav, (now - 700, now - 700))
            os.utime(first_wav, (now - 300, now - 300))
            os.utime(second_wav, (now - 10, now - 10))
            os.utime(ignored_txt, (now - 5, now - 5))

            captures = find_existing_capture_files(capture_dir, max_age_seconds=600, now_seconds=now)

        self.assertEqual([path.name for path in captures], ["first.wav", "second.wav"])

    def test_find_existing_capture_files_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            capture_dir = Path(tmp_dir)
            wav_path = capture_dir / "capture.wav"
            wav_path.write_bytes(b"data")

            captures = find_existing_capture_files(capture_dir, max_age_seconds=0)

        self.assertEqual(captures, [])

    def test_find_existing_capture_files_skips_files_before_last_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            capture_dir = Path(tmp_dir)
            already_seen = capture_dir / "already_seen.wav"
            missed = capture_dir / "missed.wav"
            for file_path in (already_seen, missed):
                file_path.write_bytes(b"data")

            now = 1_000.0
            os.utime(already_seen, (now - 300, now - 300))
            os.utime(missed, (now - 10, now - 10))

            captures = find_existing_capture_files(
                capture_dir,
                max_age_seconds=600,
                now_seconds=now,
                newer_than_seconds=now - 60,
            )

        self.assertEqual([path.name for path in captures], ["missed.wav"])

    def test_near_duplicate_voicebox_captures_are_skipped_after_transcription(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            first_wav = tmp_path / "first.wav"
            second_wav = tmp_path / "second.wav"
            first_wav.write_bytes(b"fake audio")
            second_wav.write_bytes(b"fake audio copy")

            processor = _test_processor(tmp_path)
            settings = AgentSettings(active_agent="hermes", agent_session_history_path=tmp_path / "sessions.json")
            transcripts = {
                first_wav: "Can you hear me? Is this working?",
                second_wav: "Can you hear me is this working",
            }
            events: list[tuple[object, ...]] = []
            sent_prompts: list[str] = []
            processor._wait_for_stable_file = lambda _path: True
            processor._transcribe_wav = lambda path: transcripts[Path(path)]
            processor._agent_settings_for_next_route = lambda: settings
            processor._append_event = lambda *args, **_kwargs: events.append(args)
            processor._start_async_agent_reply = lambda text, _start, _settings: sent_prompts.append(text)

            with patch("jarvis_listener.emit_status", lambda *_args, **_kwargs: None), patch("builtins.print"):
                processor._process_wav_impl(first_wav)
                processor._process_wav_impl(second_wav)

        self.assertEqual(sent_prompts, ["Can you hear me? Is this working?"])
        self.assertEqual([event[1] for event in events if event[1] == "USER"], ["USER"])

    def test_async_agent_reply_speaks_ack_before_final_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            reply_allowed = threading.Event()
            spoken: list[tuple[str, str]] = []
            processor = _test_processor(Path(tmp_dir), async_ack_text="Ack.")
            processor._append_event = lambda *_args, **_kwargs: None
            processor._play_tts_reply = lambda text, voice_id="hermes": spoken.append((voice_id, text))

            def fake_ask_agent(_text, settings, *_args, **_kwargs):
                reply_allowed.wait(timeout=2)
                return AgentReply(settings.active_agent, "Hermes", "Final answer", 0.1)

            with patch("jarvis_listener.ask_agent", side_effect=fake_ask_agent), patch(
                "jarvis_listener.emit_status",
                lambda *_args, **_kwargs: None,
            ), patch(
                "builtins.print",
            ):
                thread = processor._start_async_agent_reply("research something", time.monotonic())
                self.assertIsNotNone(thread)
                self.assertEqual(spoken, [("hermes", "Ack.")])
                reply_allowed.set()
                thread.join(timeout=2)

        self.assertEqual(spoken, [("hermes", "Ack."), ("hermes", "Final answer")])

    def test_agent_ack_mode_speaks_generated_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            reply_allowed = threading.Event()
            spoken: list[tuple[str, str]] = []
            processor = _test_processor(Path(tmp_dir), async_ack_agent=True, async_ack_text="Fallback.")
            processor._append_event = lambda *_args, **_kwargs: None
            processor._play_tts_reply = lambda text, voice_id="hermes": spoken.append((voice_id, text))

            def fake_ask_agent(text, settings, *_args, **_kwargs):
                if "acknowledge" in text.lower():
                    return AgentReply(settings.active_agent, "Hermes", "On it, spinning up sub-agents now.", 0.05)
                reply_allowed.wait(timeout=2)
                return AgentReply(settings.active_agent, "Hermes", "Final answer", 0.1)

            with patch("jarvis_listener.ask_agent", side_effect=fake_ask_agent), patch(
                "jarvis_listener.emit_status",
                lambda *_args, **_kwargs: None,
            ), patch("builtins.print"):
                thread = processor._start_async_agent_reply("research something", time.monotonic())
                self.assertIsNotNone(thread)
                self.assertEqual(spoken, [("hermes", "On it, spinning up sub-agents now.")])
                reply_allowed.set()
                thread.join(timeout=2)

        self.assertEqual(
            spoken,
            [("hermes", "On it, spinning up sub-agents now."), ("hermes", "Final answer")],
        )

    def test_fast_reply_skips_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            spoken: list[tuple[str, str]] = []
            # Generous grace window, but the reply returns immediately, so the ack is skipped.
            processor = _test_processor(Path(tmp_dir), async_ack_agent=False, async_ack_text="Ack.", async_ack_grace_seconds=5)
            processor._append_event = lambda *_args, **_kwargs: None
            processor._play_tts_reply = lambda text, voice_id="hermes": spoken.append((voice_id, text))

            def fake_ask_agent(_text, settings, *_args, **_kwargs):
                return AgentReply(settings.active_agent, "Hermes", "Quick answer", 0.05)

            with patch("jarvis_listener.ask_agent", side_effect=fake_ask_agent), patch(
                "jarvis_listener.emit_status",
                lambda *_args, **_kwargs: None,
            ), patch("builtins.print"):
                thread = processor._start_async_agent_reply("hi", time.monotonic())
                self.assertIsNotNone(thread)
                thread.join(timeout=2)

        self.assertEqual(spoken, [("hermes", "Quick answer")])

    def test_complete_reply_mentioning_subagents_is_not_treated_as_pending(self) -> None:
        complete = (
            "Alright, I just spun up two sub-agents at the same time and they came back fast. "
            "One found Onyx Coffee Lab and the other found Lake Atalanta Park. How did that hold up, Ant?"
        )
        # Mentions "sub-agents" but is a finished answer -> must NOT trigger follow-up polling.
        self.assertFalse(is_pending_followup_reply(complete))
        # A genuine short deferral still should.
        self.assertTrue(
            is_pending_followup_reply("On it - deploying a sub-agent now, I'll follow up when it finishes.")
        )

    def test_preset_ack_phrases_are_chosen_at_random(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            phrases = "On it.\nGive me a sec.\nWorking on that now."
            processor = _test_processor(
                Path(tmp_dir),
                async_ack_agent=False,
                async_ack_phrases=phrases,
            )
            expected = {"On it.", "Give me a sec.", "Working on that now."}
            self.assertEqual(set(processor.async_ack_phrases), expected)

            spoken: list[str] = []
            for _ in range(25):
                spoken.append(processor._pick_static_ack())

            self.assertTrue(set(spoken).issubset(expected))
            # Across many picks at least two distinct phrases should appear.
            self.assertGreaterEqual(len(set(spoken)), 2)

    def test_preset_ack_falls_back_to_text_without_phrases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            processor = _test_processor(Path(tmp_dir), async_ack_agent=False, async_ack_text="Only one.")
            self.assertEqual(processor._pick_static_ack(), "Only one.")

    def test_deferred_async_reply_is_polled_until_final_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            spoken: list[tuple[str, str]] = []
            responses = [
                AgentReply("hermes", "Hermes", "I started a background task and will follow up when it finishes.", 0.1),
                AgentReply("hermes", "Hermes", "STILL_WORKING", 0.1),
                AgentReply("hermes", "Hermes", "The background task is complete.", 0.1),
            ]
            prompts: list[str] = []
            processor = _test_processor(
                tmp_path,
                async_followup_initial_delay_seconds=0,
                async_followup_poll_seconds=1,
                async_followup_max_attempts=3,
            )
            processor._append_event = lambda *_args, **_kwargs: None
            processor._play_tts_reply = lambda text, voice_id="hermes": spoken.append((voice_id, text))

            def fake_ask_agent(text, *_args, **_kwargs):
                prompts.append(text)
                return responses.pop(0)

            settings = AgentSettings(active_agent="hermes", agent_session_history_path=tmp_path / "sessions.json")
            with patch("jarvis_listener.ask_agent", side_effect=fake_ask_agent), patch(
                "jarvis_listener.emit_status",
                lambda *_args, **_kwargs: None,
            ), patch("jarvis_listener.time.sleep", lambda _seconds: None), patch("builtins.print"):
                processor._complete_async_agent_reply("do background work", settings, time.monotonic())

        self.assertEqual(
            spoken,
            [
                ("hermes", "I started a background task and will follow up when it finishes."),
                ("hermes", "The background task is complete."),
            ],
        )
        self.assertEqual(len(prompts), 3)
        self.assertIn("Automatic Mara Voice follow-up check", prompts[1])

    def test_voice_inbox_watcher_plays_new_jsonl_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            inbox_path = tmp_path / "inbox.jsonl"
            inbox_path.write_text('{"text":"Inbox final","agent":"openclaw"}\n', encoding="utf-8")
            spoken: list[tuple[str, str]] = []
            processor = _test_processor(tmp_path)
            processor._append_event = lambda *_args, **_kwargs: None
            processor._play_tts_reply = lambda text, voice_id="hermes": spoken.append((voice_id, text))
            watcher = VoiceInboxWatcher(inbox_path, processor, _NullLogger(), 0.1, start_at_end=False)

            with patch("jarvis_listener.emit_status", lambda *_args, **_kwargs: None), patch("builtins.print"):
                processed = watcher.poll_once()

        self.assertEqual(processed, 1)
        self.assertEqual(spoken, [("openclaw", "Inbox final")])

    def test_voice_inbox_watcher_resumes_from_saved_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            inbox_path = tmp_path / "inbox.jsonl"
            offset_path = tmp_path / "inbox.jsonl.offset"
            with inbox_path.open("w", encoding="utf-8") as handle:
                handle.write('{"text":"Old reply","agent":"hermes"}\n')
                saved_offset = handle.tell()
                handle.write('{"text":"New reply","agent":"hermes"}\n')
            offset_path.write_text(f"{saved_offset}\n", encoding="utf-8")
            spoken: list[tuple[str, str]] = []
            processor = _test_processor(tmp_path)
            processor._append_event = lambda *_args, **_kwargs: None
            processor._play_tts_reply = lambda text, voice_id="hermes": spoken.append((voice_id, text))
            watcher = VoiceInboxWatcher(
                inbox_path,
                processor,
                _NullLogger(),
                0.1,
                start_at_end=True,
                offset_path=offset_path,
            )

            with patch("jarvis_listener.emit_status", lambda *_args, **_kwargs: None), patch("builtins.print"):
                processed = watcher.poll_once()

        self.assertEqual(processed, 1)
        self.assertEqual(spoken, [("hermes", "New reply")])


def _test_processor(
    tmp_path: Path,
    async_ack_text: str = "Working.",
    async_ack_agent: bool = False,
    async_ack_grace_seconds: float = 0.0,
    async_ack_phrases: str = "",
    async_followup_initial_delay_seconds: float = 0.0,
    async_followup_poll_seconds: float = 1.0,
    async_followup_max_attempts: int = 0,
) -> WavCaptureProcessor:
    return WavCaptureProcessor(
        capture_dir=tmp_path,
        ssh_target="test@example",
        hermes_command="hermes",
        active_agent="hermes",
        openclaw_base_url="http://example.test/v1",
        openclaw_model="test-model",
        tts_url="http://127.0.0.1:8000/tts",
        tts_stream_url="http://127.0.0.1:8000/tts/stream",
        audio_device=None,
        logger=_NullLogger(),
        agent_route_state_path=tmp_path / "route.json",
        status_interval_seconds=0,
        async_agent_replies=True,
        async_ack_agent=async_ack_agent,
        async_ack_grace_seconds=async_ack_grace_seconds,
        async_ack_text=async_ack_text,
        async_ack_phrases=async_ack_phrases,
        async_followup_initial_delay_seconds=async_followup_initial_delay_seconds,
        async_followup_poll_seconds=async_followup_poll_seconds,
        async_followup_max_attempts=async_followup_max_attempts,
    )


class _NullLogger:
    def debug(self, *_args, **_kwargs) -> None:
        pass

    def info(self, *_args, **_kwargs) -> None:
        pass

    def warning(self, *_args, **_kwargs) -> None:
        pass

    def error(self, *_args, **_kwargs) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
