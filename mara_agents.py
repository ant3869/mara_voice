from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from queue import Empty, Queue
import shlex
import subprocess
import time
from threading import Thread
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import requests


AGENT_HERMES = "hermes"
AGENT_OPENCLAW = "openclaw"
VALID_AGENT_IDS = (AGENT_HERMES, AGENT_OPENCLAW)

DEFAULT_ACTIVE_AGENT = os.getenv("MARA_ACTIVE_AGENT", AGENT_HERMES)
DEFAULT_OPENCLAW_BASE_URL = os.getenv("MARA_OPENCLAW_BASE_URL", "http://192.168.0.65:8645/v1")
DEFAULT_OPENCLAW_MODEL = os.getenv("MARA_OPENCLAW_MODEL", "gemini 3.1 pro")
DEFAULT_OPENCLAW_API_KEY_ENV = "MARA_OPENCLAW_API_KEY"


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

    remote_command = f"{settings.hermes_command} -z {shlex.quote(input_text)}"
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
        len(input_text),
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
        "messages": [{"role": "user", "content": input_text}],
        "stream": False,
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
