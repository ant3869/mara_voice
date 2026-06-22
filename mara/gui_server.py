from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.error import URLError
from urllib.request import Request as _UrlRequest
from urllib.request import urlopen

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from mara.agent_state import (
    DEFAULT_AGENT_ROUTE_STATE_PATH,
    load_agent_route_state,
    update_agent_route_state,
)
from mara.agents import AgentSettings, ask_agent, agent_display_name, normalize_agent_id, openclaw_key_configured
from mara.config import DEFAULT_CONFIG_PATH, OPTION_SCHEMA, load_options, options_payload, save_options
from mara.events import DEFAULT_EVENT_LOG_PATH, append_event, read_recent_events
from mara.gui_state import build_state
from mara.pipeline import (
    DEFAULT_AGENT_SESSION_HISTORY_MESSAGES,
    DEFAULT_AGENT_SESSION_HISTORY_PATH,
    DEFAULT_AGENT_SESSION_ID,
    DEFAULT_AGENT_SESSION_PERSISTENCE,
    DEFAULT_AUDIO_DEVICE,
    DEFAULT_HERMES_COMMAND,
    DEFAULT_HERMES_SESSION_ID,
    DEFAULT_OPENCLAW_BASE_URL,
    DEFAULT_OPENCLAW_MODEL,
    DEFAULT_OPENCLAW_SESSION_ID,
    DEFAULT_OPENCLAW_TIMEOUT_SECONDS,
    DEFAULT_SSH_TARGET,
    DEFAULT_TTS_URL,
    PipelineConfig,
    check_tts_health,
    list_output_devices,
    play_audio_bytes,
    synthesize_speech,
)
from mara.safety import redact_sensitive_text


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "gui" / "static"
INDEX_HTML = STATIC_DIR / "index.html"
LAUNCHER_PATH = BASE_DIR / "start_mara.cmd"

EVENT_LOG_PATH = DEFAULT_EVENT_LOG_PATH
CONFIG_PATH = DEFAULT_CONFIG_PATH
AGENT_ROUTE_STATE_PATH = DEFAULT_AGENT_ROUTE_STATE_PATH
TTS_HEALTH_URL = "http://127.0.0.1:8000/healthz"

# Guards so the auto-start (called on every GUI load) cannot spawn duplicate stacks.
_LAUNCH_LOCK = RLock()
_LAUNCH_COOLDOWN_SECONDS = 90.0
_last_launch_attempt = 0.0
_launched_process: subprocess.Popen | None = None
LOGGER = logging.getLogger("mara_voice.gui")


def _pipeline_seems_running() -> bool:
    # TTS health is the most reliable cross-process signal that the stack is up;
    # the launcher always brings the TTS server up before anything else.
    if current_tts_health(timeout_seconds=1.0).get("ok"):
        return True
    proc = _launched_process
    return proc is not None and proc.poll() is None


def _launch_pipeline() -> dict[str, Any]:
    global _last_launch_attempt, _launched_process
    with _LAUNCH_LOCK:
        if _pipeline_seems_running():
            return {"launched": False, "reason": "already running"}
        if not LAUNCHER_PATH.exists():
            return {"launched": False, "reason": f"launcher not found: {LAUNCHER_PATH}"}
        now = time.monotonic()
        if now - _last_launch_attempt < _LAUNCH_COOLDOWN_SECONDS:
            return {"launched": False, "reason": "launch already in progress"}
        _last_launch_attempt = now

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        try:
            # -NoGui because this GUI is already running; we only need TTS + listener.
            _launched_process = subprocess.Popen(
                ["cmd", "/c", str(LAUNCHER_PATH), "-NoGui"],
                cwd=str(BASE_DIR),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                close_fds=True,
            )
        except Exception as exc:
            return {"launched": False, "reason": f"launch failed: {exc}"}
        return {"launched": True, "reason": "pipeline starting"}


def launch_status_payload() -> dict[str, Any]:
    proc = _launched_process
    return {
        "running": _pipeline_seems_running(),
        "launcher_path": str(LAUNCHER_PATH),
        "launcher_available": LAUNCHER_PATH.exists(),
        "launched_pid": proc.pid if (proc is not None and proc.poll() is None) else None,
    }


class OptionsUpdate(BaseModel):
    options: dict[str, Any] = Field(default_factory=dict)


class AgentRouteUpdate(BaseModel):
    active_agent: str | None = None
    next_agent: str | None = None


class ChatRequest(BaseModel):
    agent: str = "hermes"
    text: str = Field(min_length=1, max_length=20000)


app = FastAPI(title="Mara Voice GUI")


