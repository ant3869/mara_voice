from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "mara_voice.local.json"

OPTION_SCHEMA: dict[str, dict[str, Any]] = {
    "tts_streaming": {"type": "bool", "default": True, "restart_required": True},
    "tts_stream_chunk_char_limit": {"type": "int", "default": 1200, "min": 40, "max": 12000, "restart_required": True},
    "tts_stream_prebuffer_seconds": {"type": "float", "default": 0.5, "min": 0.0, "max": 30.0, "restart_required": True},
    "tts_stream_prebuffer_dynamic": {"type": "bool", "default": True, "restart_required": True},
    "tts_stream_prebuffer_max_seconds": {"type": "float", "default": 4.0, "min": 0.0, "max": 30.0, "restart_required": True},
    "async_agent_replies": {"type": "bool", "default": True, "restart_required": True},
    "async_ack_agent": {"type": "bool", "default": False, "restart_required": True},
    "async_ack_grace_seconds": {"type": "float", "default": 5.0, "min": 0.0, "max": 120.0, "restart_required": True},
    "async_ack_text": {
        "type": "str",
        "default": "Yeah, let me dig into that real quick.",
        "restart_required": True,
    },
    "async_ack_phrases": {
        "type": "str",
        "default": "On it.\nGot it, working on that now.\nSure, give me a sec.\nAlright, let me look into that.\nOne moment.",
        "restart_required": True,
    },
    "async_followup_enabled": {"type": "bool", "default": True, "restart_required": True},
    "async_followup_initial_delay_seconds": {"type": "float", "default": 3.0, "min": 0.0, "max": 300.0, "restart_required": True},
    "async_followup_poll_seconds": {"type": "float", "default": 8.0, "min": 1.0, "max": 600.0, "restart_required": True},
    "async_followup_max_attempts": {"type": "int", "default": 60, "min": 0, "max": 1000, "restart_required": True},
    "voice_inbox_path": {"type": "str", "default": "", "restart_required": True},
    "voice_inbox_poll_seconds": {"type": "float", "default": 5.0, "min": 0.0, "max": 300.0, "restart_required": True},
    "agent_session_id": {"type": "str", "default": "voice-session", "restart_required": True},
    "hermes_session_id": {"type": "str", "default": "voice-session-hermes", "restart_required": True},
    "openclaw_session_id": {"type": "str", "default": "voice-session-openclaw", "restart_required": True},
    "agent_session_history_path": {"type": "str", "default": "", "restart_required": True},
    "agent_session_history_messages": {"type": "int", "default": 20, "min": 0, "max": 200, "restart_required": True},
    "agent_session_persistence": {"type": "bool", "default": True, "restart_required": True},
    "spoken_reply_char_limit": {"type": "int", "default": 900, "min": 0, "max": 12000, "restart_required": True},
    "tts_chunk_char_limit": {"type": "int", "default": 450, "min": 0, "max": 12000, "restart_required": True},
    "tts_inference_timesteps": {"type": "int", "default": 4, "min": 1, "max": 50, "restart_required": True},
    "tts_timeout_seconds": {"type": "float", "default": 300.0, "min": 0.0, "max": 3600.0, "restart_required": True},
    "ssh_timeout_seconds": {"type": "float", "default": 0.0, "min": 0.0, "max": 3600.0, "restart_required": True},
    "ssh_connect_timeout_seconds": {"type": "float", "default": 5.0, "min": 1.0, "max": 120.0, "restart_required": True},
    "status_interval_seconds": {"type": "float", "default": 10.0, "min": 0.0, "max": 300.0, "restart_required": True},
    "active_agent": {"type": "str", "default": "hermes", "choices": ("hermes", "openclaw"), "restart_required": False},
    "openclaw_base_url": {"type": "str", "default": "http://192.168.0.65:8645/v1", "restart_required": True},
    "openclaw_model": {"type": "str", "default": "gemini 3.1 pro", "restart_required": True},
    "openclaw_timeout_seconds": {"type": "float", "default": 600.0, "min": 0.0, "max": 3600.0, "restart_required": True},
    "audio_device": {"type": "str", "default": "", "restart_required": True},
    "tts_device": {"type": "str", "default": "cuda", "restart_required": True},
    "capture_dir": {"type": "str", "default": "", "restart_required": True},
    "hermes_command": {"type": "str", "default": "", "restart_required": True},
    "voice_reference_path": {"type": "str", "default": "", "restart_required": True},
    "voice_reference_text": {"type": "str", "default": "", "restart_required": True},
    "regenerate_voice_reference": {"type": "bool", "default": False, "restart_required": True},
    "hermes_voice_profile": {
        "type": "str",
        "default": "mara_default",
        "choices": (
            "mara_default",
            "mara_warm_expressive",
            "mara_professional",
            "mara_casual",
            "mara_soft_soothing",
            "custom",
        ),
        "restart_required": True,
    },
    "hermes_voice_style": {
        "type": "str",
        "default": "A clear adult female speaking voice with a warm lower tone, calm conversational pace, natural articulation, and no humming, sighing, singing, or nonverbal sounds",
        "restart_required": True,
    },
    "openclaw_voice_profile": {
        "type": "str",
        "default": "openclaw_default",
        "choices": (
            "openclaw_default",
            "openclaw_deep_authoritative",
            "openclaw_technical",
            "openclaw_energetic",
            "openclaw_crisp",
            "custom",
        ),
        "restart_required": True,
    },
    "openclaw_voice_style": {
        "type": "str",
        "default": "A clear adult male speaking voice with a steady lower register, calm conversational pace, natural articulation, and no humming, sighing, singing, or nonverbal sounds",
        "restart_required": True,
    },
    "tts_backend": {
        "type": "str",
        "default": "voxcpm2",
        "choices": ("voxcpm2", "voicebox"),
        "restart_required": True,
    },
    "voicebox_url": {
        "type": "str",
        "default": "http://127.0.0.1:17493",
        "restart_required": True,
    },
    "hermes_voicebox_profile_id": {
        "type": "str",
        "default": "",
        "restart_required": False,
    },
    "openclaw_voicebox_profile_id": {
        "type": "str",
        "default": "",
        "restart_required": False,
    },
}


