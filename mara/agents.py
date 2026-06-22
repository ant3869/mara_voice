from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import re
from pathlib import Path
from queue import Empty, Queue
import shlex
import subprocess
import time
from threading import RLock, Thread
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import requests

from mara.env import load_dotenv


load_dotenv()

AGENT_HERMES = "hermes"
AGENT_OPENCLAW = "openclaw"
VALID_AGENT_IDS = (AGENT_HERMES, AGENT_OPENCLAW)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ACTIVE_AGENT = os.getenv("MARA_ACTIVE_AGENT", AGENT_HERMES)
DEFAULT_OPENCLAW_BASE_URL = os.getenv("MARA_OPENCLAW_BASE_URL", "http://192.168.0.65:8645/v1")
DEFAULT_OPENCLAW_MODEL = os.getenv("MARA_OPENCLAW_MODEL", "gemini 3.1 pro")
DEFAULT_OPENCLAW_API_KEY_ENV = "MARA_OPENCLAW_API_KEY"
# Shared spoken-audio style guidance appended to every persona. Replies are read aloud
# by TTS, so they must stay short and free of anything that sounds wrong when spoken.
_AUDIO_STYLE_GUIDANCE = (
    " Keep replies brief and conversational: say more with fewer words, usually one to three "
    "short sentences. Your reply is read aloud by a text-to-speech voice, so write only plain "
    "spoken words: no markdown, code blocks, bullet lists, headings, emoji, symbols, URLs, or "
    "file paths. Spell out anything technical the way you would naturally say it out loud."
)

# Built-in persona prompts. These anchor each agent's identity so the underlying
# model (or a remote gateway with its own system prompt) cannot drift into claiming
# to be the other assistant. Precedence at load time:
#   MARA_<AGENT>_SYSTEM_PROMPT env var > config/mara_personas.json > built-in below.
_BUILTIN_OPENCLAW_SYSTEM_PROMPT = (
    "You are Claw, the user's voice assistant, talking to Ant. "
    "Always speak and refer to yourself as Claw. "
    "You are NOT Mara and you are NOT Hermes: Mara (run by the Hermes agent) is a completely "
    "separate assistant, not you and not the software running you. Never claim to be Mara or Hermes. "
    "Do not describe yourself as 'the software running in the background.' "
    "You cannot message Mara directly; never pretend to relay messages to or from her. "
    "Answer only the user's latest request and do not bring up old, already-finished tasks unless asked."
    + _AUDIO_STYLE_GUIDANCE
)
_BUILTIN_HERMES_SYSTEM_PROMPT = (
    "You are Mara, the user's voice assistant, talking to Ant. "
    "Always speak and refer to yourself as Mara. "
    "You are NOT Claw and you are NOT OpenClaw: Claw is a completely separate assistant, not you. "
    "Never claim to be Claw. "
    "You cannot message Claw directly; never pretend to relay messages to or from him. "
    "Answer only the user's latest request and do not bring up old, already-finished tasks unless asked."
    + _AUDIO_STYLE_GUIDANCE
)
DEFAULT_PERSONA_CONFIG_PATH = Path(
    os.getenv("MARA_PERSONA_CONFIG_PATH") or str(BASE_DIR / "config" / "mara_personas.json")
)


def _load_persona_file() -> dict[str, str]:
    path = DEFAULT_PERSONA_CONFIG_PATH
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def load_persona_prompt(agent_id: str, builtin: str) -> str:
    """Resolve an agent's identity prompt: env override, then persona file, then built-in."""
    env_value = os.getenv(f"MARA_{agent_id.upper()}_SYSTEM_PROMPT")
    if env_value is not None:
        return env_value
    file_value = _load_persona_file().get(agent_id)
    if file_value and file_value.strip():
        return file_value
    return builtin


