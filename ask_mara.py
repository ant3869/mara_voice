from __future__ import annotations

import argparse
import os
from pathlib import Path

from mara.pipeline import (
    DEFAULT_AGENT_SESSION_HISTORY_MESSAGES,
    DEFAULT_AGENT_SESSION_HISTORY_PATH,
    DEFAULT_AGENT_SESSION_ID,
    DEFAULT_AGENT_SESSION_PERSISTENCE,
    DEFAULT_HERMES_SESSION_ID,
    DEFAULT_OPENCLAW_SESSION_ID,
    DEFAULT_CAPTURE_DIR,
    DEFAULT_OPENCLAW_BASE_URL,
    DEFAULT_OPENCLAW_MODEL,
    DEFAULT_OPENCLAW_TIMEOUT_SECONDS,
    DEFAULT_HERMES_COMMAND,
    DEFAULT_ACTIVE_AGENT,
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
        description="Send text to the selected agent, synthesize Mara's reply, and play it locally."
    )
    parser.add_argument("text", nargs="*", help="Text to send to the selected agent")
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
        help="Stop after getting the agent reply, without calling TTS or audio playback",
    )
    parser.add_argument("--ssh-target", default=DEFAULT_SSH_TARGET)
    parser.add_argument("--hermes-command", default=DEFAULT_HERMES_COMMAND)
    parser.add_argument("--active-agent", choices=("hermes", "openclaw"), default=DEFAULT_ACTIVE_AGENT)
    parser.add_argument("--openclaw-base-url", default=DEFAULT_OPENCLAW_BASE_URL)
    parser.add_argument("--openclaw-model", default=DEFAULT_OPENCLAW_MODEL)
    parser.add_argument(
        "--agent-session-id",
        default=DEFAULT_AGENT_SESSION_ID,
        help="Base voice session id. Per-agent ids default to <base>-hermes and <base>-openclaw.",
    )
    parser.add_argument(
        "--hermes-session-id",
        default=DEFAULT_HERMES_SESSION_ID,
        help="Stable session id sent to Hermes. Blank derives from --agent-session-id.",
    )
    parser.add_argument(
        "--openclaw-session-id",
        default=DEFAULT_OPENCLAW_SESSION_ID,
        help="Stable user/session id sent to OpenClaw. Blank derives from --agent-session-id.",
    )
    parser.add_argument(
        "--agent-session-history-path",
        default=str(DEFAULT_AGENT_SESSION_HISTORY_PATH),
        help="JSON file storing persistent per-agent conversation history.",
    )
    parser.add_argument(
        "--agent-session-history-messages",
        type=int,
        default=DEFAULT_AGENT_SESSION_HISTORY_MESSAGES,
        help="Maximum prior user/assistant messages retained per agent session.",
    )
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument(
        "--agent-session-persistence",
        dest="agent_session_persistence",
        action="store_true",
        default=DEFAULT_AGENT_SESSION_PERSISTENCE,
        help="Persist and reuse per-agent conversation history.",
    )
    session_group.add_argument(
        "--no-agent-session-persistence",
        dest="agent_session_persistence",
        action="store_false",
        help="Disable persistent per-agent conversation history.",
    )
    parser.add_argument(
        "--openclaw-timeout",
        type=lambda value: parse_optional_timeout(value, DEFAULT_OPENCLAW_TIMEOUT_SECONDS),
        default=DEFAULT_OPENCLAW_TIMEOUT_SECONDS,
        help="Seconds to wait for OpenClaw HTTP response. Use 0/none to disable it.",
    )
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
        active_agent=args.active_agent,
        openclaw_base_url=args.openclaw_base_url,
        openclaw_model=args.openclaw_model,
        openclaw_timeout_seconds=args.openclaw_timeout,
        agent_session_id=args.agent_session_id,
        hermes_session_id=args.hermes_session_id,
        openclaw_session_id=args.openclaw_session_id,
        agent_session_history_path=Path(args.agent_session_history_path),
        agent_session_history_messages=args.agent_session_history_messages,
        agent_session_persistence=args.agent_session_persistence,
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
    emit_status(logger, "THINKING", f"Sending prompt to {args.active_agent}")

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