def default_options() -> dict[str, Any]:
    return {key: deepcopy(spec["default"]) for key, spec in OPTION_SCHEMA.items()}


def _coerce_option(name: str, value: Any) -> Any:
    spec = OPTION_SCHEMA[name]
    option_type = spec["type"]
    if option_type == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if option_type == "int":
        coerced = int(value)
    elif option_type == "float":
        coerced = float(value)
    elif option_type == "str":
        coerced = "" if value is None else str(value)
    else:
        raise ValueError(f"Unsupported option type for {name}: {option_type}")

    if "min" in spec and coerced < spec["min"]:
        raise ValueError(f"{name} must be >= {spec['min']}")
    if "max" in spec and coerced > spec["max"]:
        raise ValueError(f"{name} must be <= {spec['max']}")
    if "choices" in spec and coerced not in spec["choices"]:
        raise ValueError(f"{name} must be one of: {', '.join(spec['choices'])}")
    return coerced


def normalize_options(raw_options: dict[str, Any]) -> dict[str, Any]:
    options = default_options()
    for name, value in raw_options.items():
        if name not in OPTION_SCHEMA:
            continue
        options[name] = _coerce_option(name, value)
    return options


def load_options(config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    if not config_path.exists():
        return default_options()

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Config file must contain a JSON object: {config_path}")
    return normalize_options(payload)


def save_options(options: dict[str, Any], config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    normalized = normalize_options(options)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return normalized


def options_payload(config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    return {
        "config_path": str(config_path),
        "options": load_options(config_path),
        "schema": OPTION_SCHEMA,
    }