DEFAULT_OPENCLAW_SYSTEM_PROMPT = load_persona_prompt(AGENT_OPENCLAW, _BUILTIN_OPENCLAW_SYSTEM_PROMPT)
DEFAULT_HERMES_SYSTEM_PROMPT = load_persona_prompt(AGENT_HERMES, _BUILTIN_HERMES_SYSTEM_PROMPT)
DEFAULT_AGENT_SESSION_ID = os.getenv("MARA_AGENT_SESSION_ID", "voice-session")
DEFAULT_HERMES_SESSION_ID = os.getenv("MARA_HERMES_SESSION_ID", "")
DEFAULT_OPENCLAW_SESSION_ID = os.getenv("MARA_OPENCLAW_SESSION_ID", "")
_DEFAULT_AGENT_SESSION_HISTORY_PATH_TEXT = str(BASE_DIR / "config" / "mara_agent_sessions.json")
DEFAULT_AGENT_SESSION_HISTORY_PATH = Path(
    os.getenv("MARA_AGENT_SESSION_HISTORY_PATH") or _DEFAULT_AGENT_SESSION_HISTORY_PATH_TEXT
)
DEFAULT_AGENT_SESSION_HISTORY_MESSAGES = int(os.getenv("MARA_AGENT_SESSION_HISTORY_MESSAGES", "20"))
DEFAULT_AGENT_SESSION_ENTRY_CHAR_LIMIT = int(os.getenv("MARA_AGENT_SESSION_ENTRY_CHAR_LIMIT", "2000"))

_SESSION_LOCK = RLock()


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _parse_optional_timeout(value: str | None, default: float | None) -> float | None:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in ("", "0", "none", "off", "false", "disabled", "never"):
        return None
    timeout_seconds = float(normalized)
    if timeout_seconds <= 0:
        return None
    return timeout_seconds


DEFAULT_OPENCLAW_TIMEOUT_SECONDS = _parse_optional_timeout(os.getenv("MARA_OPENCLAW_TIMEOUT"), 600.0)
DEFAULT_AGENT_SESSION_PERSISTENCE = _parse_bool(os.getenv("MARA_AGENT_SESSION_PERSISTENCE"), True)
# Drop replayed history older than this so finished tasks and stale identity slips
# stop resurfacing turns later. None/0 disables the age filter (count cap still applies).
DEFAULT_AGENT_SESSION_HISTORY_TTL_SECONDS = _parse_optional_timeout(
    os.getenv("MARA_AGENT_SESSION_HISTORY_TTL_SECONDS"), 900.0
)
# Post-reply identity/fabrication guard. Corrects blatant self-misidentification and
# flags fabricated cross-agent relay claims.
DEFAULT_AGENT_GUARDRAIL_ENABLED = _parse_bool(os.getenv("MARA_AGENT_GUARDRAIL"), True)


class AgentError(RuntimeError):
    pass


StatusCallback = Callable[[str, str, dict[str, Any] | None], None]


@dataclass(slots=True)
class AgentSettings:
    active_agent: str = DEFAULT_ACTIVE_AGENT
    ssh_target: str = ""
    hermes_command: str = ""
    ssh_timeout_seconds: float | None = None
    ssh_connect_timeout_seconds: float = 5.0
    openclaw_base_url: str = DEFAULT_OPENCLAW_BASE_URL
    openclaw_model: str = DEFAULT_OPENCLAW_MODEL
    openclaw_timeout_seconds: float | None = DEFAULT_OPENCLAW_TIMEOUT_SECONDS
    openclaw_api_key: str = ""
    openclaw_system_prompt: str = DEFAULT_OPENCLAW_SYSTEM_PROMPT
    hermes_system_prompt: str = DEFAULT_HERMES_SYSTEM_PROMPT
    agent_session_id: str = DEFAULT_AGENT_SESSION_ID
    hermes_session_id: str = DEFAULT_HERMES_SESSION_ID
    openclaw_session_id: str = DEFAULT_OPENCLAW_SESSION_ID
    agent_session_history_path: Path = DEFAULT_AGENT_SESSION_HISTORY_PATH
    agent_session_history_messages: int = DEFAULT_AGENT_SESSION_HISTORY_MESSAGES
    agent_session_history_ttl_seconds: float | None = DEFAULT_AGENT_SESSION_HISTORY_TTL_SECONDS
    agent_session_persistence: bool = DEFAULT_AGENT_SESSION_PERSISTENCE
    agent_guardrail_enabled: bool = DEFAULT_AGENT_GUARDRAIL_ENABLED


@dataclass(slots=True)
class AgentReply:
    agent_id: str
    agent_name: str
    text: str
    elapsed_seconds: float


