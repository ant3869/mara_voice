from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import os

from jarvis_listener import find_existing_capture_files


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


if __name__ == "__main__":
    unittest.main()
