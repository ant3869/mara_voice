from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
from queue import Empty, Queue
import shlex
import subprocess
import time
from threading import RLock, Thread
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import requests

from mara_env import load_dotenv


load_dotenv()

AGENT_HERMES = "hermes"
AGENT_OPENCLAW = "openclaw"
VALID_AGENT_IDS = (AGENT_HERMES, AGENT_OPENCLAW)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ACTIVE_AGENT = os.getenv("MARA_ACTIVE_AGENT", AGENT_HERMES)
DEFAULT_OPENCLAW_BASE_URL = os.getenv("MARA_OPENCLAW_BASE_URL", "http://192.168.0.65:8645/v1")
DEFAULT_OPENCLAW_MODEL = os.getenv("MARA_OPENCLAW_MODEL", "gemini 3.1 pro")
DEFAULT_OPENCLAW_API_KEY_ENV = "MARA_OPENCLAW_API_KEY"
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
    agent_session_id: str = DEFAULT_AGENT_SESSION_ID
    hermes_session_id: str = DEFAULT_HERMES_SESSION_ID
    openclaw_session_id: str = DEFAULT_OPENCLAW_SESSION_ID
    agent_session_history_path: Path = DEFAULT_AGENT_SESSION_HISTORY_PATH
    agent_session_history_messages: int = DEFAULT_AGENT_SESSION_HISTORY_MESSAGES
    agent_session_persistence: bool = DEFAULT_AGENT_SESSION_PERSISTENCE


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


def _coerce_history_messages(raw_messages: object) -> list[dict[str, str]]:
    if not isinstance(raw_messages, list):
        return []
    messages: list[dict[str, str]] = []
    for item in raw_messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        messages.append({"role": role, "content": _trim_session_content(content)})
    return messages


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
    return messages[-limit:]


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
        messages.extend(
            [
                {"role": "user", "content": _trim_session_content(user_text)},
                {"role": "assistant", "content": _trim_session_content(assistant_text)},
            ]
        )
        session[normalized_agent] = messages[-limit:]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(path)


def _openclaw_messages(input_text: str, settings: AgentSettings) -> list[dict[str, str]]:
    history = load_session_messages(settings, AGENT_OPENCLAW)
    return [*history, {"role": "user", "content": input_text}]


def _hermes_input_text(input_text: str, settings: AgentSettings) -> str:
    history = load_session_messages(settings, AGENT_HERMES)
    if not history:
        return input_text

    lines = [
        f'Continue Mara Voice session "{agent_session_id_for_agent(settings, AGENT_HERMES)}".',
        "Use the prior turns only as context. Answer the latest user request directly.",
        "",
        "Prior turns:",
    ]
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
    try:
        record_session_turn(settings, AGENT_OPENCLAW, input_text, reply)
    except Exception as exc:
        logger.warning("Could not persist OpenClaw session history: %s", exc)
    return AgentReply(AGENT_OPENCLAW, agent_display_name(AGENT_OPENCLAW), reply, elapsed)


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