def normalize_agent_id(agent_id: str | None) -> str:
    normalized = (agent_id or AGENT_HERMES).strip().lower()
    if normalized not in VALID_AGENT_IDS:
        raise ValueError(f"Unsupported agent '{agent_id}'. Expected one of: {', '.join(VALID_AGENT_IDS)}")
    return normalized


def agent_display_name(agent_id: str) -> str:
    normalized = normalize_agent_id(agent_id)
    if normalized == AGENT_OPENCLAW:
        return "OpenClaw"
    return "Hermes"


def format_timeout(timeout_seconds: float | None) -> str:
    if timeout_seconds is None:
        return "disabled"
    return f"{timeout_seconds:.1f}s"


def openclaw_key_configured(settings: AgentSettings | None = None) -> bool:
    if settings is not None and settings.openclaw_api_key:
        return True
    return bool(os.getenv(DEFAULT_OPENCLAW_API_KEY_ENV))


def _emit(status_callback: StatusCallback | None, status: str, message: str, details: dict[str, Any] | None = None) -> None:
    if status_callback is not None:
        status_callback(status, message, details)


def _agent_session_id(settings: AgentSettings) -> str:
    return (settings.agent_session_id or DEFAULT_AGENT_SESSION_ID).strip() or "voice-session"


def default_agent_session_id_for_agent(base_session_id: str, agent_id: str) -> str:
    base = (base_session_id or DEFAULT_AGENT_SESSION_ID).strip() or "voice-session"
    normalized_agent = normalize_agent_id(agent_id)
    return f"{base}-{normalized_agent}"


def agent_session_id_for_agent(settings: AgentSettings, agent_id: str) -> str:
    normalized_agent = normalize_agent_id(agent_id)
    if normalized_agent == AGENT_HERMES:
        configured = (settings.hermes_session_id or DEFAULT_HERMES_SESSION_ID).strip()
    else:
        configured = (settings.openclaw_session_id or DEFAULT_OPENCLAW_SESSION_ID).strip()
    return configured or default_agent_session_id_for_agent(_agent_session_id(settings), normalized_agent)


def _session_message_limit(settings: AgentSettings) -> int:
    return max(0, int(settings.agent_session_history_messages))


def _trim_session_content(text: str) -> str:
    normalized = " ".join(str(text).split())
    if len(normalized) <= DEFAULT_AGENT_SESSION_ENTRY_CHAR_LIMIT:
        return normalized
    return normalized[:DEFAULT_AGENT_SESSION_ENTRY_CHAR_LIMIT].rstrip() + "..."


def _load_session_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "sessions": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "sessions": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "sessions": {}}
    sessions = payload.get("sessions")
    if not isinstance(sessions, dict):
        payload["sessions"] = {}
    payload.setdefault("version", 1)
    return payload


def _coerce_history_messages(raw_messages: object) -> list[dict[str, Any]]:
    """Parse stored turns, preserving an optional ``ts`` (epoch seconds) for TTL filtering."""
    if not isinstance(raw_messages, list):
        return []
    messages: list[dict[str, Any]] = []
    for item in raw_messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        entry: dict[str, Any] = {"role": role, "content": _trim_session_content(content)}
        ts = item.get("ts")
        if isinstance(ts, (int, float)):
            entry["ts"] = float(ts)
        messages.append(entry)
    return messages


def _filter_expired_messages(
    messages: list[dict[str, Any]], ttl_seconds: float | None
) -> list[dict[str, Any]]:
    if not ttl_seconds or ttl_seconds <= 0:
        return messages
    cutoff = time.time() - ttl_seconds
    # Turns without a stored timestamp are treated as fresh (kept) for backwards
    # compatibility; only turns we know are older than the TTL are dropped.
    return [m for m in messages if float(m.get("ts", cutoff)) >= cutoff]


