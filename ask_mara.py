from __future__ import annotations

import argparse
import os
from pathlib import Path

from mara_pipeline import (
    DEFAULT_CAPTURE_DIR,
    DEFAULT_HERMES_COMMAND,
    DEFAULT_SSH_TARGET,
    DEFAULT_TTS_HEALTH_URL,
    DEFAULT_TTS_URL,
    PipelineConfig,
    configure_logging,
    emit_status,
    format_output_devices,
    parse_optional_timeout,
    run_diagnostics,
    run_pipeline,
)


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description="Send text to the Hermes agent, synthesize Mara's reply, and play it locally."
	)
	parser.add_argument("text", nargs="*", help="Text to send to Hermes")
	parser.add_argument("--capture-dir", default=str(DEFAULT_CAPTURE_DIR))
	parser.add_argument("--audio-device", help="Sounddevice output device index or name")
	parser.add_argument(
		"--diagnose",
		action="store_true",
		help="Run environment checks before doing anything else",
	)
	parser.add_argument(
		"--list-audio-devices",
		action="store_true",
		help="Print available audio output devices and exit",
	)
	parser.add_argument(
		"--log-level",
		default="INFO",
		help="Logging level: DEBUG, INFO, WARNING, ERROR",
	)
	parser.add_argument(
		"--no-playback",
		action="store_true",
		help="Stop after getting the Hermes reply, without calling TTS or audio playback",
	)
	parser.add_argument("--ssh-target", default=DEFAULT_SSH_TARGET)
	parser.add_argument("--hermes-command", default=DEFAULT_HERMES_COMMAND)
	parser.add_argument(
		"--ssh-timeout",
		type=lambda value: parse_optional_timeout(value, None),
		default=parse_optional_timeout(os.getenv("MARA_SSH_TIMEOUT"), None),
		help="Seconds to wait for Hermes over SSH. Use 0/none to wait indefinitely.",
	)
	parser.add_argument("--tts-url", default=DEFAULT_TTS_URL)
	parser.add_argument("--tts-health-url", default=DEFAULT_TTS_HEALTH_URL)
	parser.add_argument(
		"--tts-timeout",
		type=lambda value: parse_optional_timeout(value, 300.0),
		default=parse_optional_timeout(os.getenv("MARA_TTS_TIMEOUT"), 300.0),
		help="Seconds to wait for local TTS audio generation. Use 0/none to disable it.",
	)
	parser.add_argument(
		"--voice-style",
		help="Optional voice description to pass through to the TTS server",
	)
	return parser


def main() -> int:
	parser = build_parser()
	args = parser.parse_args()

	logger = configure_logging(args.log_level).getChild("ask_mara")
	config = PipelineConfig(
		capture_dir=Path(args.capture_dir),
		ssh_target=args.ssh_target,
		hermes_command=args.hermes_command,
		tts_url=args.tts_url,
		tts_health_url=args.tts_health_url,
		audio_device=args.audio_device,
	)
	config.ssh_timeout_seconds = args.ssh_timeout
	config.tts_timeout_seconds = args.tts_timeout

	if args.list_audio_devices:
		print(format_output_devices())
		return 0

	if args.diagnose:
		emit_status(logger, "THINKING", "Running diagnostics")
		run_diagnostics(config, logger, include_ssh=True)
		if not args.text:
			return 0

	if not args.text:
		parser.error("Provide text to send to Mara, or use --diagnose / --list-audio-devices.")

	prompt = " ".join(args.text)
	logger.info("User prompt: %s", prompt)
	emit_status(logger, "THINKING", "Sending prompt to Mara")

	try:
		emit_status(logger, "TALKING", "Waiting for Mara reply")
		reply = run_pipeline(
			prompt,
			config,
			logger,
			play_audio=not args.no_playback,
			voice_style=args.voice_style,
		)
	except Exception:
		logger.exception("ask_mara failed")
		return 1

	emit_status(logger, "LISTENING", "Ready")
	print(reply)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
