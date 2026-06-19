from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import requests
import sounddevice as sd
import soundfile as sf

from mara_events import append_event
from mara_agents import (
    DEFAULT_ACTIVE_AGENT,
    DEFAULT_AGENT_SESSION_HISTORY_MESSAGES,
    DEFAULT_AGENT_SESSION_HISTORY_PATH,
    DEFAULT_AGENT_SESSION_ID,
    DEFAULT_AGENT_SESSION_PERSISTENCE,
    DEFAULT_HERMES_SESSION_ID,
    DEFAULT_OPENCLAW_BASE_URL,
    DEFAULT_OPENCLAW_MODEL,
    DEFAULT_OPENCLAW_TIMEOUT_SECONDS,
    DEFAULT_OPENCLAW_SESSION_ID,
    AgentSettings,
    ask_agent,
    check_openclaw_health,
    redact_url,
)
from mara_safety import redact_sensitive_text


BASE_DIR = Path(__file__).resolve().parent


def _default_capture_dir() -> Path:
    explicit = os.getenv("MARA_CAPTURE_DIR")
    if explicit:
        return Path(explicit)
    # Voicebox stores captures under the per-user roaming app data directory.
    # Resolve it from the environment so the default is portable across machines
    # instead of hardcoding a single user's profile path.
    appdata = os.getenv("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "sh.voicebox.app" / "captures"


DEFAULT_CAPTURE_DIR = _default_capture_dir()
DEFAULT_SSH_TARGET = os.getenv("MARA_SSH_TARGET", "ant@192.168.0.65")
DEFAULT_HERMES_COMMAND = os.getenv("MARA_HERMES_COMMAND", "~/.local/bin/hermes")
DEFAULT_TTS_URL = os.getenv("MARA_TTS_URL", "http://127.0.0.1:8000/tts")
DEFAULT_TTS_HEALTH_URL = os.getenv("MARA_TTS_HEALTH_URL", "http://127.0.0.1:8000/healthz")
DEFAULT_LOG_DIR = Path(os.getenv("MARA_LOG_DIR", str(BASE_DIR / "logs")))
DEFAULT_AUDIO_DEVICE = os.getenv("MARA_AUDIO_DEVICE")

STATUS_COLORS = {
    "LISTENING": "\033[96m",
    "THINKING": "\033[93m",
    "CODING": "\033[95m",
    "WEB BROWSING": "\033[94m",
    "WRITING": "\033[92m",
    "TALKING": "\033[97m",
    "DREAMING": "\033[90m",
    "REMEMBERING": "\033[36m",
    "RESET": "\033[0m",
}


def parse_optional_timeout(value: str | None, default: float | None) -> float | None:
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in ("", "0", "none", "off", "false", "disabled", "never"):
        return None

    timeout_seconds = float(normalized)
    if timeout_seconds <= 0:
        return None
    return timeout_seconds


def format_timeout(timeout_seconds: float | None) -> str:
    if timeout_seconds is None:
        return "disabled"
    return f"{timeout_seconds:.1f}s"


def infer_workflow_status(text: str) -> str | None:
    lowered = text.lower()

    if any(keyword in lowered for keyword in ("remember", "memory", "memories", "saved", "note", "store")):
        return "REMEMBERING"

    if any(keyword in lowered for keyword in ("browse", "browser", "web", "website", "url", "http://", "https://", "search the web", "look up")):
        return "WEB BROWSING"

    if any(keyword in lowered for keyword in ("code", "coding", "refactor", "script", "function", "class", "python", "javascript", "ts", "typescript")):
        return "CODING"

    if any(keyword in lowered for keyword in ("dream", "daydream", "imagine", "creative", "story", "poem", "write a scene")):
        return "DREAMING"

    return None


@dataclass(slots=True)
class PipelineConfig:
    capture_dir: Path = DEFAULT_CAPTURE_DIR
    ssh_target: str = DEFAULT_SSH_TARGET
    hermes_command: str = DEFAULT_HERMES_COMMAND
    active_agent: str = DEFAULT_ACTIVE_AGENT
    openclaw_base_url: str = DEFAULT_OPENCLAW_BASE_URL
    openclaw_model: str = DEFAULT_OPENCLAW_MODEL
    openclaw_timeout_seconds: float | None = DEFAULT_OPENCLAW_TIMEOUT_SECONDS
    agent_session_id: str = DEFAULT_AGENT_SESSION_ID
    hermes_session_id: str = DEFAULT_HERMES_SESSION_ID
    openclaw_session_id: str = DEFAULT_OPENCLAW_SESSION_ID
    agent_session_history_path: Path = DEFAULT_AGENT_SESSION_HISTORY_PATH
    agent_session_history_messages: int = DEFAULT_AGENT_SESSION_HISTORY_MESSAGES
    agent_session_persistence: bool = DEFAULT_AGENT_SESSION_PERSISTENCE
    tts_url: str = DEFAULT_TTS_URL
    tts_health_url: str = DEFAULT_TTS_HEALTH_URL
    audio_device: str | int | None = DEFAULT_AUDIO_DEVICE
    ssh_timeout_seconds: float | None = parse_optional_timeout(os.getenv("MARA_SSH_TIMEOUT"), None)
    tts_timeout_seconds: float | None = parse_optional_timeout(os.getenv("MARA_TTS_TIMEOUT"), 300.0)
    log_dir: Path = DEFAULT_LOG_DIR


class StatusFormatter(logging.Formatter):
    def __init__(self, use_colors: bool = True) -> None:
        super().__init__("%(message)s")
        self.use_colors = use_colors

    def format(self, record: logging.LogRecord) -> str:
        status = getattr(record, "status", None)
        message = super().format(record)
        if not status:
            return message

        if not self.use_colors:
            return f"[{status}] {message}"

        color = STATUS_COLORS.get(str(status).upper(), "")
        reset = STATUS_COLORS["RESET"] if color else ""
        return f"{color}[{status}] {message}{reset}"


def configure_logging(level: str = "INFO", log_dir: Path | None = None) -> logging.Logger:
    logger = logging.getLogger("mara_voice")
    resolved_level = getattr(logging, str(level).upper(), logging.INFO)

    if logger.handlers:
        logger.setLevel(resolved_level)
        for handler in logger.handlers:
            handler.setLevel(resolved_level)
        return logger

    logger.setLevel(resolved_level)
    logger.propagate = False

    resolved_log_dir = log_dir or DEFAULT_LOG_DIR
    resolved_log_dir.mkdir(parents=True, exist_ok=True)
    log_format = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    console_format = StatusFormatter(use_colors=sys.stdout.isatty())

    console_handler = logging.StreamHandler()
    console_handler.setLevel(resolved_level)
    console_handler.setFormatter(console_format)

    file_handler = RotatingFileHandler(
        resolved_log_dir / "mara_voice.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(resolved_level)
    file_handler.setFormatter(log_format)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


def emit_status(
    logger: logging.Logger,
    status: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    logger.info(message, extra={"status": status})
    try:
        append_event("status", status, message, details=details)
    except Exception as exc:
        logger.debug("Could not write status event: %s", exc)


def normalize_audio_device(value: str | int | None) -> str | int | None:
    if value in (None, ""):
        return None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value


def list_output_devices() -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for index, device in enumerate(sd.query_devices()):
        if int(device.get("max_output_channels", 0)) <= 0:
            continue
        devices.append(
            {
                "index": index,
                "name": device.get("name", "unknown"),
                "channels": int(device.get("max_output_channels", 0)),
                "default_samplerate": device.get("default_samplerate"),
            }
        )
    return devices


def format_output_devices() -> str:
    devices = list_output_devices()
    if not devices:
        return "No audio output devices were found."

    lines = []
    for device in devices:
        lines.append(
            f"[{device['index']}] {device['name']} | channels={device['channels']} | default_sr={device['default_samplerate']}"
        )
    return "\n".join(lines)


def check_tts_health(config: PipelineConfig, timeout_seconds: float = 2.0) -> dict[str, Any]:
    try:
        response = requests.get(config.tts_health_url, timeout=timeout_seconds)
        payload: dict[str, Any]
        if "application/json" in response.headers.get("content-type", ""):
            payload = response.json()
        else:
            payload = {"raw": response.text[:200]}

        return {
            "ok": response.ok and payload.get("status") == "ok",
            "status_code": response.status_code,
            "payload": payload,
        }
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


def check_ssh(config: PipelineConfig, timeout_seconds: float = 10.0) -> dict[str, Any]:
    command = [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=8",
        config.ssh_target,
        "printf ok",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    return {
        "ok": result.returncode == 0 and result.stdout.strip() == "ok",
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def run_diagnostics(
    config: PipelineConfig,
    logger: logging.Logger,
    include_ssh: bool = True,
) -> None:
    logger.info("Diagnostics started")
    logger.info("Python executable: %s", sys.executable)
    logger.info("Working directory: %s", Path.cwd())
    logger.info("Log file: %s", config.log_dir / "mara_voice.log")
    logger.info("Capture directory: %s", config.capture_dir)

    if config.capture_dir.exists():
        logger.info("Voicebox capture directory exists")
    else:
        logger.warning(
            "Voicebox capture directory does not exist yet: %s",
            config.capture_dir,
        )

    try:
        default_devices = sd.default.device
        logger.info("Default sounddevice input/output indices: %s", default_devices)
        outputs = list_output_devices()
        if outputs:
            preview = ", ".join(
                f"{device['index']}:{device['name']}" for device in outputs[:8]
            )
            logger.info("Detected audio output devices: %s", preview)
        else:
            logger.warning(
                "No audio output devices were detected. Windows may be routing audio to a disconnected device."
            )
    except Exception:
        logger.exception("Unable to enumerate audio devices")

    tts_health = check_tts_health(config)
    if tts_health.get("ok"):
        logger.info("TTS health check passed: %s", tts_health.get("payload"))
    else:
        logger.warning(
            "TTS health check failed for %s: %s",
            config.tts_health_url,
            tts_health.get("error") or tts_health.get("payload"),
        )
        logger.warning(
            "If the TTS server is not running, start it with: python mara_tts_server.py"
        )

    if include_ssh:
        ssh_status = check_ssh(config)
        if ssh_status.get("ok"):
            logger.info("SSH connectivity to %s looks good", config.ssh_target)
        else:
            logger.warning(
                "SSH check failed for %s: %s",
                config.ssh_target,
                ssh_status.get("error")
                or ssh_status.get("stderr")
                or ssh_status.get("stdout")
                or ssh_status,
            )
            logger.warning(
                "Confirm key-based auth works with: ssh %s 'printf ok'",
                config.ssh_target,
            )

    if config.active_agent == "openclaw":
        openclaw_status = check_openclaw_health(
            AgentSettings(
                active_agent=config.active_agent,
                openclaw_base_url=config.openclaw_base_url,
                openclaw_model=config.openclaw_model,
                openclaw_timeout_seconds=config.openclaw_timeout_seconds,
            )
        )
        if openclaw_status.get("ok"):
            logger.info("OpenClaw API looks reachable at %s", redact_url(config.openclaw_base_url))
        else:
            logger.warning(
                "OpenClaw check failed for %s: %s",
                redact_url(config.openclaw_base_url),
                openclaw_status.get("error") or openclaw_status.get("payload") or openclaw_status,
            )
            logger.warning(
                "Confirm the Dell gateway has API_SERVER_ENABLED=true, port 8645 is reachable, and MARA_OPENCLAW_API_KEY is set locally."
            )


def read_stable_text(
    file_path: Path,
    settle_seconds: float = 0.25,
    max_attempts: int = 6,
) -> str:
    last_size = -1
    for _ in range(max_attempts):
        current_size = file_path.stat().st_size
        if current_size == last_size:
            break
        last_size = current_size
        time.sleep(settle_seconds)

    with file_path.open("r", encoding="utf-8") as handle:
        return " ".join(handle.read().split())


def ask_selected_agent(
    input_text: str,
    config: PipelineConfig,
    logger: logging.Logger,
) -> str:
    workflow_status = infer_workflow_status(input_text)
    if workflow_status:
        emit_status(logger, workflow_status, f"Prompt looks like {workflow_status.lower()}")

    reply = ask_agent(
        input_text,
        AgentSettings(
            active_agent=config.active_agent,
            ssh_target=config.ssh_target,
            hermes_command=config.hermes_command,
            ssh_timeout_seconds=config.ssh_timeout_seconds,
            ssh_connect_timeout_seconds=5.0,
            openclaw_base_url=config.openclaw_base_url,
            openclaw_model=config.openclaw_model,
            openclaw_timeout_seconds=config.openclaw_timeout_seconds,
            agent_session_id=config.agent_session_id,
            hermes_session_id=config.hermes_session_id,
            openclaw_session_id=config.openclaw_session_id,
            agent_session_history_path=config.agent_session_history_path,
            agent_session_history_messages=config.agent_session_history_messages,
            agent_session_persistence=config.agent_session_persistence,
        ),
        logger,
        status_callback=lambda status, message, details=None: emit_status(logger, status, message, details),
    )
    return reply.text


ask_hermes = ask_selected_agent


def synthesize_speech(
    text: str,
    config: PipelineConfig,
    logger: logging.Logger,
    voice_style: str | None = None,
    voice_id: str | None = None,
) -> bytes:
    payload: dict[str, Any] = {"text": text}
    if voice_style:
        payload["voice_style"] = voice_style
    if voice_id:
        payload["voice_id"] = voice_id

    emit_status(logger, "WRITING", "Composing voice reply")
    logger.info(
        "Requesting TTS audio from %s (voice_id=%s, timeout=%s)",
        config.tts_url,
        voice_id or "hermes",
        format_timeout(config.tts_timeout_seconds),
    )
    start_time = time.perf_counter()
    try:
        response = requests.post(
            config.tts_url,
            json=payload,
            timeout=config.tts_timeout_seconds,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not reach the TTS server at {config.tts_url}: {exc}. Start it with `python mara_tts_server.py`."
        ) from exc

    elapsed = time.perf_counter() - start_time
    if not response.ok:
        body = response.text[:400].strip()
        raise RuntimeError(
            f"TTS server returned {response.status_code}: {body or '<empty response>'}"
        )

    logger.info("TTS returned %d bytes in %.2fs", len(response.content), elapsed)
    return response.content


def play_audio_bytes(
    wav_bytes: bytes,
    config: PipelineConfig,
    logger: logging.Logger,
) -> float:
    data, sample_rate = sf.read(io.BytesIO(wav_bytes), dtype="float32")
    audio_device = normalize_audio_device(config.audio_device)
    frame_count = len(data) if getattr(data, "ndim", 1) == 1 else data.shape[0]
    duration_seconds = frame_count / float(sample_rate)

    logger.info(
        "Playing %.2fs of audio at %d Hz using device %s",
        duration_seconds,
        sample_rate,
        audio_device if audio_device is not None else "default",
    )

    try:
        sd.play(data, sample_rate, device=audio_device)
        sd.wait()
    except Exception as exc:
        available_devices = format_output_devices()
        raise RuntimeError(
            "Audio playback failed. Use `python ask_mara.py --list-audio-devices` and retry with "
            f"`--audio-device <index>`. Details: {exc}\nAvailable devices:\n{available_devices}"
        ) from exc

    return duration_seconds


def run_pipeline(
    input_text: str,
    config: PipelineConfig,
    logger: logging.Logger,
    play_audio: bool = True,
    voice_style: str | None = None,
) -> str:
    normalized_text = " ".join(input_text.split())
    if not normalized_text:
        raise ValueError("Input text is empty after normalization.")

    logger.info("Pipeline started for %d characters", len(normalized_text))
    reply = ask_selected_agent(normalized_text, config, logger)
    safe_reply = redact_sensitive_text(reply)
    logger.info("Agent reply: %s", safe_reply)

    if not play_audio:
        emit_status(logger, "LISTENING", "Ready")
        return safe_reply

    wav_bytes = synthesize_speech(safe_reply, config, logger, voice_style=voice_style, voice_id=config.active_agent)
    emit_status(logger, "TALKING", "Playing reply")
    play_audio_bytes(wav_bytes, config, logger)
    emit_status(logger, "LISTENING", "Ready")
    logger.info("Pipeline finished successfully")
    return safe_reply