def _optional_timeout(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    timeout_seconds = float(value)
    if timeout_seconds <= 0:
        return None
    return timeout_seconds


def _option_int(value: Any, default: int) -> int:
    if value in (None, ""):
        return default
    return int(value)


def _chat_agent_settings(agent_id: str, options: dict[str, Any]) -> AgentSettings:
    history_path = options.get("agent_session_history_path") or DEFAULT_AGENT_SESSION_HISTORY_PATH
    return AgentSettings(
        active_agent=agent_id,
        ssh_target=str(options.get("ssh_target") or DEFAULT_SSH_TARGET),
        hermes_command=str(options.get("hermes_command") or DEFAULT_HERMES_COMMAND),
        ssh_timeout_seconds=_optional_timeout(options.get("ssh_timeout_seconds")),
        ssh_connect_timeout_seconds=_optional_timeout(options.get("ssh_connect_timeout_seconds"), 5.0) or 5.0,
        openclaw_base_url=str(options.get("openclaw_base_url") or DEFAULT_OPENCLAW_BASE_URL),
        openclaw_model=str(options.get("openclaw_model") or DEFAULT_OPENCLAW_MODEL),
        openclaw_timeout_seconds=_optional_timeout(options.get("openclaw_timeout_seconds"), DEFAULT_OPENCLAW_TIMEOUT_SECONDS),
        agent_session_id=str(options.get("agent_session_id") or DEFAULT_AGENT_SESSION_ID),
        hermes_session_id=str(options.get("hermes_session_id") or DEFAULT_HERMES_SESSION_ID),
        openclaw_session_id=str(options.get("openclaw_session_id") or DEFAULT_OPENCLAW_SESSION_ID),
        agent_session_history_path=Path(history_path),
        agent_session_history_messages=_option_int(
            options.get("agent_session_history_messages"),
            DEFAULT_AGENT_SESSION_HISTORY_MESSAGES,
        ),
        agent_session_persistence=bool(options.get("agent_session_persistence", DEFAULT_AGENT_SESSION_PERSISTENCE)),
    )


def _chat_pipeline_config(agent_id: str, options: dict[str, Any]) -> PipelineConfig:
    tts_url = DEFAULT_TTS_URL
    if TTS_HEALTH_URL:
        tts_url = f"{TTS_HEALTH_URL.rsplit('/', 1)[0]}/tts"
    return PipelineConfig(
        active_agent=agent_id,
        tts_url=tts_url,
        tts_health_url=TTS_HEALTH_URL,
        audio_device=options.get("audio_device") or DEFAULT_AUDIO_DEVICE,
    )


def _voice_style_for_agent(agent_id: str, options: dict[str, Any]) -> str:
    if agent_id == "openclaw":
        return str(options.get("openclaw_voice_style") or "")
    return str(options.get("hermes_voice_style") or "")


def _speak_chat_reply(agent_id: str, reply_text: str, options: dict[str, Any]) -> float:
    config = _chat_pipeline_config(agent_id, options)
    config.tts_timeout_seconds = _optional_timeout(options.get("tts_timeout_seconds"), config.tts_timeout_seconds)
    voice_style = _voice_style_for_agent(agent_id, options) or None
    _append_gui_event(
        "status",
        "TALKING",
        f"Generating typed reply voice for {agent_display_name(agent_id)}",
        {"agent": agent_id, "agent_name": agent_display_name(agent_id), "source": "gui_text"},
    )
    wav_bytes = synthesize_speech(reply_text, config, LOGGER, voice_style=voice_style, voice_id=agent_id)
    _append_gui_event(
        "status",
        "TALKING",
        f"Playing typed reply from {agent_display_name(agent_id)}",
        {"agent": agent_id, "agent_name": agent_display_name(agent_id), "source": "gui_text"},
    )
    return play_audio_bytes(wav_bytes, config, LOGGER)


def _append_gui_event(
    event_type: str,
    status: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    try:
        return append_event(event_type, status, message, details=details, event_log_path=EVENT_LOG_PATH)
    except Exception as exc:
        LOGGER.debug("Could not write GUI event: %s", exc)
        return None


def _push_voice_styles_to_tts(options: dict[str, Any]) -> None:
    payload = {k: v for k, v in options.items() if k in ("hermes_voice_style", "openclaw_voice_style") and v}
    if not payload:
        return
    tts_base = TTS_HEALTH_URL.rsplit("/", 1)[0]
    try:
        req = _UrlRequest(
            f"{tts_base}/voice-style",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        urlopen(req, timeout=3.0)
    except (URLError, OSError):
        pass


def _push_voicebox_profile_map(options: dict[str, Any]) -> None:
    payload: dict[str, str] = {}
    if "hermes_voicebox_profile_id" in options:
        payload["hermes_profile_id"] = options["hermes_voicebox_profile_id"] or ""
    if "openclaw_voicebox_profile_id" in options:
        payload["openclaw_profile_id"] = options["openclaw_voicebox_profile_id"] or ""
    if not payload:
        return
    tts_base = TTS_HEALTH_URL.rsplit("/", 1)[0]
    try:
        req = _UrlRequest(
            f"{tts_base}/voicebox/profile-map",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        urlopen(req, timeout=3.0)
    except (URLError, OSError):
        pass


def current_tts_health(timeout_seconds: float = 2.0) -> dict[str, Any]:
    return check_tts_health(
        PipelineConfig(tts_health_url=TTS_HEALTH_URL),
        timeout_seconds=timeout_seconds,
    )


def current_options_payload() -> dict[str, Any]:
    payload = options_payload(CONFIG_PATH)
    payload["audio_devices"] = list_output_devices()
    payload["tts_health"] = current_tts_health(timeout_seconds=1.5)
    payload["agent_route"] = current_agent_route_payload()
    payload["restart_required"] = True
    return payload


def current_agent_route_payload() -> dict[str, Any]:
    options = load_options(CONFIG_PATH)
    state = load_agent_route_state(
        AGENT_ROUTE_STATE_PATH,
        fallback_active_agent=str(options.get("active_agent") or "hermes"),
    )
    return {
        "active_agent": state.active_agent,
        "next_agent": state.next_agent,
        "state_path": str(AGENT_ROUTE_STATE_PATH),
        "openclaw_key_configured": openclaw_key_configured(),
    }


@app.get("/")
def index() -> FileResponse:
    if not INDEX_HTML.exists():
        raise HTTPException(status_code=404, detail=f"Missing GUI file: {INDEX_HTML}")
    return FileResponse(
        INDEX_HTML,
        headers={
            "Cache-Control": "no-store, max-age=0, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/state")
def api_state(limit: int = 500, recent_limit: int = 80) -> dict[str, Any]:
    events = read_recent_events(limit=max(1, min(limit, 5000)), event_log_path=EVENT_LOG_PATH)
    state = build_state(events, recent_limit=max(1, min(recent_limit, 500)))
    payload = asdict(state)
    payload["event_log_path"] = str(EVENT_LOG_PATH)
    payload["tts_health"] = current_tts_health(timeout_seconds=1.0)
    payload["agent_route"] = current_agent_route_payload()
    return payload


@app.get("/api/events")
def api_events(limit: int = 200) -> dict[str, Any]:
    return {
        "event_log_path": str(EVENT_LOG_PATH),
        "events": read_recent_events(limit=max(1, min(limit, 5000)), event_log_path=EVENT_LOG_PATH),
    }


@app.get("/api/options")
def api_options() -> dict[str, Any]:
    try:
        return current_options_payload()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/voicebox/profiles")
def api_voicebox_profiles() -> list:
    options = load_options(CONFIG_PATH)
    voicebox_url = str(options.get("voicebox_url") or "http://127.0.0.1:17493").rstrip("/")
    try:
        req = _UrlRequest(f"{voicebox_url}/profiles")
        with urlopen(req, timeout=5.0) as resp:
            return json.loads(resp.read())
    except (URLError, OSError):
        return []


@app.post("/api/options")
def api_save_options(update: OptionsUpdate) -> dict[str, Any]:
    try:
        existing = load_options(CONFIG_PATH)
        existing.update(update.options)
        save_options(existing, CONFIG_PATH)
        _push_voice_styles_to_tts(update.options)
        _push_voicebox_profile_map(update.options)
        if "active_agent" in update.options:
            update_agent_route_state(
                AGENT_ROUTE_STATE_PATH,
                fallback_active_agent=str(update.options.get("active_agent") or existing.get("active_agent") or "hermes"),
                active_agent=str(update.options["active_agent"]),
            )
        return current_options_payload()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "event_log_path": str(EVENT_LOG_PATH),
        "config_path": str(CONFIG_PATH),
        "agent_route_state_path": str(AGENT_ROUTE_STATE_PATH),
        "option_schema_keys": sorted(OPTION_SCHEMA.keys()),
        "agent_route": current_agent_route_payload(),
        "tts_health_url": TTS_HEALTH_URL,
        "tts_health": current_tts_health(timeout_seconds=1.0),
    }


@app.get("/api/route")
def api_agent_route() -> dict[str, Any]:
    try:
        return current_agent_route_payload()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/route")
def api_save_agent_route(update: AgentRouteUpdate) -> dict[str, Any]:
    try:
        options = load_options(CONFIG_PATH)
        fallback_active_agent = str(options.get("active_agent") or "hermes")
        if update.active_agent is not None:
            options["active_agent"] = update.active_agent
            save_options(options, CONFIG_PATH)
        update_agent_route_state(
            AGENT_ROUTE_STATE_PATH,
            fallback_active_agent=fallback_active_agent,
            active_agent=update.active_agent,
            next_agent=update.next_agent,
        )
        return current_agent_route_payload()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/chat")
def api_chat(update: ChatRequest) -> dict[str, Any]:
    try:
        agent_id = normalize_agent_id(update.agent)
        input_text = update.text.strip()
        if not input_text:
            raise ValueError("Chat text cannot be blank.")

        options = load_options(CONFIG_PATH)
        settings = _chat_agent_settings(agent_id, options)
        status_interval = _optional_timeout(options.get("status_interval_seconds"), 10.0) or 0.0
        agent_name = agent_display_name(agent_id)

        _append_gui_event(
            "conversation",
            "USER",
            "Typed user message",
            {"text": input_text, "agent": agent_id, "agent_name": agent_name, "source": "gui_text"},
        )

        def status_callback(status: str, message: str, details: dict[str, Any] | None = None) -> None:
            event_details = dict(details or {})
            event_details.setdefault("agent", agent_id)
            event_details.setdefault("agent_name", agent_name)
            event_details["source"] = "gui_text"
            _append_gui_event("status", status, message, event_details)

        reply = ask_agent(
            input_text,
            settings,
            LOGGER,
            status_callback=status_callback,
            status_interval_seconds=status_interval,
        )
        safe_reply = redact_sensitive_text(reply.text)
        tts_seconds: float | None = None
        voice_error: str | None = None
        _append_gui_event(
            "conversation",
            "MARA",
            f"{reply.agent_name} typed reply",
            {
                "text": safe_reply,
                "agent": reply.agent_id,
                "agent_name": reply.agent_name,
                "agent_seconds": round(reply.elapsed_seconds, 3),
                "source": "gui_text",
            },
        )
        try:
            tts_seconds = _speak_chat_reply(reply.agent_id, safe_reply, options)
        except Exception as exc:
            voice_error = redact_sensitive_text(str(exc))
            _append_gui_event(
                "error",
                "ERROR",
                f"Typed reply voice failed: {voice_error}",
                {"agent": reply.agent_id, "agent_name": reply.agent_name, "source": "gui_text", "error": voice_error},
            )

        metric_details: dict[str, Any] = {
            "agent": reply.agent_id,
            "agent_name": reply.agent_name,
            "agent_seconds": round(reply.elapsed_seconds, 3),
            "source": "gui_text",
        }
        if tts_seconds is not None:
            metric_details["tts_seconds"] = round(tts_seconds, 3)
        _append_gui_event(
            "metric",
            "TIMING",
            f"{reply.agent_name} replied in {reply.elapsed_seconds:.2f}s",
            metric_details,
        )
        _append_gui_event(
            "status",
            "LISTENING",
            "Ready",
            {"agent": reply.agent_id, "agent_name": reply.agent_name, "source": "gui_text"},
        )
        return {
            "ok": True,
            "agent": reply.agent_id,
            "agent_name": reply.agent_name,
            "text": safe_reply,
            "elapsed_seconds": round(reply.elapsed_seconds, 3),
            "voice_ok": voice_error is None,
            "voice_error": voice_error,
            "tts_seconds": round(tts_seconds, 3) if tts_seconds is not None else None,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        safe_error = redact_sensitive_text(str(exc))
        _append_gui_event(
            "error",
            "ERROR",
            f"Typed chat failed: {safe_error}",
            {"agent": update.agent, "source": "gui_text", "error": safe_error},
        )
        raise HTTPException(status_code=500, detail=safe_error) from exc


@app.get("/api/launch")
def api_launch_status() -> dict[str, Any]:
    return launch_status_payload()


@app.post("/api/launch")
def api_launch() -> dict[str, Any]:
    result = _launch_pipeline()
    result.update(launch_status_payload())
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local Mara Voice GUI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--event-log", default=str(DEFAULT_EVENT_LOG_PATH))
    parser.add_argument("--config-path", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--agent-route-state-path", default=str(DEFAULT_AGENT_ROUTE_STATE_PATH))
    parser.add_argument("--tts-health-url", default=TTS_HEALTH_URL)
    return parser


def main() -> int:
    global EVENT_LOG_PATH, CONFIG_PATH, AGENT_ROUTE_STATE_PATH, TTS_HEALTH_URL

    args = build_parser().parse_args()
    EVENT_LOG_PATH = Path(args.event_log)
    CONFIG_PATH = Path(args.config_path)
    AGENT_ROUTE_STATE_PATH = Path(args.agent_route_state_path)
    TTS_HEALTH_URL = args.tts_health_url
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