def _strip_history_metadata(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [{"role": m["role"], "content": m["content"]} for m in messages]


def load_session_messages(settings: AgentSettings, agent_id: str) -> list[dict[str, str]]:
    if not settings.agent_session_persistence:
        return []
    normalized_agent = normalize_agent_id(agent_id)
    session_id = agent_session_id_for_agent(settings, normalized_agent)
    legacy_session_id = _agent_session_id(settings)
    path = Path(settings.agent_session_history_path)
    with _SESSION_LOCK:
        payload = _load_session_payload(path)
        sessions = payload.get("sessions") if isinstance(payload.get("sessions"), dict) else {}
        session = sessions.get(session_id) if isinstance(sessions.get(session_id), dict) else {}
        messages = _coerce_history_messages(session.get(normalized_agent) if isinstance(session, dict) else [])
        if not messages and legacy_session_id != session_id:
            legacy_session = sessions.get(legacy_session_id) if isinstance(sessions.get(legacy_session_id), dict) else {}
            messages = _coerce_history_messages(
                legacy_session.get(normalized_agent) if isinstance(legacy_session, dict) else []
            )
    limit = _session_message_limit(settings)
    if limit <= 0:
        return []
    messages = _filter_expired_messages(messages, settings.agent_session_history_ttl_seconds)
    return _strip_history_metadata(messages[-limit:])


def record_session_turn(settings: AgentSettings, agent_id: str, user_text: str, assistant_text: str) -> None:
    if not settings.agent_session_persistence:
        return
    limit = _session_message_limit(settings)
    if limit <= 0:
        return
    normalized_agent = normalize_agent_id(agent_id)
    session_id = agent_session_id_for_agent(settings, normalized_agent)
    path = Path(settings.agent_session_history_path)
    with _SESSION_LOCK:
        payload = _load_session_payload(path)
        sessions = payload.setdefault("sessions", {})
        if not isinstance(sessions, dict):
            sessions = {}
            payload["sessions"] = sessions
        session = sessions.setdefault(session_id, {})
        if not isinstance(session, dict):
            session = {}
            sessions[session_id] = session
        messages = _coerce_history_messages(session.get(normalized_agent))
        now = time.time()
        messages.extend(
            [
                {"role": "user", "content": _trim_session_content(user_text), "ts": now},
                {"role": "assistant", "content": _trim_session_content(assistant_text), "ts": now},
            ]
        )
        session[normalized_agent] = messages[-limit:]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(path)


def _openclaw_messages(input_text: str, settings: AgentSettings) -> list[dict[str, str]]:
    history = load_session_messages(settings, AGENT_OPENCLAW)
    messages: list[dict[str, str]] = []
    system_prompt = (settings.openclaw_system_prompt or "").strip()
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(history)
    messages.append({"role": "user", "content": input_text})
    return messages


def _hermes_input_text(input_text: str, settings: AgentSettings) -> str:
    history = load_session_messages(settings, AGENT_HERMES)
    system_prompt = (settings.hermes_system_prompt or "").strip()
    if not history:
        if system_prompt:
            return f"{system_prompt}\n\nLatest user request:\n{input_text}"
        return input_text

    lines: list[str] = []
    if system_prompt:
        lines.extend([system_prompt, ""])
    lines.extend(
        [
            f'Continue Mara Voice session "{agent_session_id_for_agent(settings, AGENT_HERMES)}".',
            "Use the prior turns only as context. Answer the latest user request directly.",
            "",
            "Prior turns:",
        ]
    )
    for message in history:
        role = "User" if message["role"] == "user" else "Mara"
        lines.append(f"{role}: {message['content']}")
    lines.extend(["", "Latest user request:", input_text])
    return "\n".join(lines)


def _openclaw_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    netloc = parsed.netloc
    if "@" in netloc:
        _, host = netloc.rsplit("@", 1)
        netloc = f"<redacted>@{host}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _openclaw_headers(settings: AgentSettings) -> dict[str, str]:
    api_key = settings.openclaw_api_key or os.getenv(DEFAULT_OPENCLAW_API_KEY_ENV, "")
    if not api_key:
        raise AgentError(
            f"OpenClaw API key is not configured. Set {DEFAULT_OPENCLAW_API_KEY_ENV} before routing prompts to OpenClaw."
        )
    return {
        "authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }


def check_openclaw_health(settings: AgentSettings, session: requests.Session | None = None) -> dict[str, Any]:
    try:
        headers = _openclaw_headers(settings)
    except AgentError as exc:
        return {"ok": False, "error": str(exc), "key_configured": False}

    http = session or requests.Session()
    try:
        response = http.get(
            _openclaw_url(settings.openclaw_base_url, "models"),
            headers=headers,
            timeout=min(settings.openclaw_timeout_seconds or 10.0, 10.0),
        )
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc), "key_configured": True}

    payload: dict[str, Any]
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:200]}

    return {
        "ok": response.ok,
        "status_code": response.status_code,
        "payload": payload,
        "key_configured": True,
    }


