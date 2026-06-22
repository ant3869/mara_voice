from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from mara.agents import AGENT_HERMES, normalize_agent_id


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_AGENT_ROUTE_STATE_PATH = BASE_DIR / "config" / "mara_agent_route.runtime.json"


@dataclass(slots=True)
class AgentRouteState:
    active_agent: str = AGENT_HERMES
    next_agent: str = ""


@dataclass(slots=True)
class ConsumedAgentRoute:
    agent_id: str
    active_agent: str
    one_shot: bool


def _normalize_next_agent(value: str | None) -> str:
    if not value:
        return ""
    return normalize_agent_id(value)


def load_agent_route_state(
    path: Path = DEFAULT_AGENT_ROUTE_STATE_PATH,
    fallback_active_agent: str = AGENT_HERMES,
) -> AgentRouteState:
    active_agent = normalize_agent_id(fallback_active_agent)
    if not path.exists():
        return AgentRouteState(active_agent=active_agent)

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return AgentRouteState(active_agent=active_agent)

    active_agent = normalize_agent_id(str(payload.get("active_agent") or active_agent))
    next_agent = _normalize_next_agent(payload.get("next_agent"))
    return AgentRouteState(active_agent=active_agent, next_agent=next_agent)


def save_agent_route_state(
    state: AgentRouteState,
    path: Path = DEFAULT_AGENT_ROUTE_STATE_PATH,
) -> AgentRouteState:
    state = AgentRouteState(
        active_agent=normalize_agent_id(state.active_agent),
        next_agent=_normalize_next_agent(state.next_agent),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return state


def update_agent_route_state(
    path: Path = DEFAULT_AGENT_ROUTE_STATE_PATH,
    fallback_active_agent: str = AGENT_HERMES,
    active_agent: str | None = None,
    next_agent: str | None = None,
) -> AgentRouteState:
    state = load_agent_route_state(path, fallback_active_agent=fallback_active_agent)
    if active_agent is not None:
        state.active_agent = normalize_agent_id(active_agent)
    if next_agent is not None:
        state.next_agent = _normalize_next_agent(next_agent)
    return save_agent_route_state(state, path)


def consume_agent_route(
    path: Path = DEFAULT_AGENT_ROUTE_STATE_PATH,
    fallback_active_agent: str = AGENT_HERMES,
) -> ConsumedAgentRoute:
    state = load_agent_route_state(path, fallback_active_agent=fallback_active_agent)
    if state.next_agent:
        consumed = ConsumedAgentRoute(
            agent_id=state.next_agent,
            active_agent=state.active_agent,
            one_shot=True,
        )
        state.next_agent = ""
        save_agent_route_state(state, path)
        return consumed

    return ConsumedAgentRoute(
        agent_id=state.active_agent,
        active_agent=state.active_agent,
        one_shot=False,
    )
