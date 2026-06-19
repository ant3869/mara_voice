from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from mara_agent_state import (
    DEFAULT_AGENT_ROUTE_STATE_PATH,
    load_agent_route_state,
    update_agent_route_state,
)
from mara_agents import openclaw_key_configured
from mara_config import DEFAULT_CONFIG_PATH, load_options, options_payload, save_options
from mara_events import DEFAULT_EVENT_LOG_PATH, read_recent_events
from mara_gui_state import build_state
from mara_pipeline import PipelineConfig, check_tts_health, list_output_devices


BASE_DIR = Path(__file__).resolve().parent
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


app = FastAPI(title="Mara Voice GUI")


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
    return FileResponse(INDEX_HTML)


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


@app.post("/api/options")
def api_save_options(update: OptionsUpdate) -> dict[str, Any]:
    try:
        existing = load_options(CONFIG_PATH)
        existing.update(update.options)
        save_options(existing, CONFIG_PATH)
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