def _ask_hermes_ssh(
    input_text: str,
    settings: AgentSettings,
    logger: logging.Logger,
    status_callback: StatusCallback | None,
    status_interval_seconds: float,
) -> AgentReply:
    if not settings.ssh_target:
        raise AgentError("Hermes SSH target is not configured.")
    if not settings.hermes_command:
        raise AgentError("Hermes command is not configured.")

    agent_input = _hermes_input_text(input_text, settings)
    session_prefix = ""
    if settings.agent_session_persistence:
        session_prefix = (
            f"MARA_AGENT_SESSION_ID={shlex.quote(agent_session_id_for_agent(settings, AGENT_HERMES))} "
            f"MARA_AGENT_NAME={shlex.quote(AGENT_HERMES)} "
        )
    remote_command = f"{session_prefix}{settings.hermes_command} -z {shlex.quote(agent_input)}"
    command = [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={int(settings.ssh_connect_timeout_seconds)}",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=8",
        settings.ssh_target,
        remote_command,
    ]

    _emit(
        status_callback,
        "THINKING",
        "Sending prompt to Hermes",
        {"agent": AGENT_HERMES, "target": settings.ssh_target},
    )
    logger.info(
        "Sending %d characters to Hermes on %s (timeout=%s)",
        len(agent_input),
        settings.ssh_target,
        format_timeout(settings.ssh_timeout_seconds),
    )

    start_time = time.monotonic()
    next_status_time = max(0.0, status_interval_seconds)
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        raise AgentError(f"SSH communication failed: {exc}") from exc

    while True:
        elapsed = time.monotonic() - start_time
        if settings.ssh_timeout_seconds is not None and elapsed >= settings.ssh_timeout_seconds:
            process.kill()
            stdout, stderr = process.communicate()
            stderr_text = stderr.strip()
            stdout_text = stdout.strip()
            detail = stderr_text or stdout_text or "no output"
            raise AgentError(
                f"SSH/Hermes response timed out after {settings.ssh_timeout_seconds:.1f}s: {detail}"
            )

        wait_seconds = 0.5
        if settings.ssh_timeout_seconds is not None:
            wait_seconds = max(0.05, min(wait_seconds, settings.ssh_timeout_seconds - elapsed))

        try:
            stdout, stderr = process.communicate(timeout=wait_seconds)
            break
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start_time
            if status_interval_seconds and elapsed >= next_status_time:
                if settings.ssh_timeout_seconds is None:
                    detail = "no SSH response timeout"
                else:
                    detail = f"{max(0.0, settings.ssh_timeout_seconds - elapsed):.0f}s before timeout"
                _emit(
                    status_callback,
                    "THINKING",
                    f"Hermes is still working over SSH ({elapsed:.0f}s elapsed, {detail})",
                    {"agent": AGENT_HERMES, "elapsed_seconds": round(elapsed, 3)},
                )
                next_status_time += status_interval_seconds
            continue
        except KeyboardInterrupt:
            process.kill()
            process.communicate()
            raise
        except Exception as exc:
            process.kill()
            process.communicate()
            raise AgentError(f"SSH communication failed: {exc}") from exc

    elapsed = time.monotonic() - start_time
    stdout_text = stdout.strip()
    stderr_text = stderr.strip()
    if process.returncode != 0:
        raise AgentError(
            f"SSH/Hermes failed with exit code {process.returncode}: {stderr_text or stdout_text or 'no output'}"
        )
    if not stdout_text:
        raise AgentError(f"Hermes returned no reply. stderr={stderr_text or '<empty>'}")
    if stderr_text:
        logger.debug("Hermes stderr: %s", stderr_text)

    logger.info("Hermes replied in %.2fs with %d characters", elapsed, len(stdout_text))
    stdout_text = _apply_guardrail(AGENT_HERMES, stdout_text, settings, logger)
    try:
        record_session_turn(settings, AGENT_HERMES, input_text, stdout_text)
    except Exception as exc:
        logger.warning("Could not persist Hermes session history: %s", exc)
    return AgentReply(AGENT_HERMES, agent_display_name(AGENT_HERMES), stdout_text, elapsed)


def _extract_openclaw_reply(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AgentError("OpenClaw returned no choices.")

    choice = choices[0]
    if not isinstance(choice, dict):
        raise AgentError("OpenClaw returned an invalid choice.")

    message = choice.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()

    text = choice.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    raise AgentError("OpenClaw returned no assistant text.")


def _ask_openclaw_http(
    input_text: str,
    settings: AgentSettings,
    logger: logging.Logger,
    status_callback: StatusCallback | None,
    status_interval_seconds: float,
    session: requests.Session | None,
) -> AgentReply:
    headers = _openclaw_headers(settings)
    http = session or requests.Session()
    payload = {
        "model": settings.openclaw_model,
        "messages": _openclaw_messages(input_text, settings),
        "stream": False,
        "user": agent_session_id_for_agent(settings, AGENT_OPENCLAW),
    }
    url = _openclaw_url(settings.openclaw_base_url, "chat/completions")

    _emit(
        status_callback,
        "THINKING",
        "Sending prompt to OpenClaw",
        {"agent": AGENT_OPENCLAW, "base_url": redact_url(settings.openclaw_base_url), "model": settings.openclaw_model},
    )
    logger.info(
        "Sending %d characters to OpenClaw at %s (model=%s, timeout=%s)",
        len(input_text),
        redact_url(settings.openclaw_base_url),
        settings.openclaw_model,
        format_timeout(settings.openclaw_timeout_seconds),
    )
    result_queue: Queue[tuple[requests.Response | None, BaseException | None]] = Queue(maxsize=1)

    def worker() -> None:
        try:
            result_queue.put(
                (
                    http.post(
                        url,
                        headers=headers,
                        data=json.dumps(payload),
                        timeout=settings.openclaw_timeout_seconds,
                    ),
                    None,
                )
            )
        except BaseException as exc:
            result_queue.put((None, exc))

    start_time = time.monotonic()
    next_status_time = max(0.0, status_interval_seconds)
    Thread(target=worker, daemon=True).start()

    while True:
        try:
            response, error = result_queue.get(timeout=0.5)
            break
        except Empty:
            elapsed = time.monotonic() - start_time
            if status_interval_seconds and elapsed >= next_status_time:
                _emit(
                    status_callback,
                    "THINKING",
                    f"OpenClaw is still working over HTTP ({elapsed:.0f}s elapsed)",
                    {"agent": AGENT_OPENCLAW, "elapsed_seconds": round(elapsed, 3)},
                )
                next_status_time += status_interval_seconds

    elapsed = time.monotonic() - start_time
    if error is not None:
        if isinstance(error, requests.Timeout):
            raise AgentError(f"OpenClaw timed out after {format_timeout(settings.openclaw_timeout_seconds)}.") from error
        if isinstance(error, requests.RequestException):
            raise AgentError(f"OpenClaw request failed: {error}") from error
        raise AgentError(f"OpenClaw request failed: {error}") from error
    if response is None:
        raise AgentError("OpenClaw returned no HTTP response.")
    if not response.ok:
        body = response.text[:400].strip()
        raise AgentError(f"OpenClaw returned HTTP {response.status_code}: {body or '<empty response>'}")

    try:
        response_payload = response.json()
    except ValueError as exc:
        raise AgentError("OpenClaw returned invalid JSON.") from exc

    reply = _extract_openclaw_reply(response_payload)
    logger.info("OpenClaw replied in %.2fs with %d characters", elapsed, len(reply))
    reply = _apply_guardrail(AGENT_OPENCLAW, reply, settings, logger)
    try:
        record_session_turn(settings, AGENT_OPENCLAW, input_text, reply)
    except Exception as exc:
        logger.warning("Could not persist OpenClaw session history: %s", exc)
    return AgentReply(AGENT_OPENCLAW, agent_display_name(AGENT_OPENCLAW), reply, elapsed)


_GUARDRAIL_DISPLAY_NAME = {AGENT_OPENCLAW: "Claw", AGENT_HERMES: "Mara"}
_GUARDRAIL_WRONG_NAMES = {
    AGENT_OPENCLAW: ("Mara", "Hermes"),
    AGENT_HERMES: ("Claw", "OpenClaw"),
}
# Heuristics for fabricated "I talked to the other agent" claims. Flag-only: these
# are surfaced in logs/events but never silently rewritten, since the meaning can't
# be safely repaired.
_FABRICATION_PATTERNS = (
    re.compile(
        r"\bI (?:just |already )?(?:pinged|messaged|texted|relayed|forwarded|"
        r"sent (?:a |the )?(?:message|password|note|word)(?:\s+\w+)? to)\s+(?:her|him|mara|claw)\b",
        re.I,
    ),
    re.compile(r"\b(?:she|he) (?:just )?(?:got it|hit me back|replied|responded|wrote back|messaged me)\b", re.I),
    re.compile(r"\bdropped .{0,40}?\b(?:in|into)\b .{0,20}?\bdiscord\b", re.I),
    re.compile(r"\bshared (?:system )?memory\b", re.I),
)


def guardrail_review(agent_id: str, text: str) -> tuple[str, list[str]]:
    """Anchor agent identity in its own reply.

    Returns ``(possibly_corrected_text, violations)``. Blatant first-person
    self-misidentification ("I am Mara" from Claw) is rewritten to the correct
    name; fabricated cross-agent relay claims are flagged but left untouched.
    """
    try:
        normalized = normalize_agent_id(agent_id)
    except ValueError:
        return text, []
    if not text or not text.strip():
        return text, []

    violations: list[str] = []
    corrected = text
    wrong_names = _GUARDRAIL_WRONG_NAMES.get(normalized, ())
    if wrong_names:
        correct = _GUARDRAIL_DISPLAY_NAME[normalized]
        names = "|".join(re.escape(name) for name in sorted(wrong_names, key=len, reverse=True))
        first_person_pattern = re.compile(
            r"(?P<lead>\b(?:I am still|I am|I'm|Im|my name is)"
            r"(?:\s+(?:actually|really|just|still|literally|currently|now|technically))?\s+)"
            r"(?P<name>" + names + r")\b(?!['\w])",
            re.I,
        )
        curly_first_person_pattern = re.compile(
            r"(?P<lead>\bI’m"
            r"(?:\s+(?:actually|really|just|still|literally|currently|now|technically))?\s+)"
            r"(?P<name>" + names + r")\b(?!['\w])",
            re.I,
        )
        here_pattern = re.compile(r"(?P<name>\b(?:" + names + r"))(?P<trail>\s+here\b)", re.I)
        this_is_pattern = re.compile(
            r"(?P<lead>\b(?:this is|it is|it's|its|call me|you can call me)\s+)"
            r"(?P<name>" + names + r")\b(?!['\w])",
            re.I,
        )

        def _replace(match: "re.Match[str]") -> str:
            violations.append(f"self-identified as {match.group('name')}")
            return match.group("lead") + correct

        def _replace_here(match: "re.Match[str]") -> str:
            violations.append(f"self-identified as {match.group('name')}")
            return correct + match.group("trail")

        for pattern in (first_person_pattern, curly_first_person_pattern, this_is_pattern):
            corrected = pattern.sub(_replace, corrected)
        corrected = here_pattern.sub(_replace_here, corrected)

    if any(fab.search(corrected) for fab in _FABRICATION_PATTERNS):
        violations.append("fabricated cross-agent relay")

    return corrected, violations


def _apply_guardrail(agent_id: str, text: str, settings: AgentSettings, logger: logging.Logger) -> str:
    if not settings.agent_guardrail_enabled:
        return text
    corrected, violations = guardrail_review(agent_id, text)
    if violations:
        logger.warning("Guardrail flagged %s reply: %s", agent_id, "; ".join(sorted(set(violations))))
    return corrected


def ask_agent(
    input_text: str,
    settings: AgentSettings,
    logger: logging.Logger,
    status_callback: StatusCallback | None = None,
    status_interval_seconds: float = 10.0,
    requests_session: requests.Session | None = None,
) -> AgentReply:
    agent_id = normalize_agent_id(settings.active_agent)
    settings.active_agent = agent_id
    if agent_id == AGENT_OPENCLAW:
        return _ask_openclaw_http(input_text, settings, logger, status_callback, status_interval_seconds, requests_session)
    return _ask_hermes_ssh(input_text, settings, logger, status_callback, status_interval_seconds)
