# OpenClaw Agent Routing

**Branch:** `feature/openclaw-agent-routing`
**Description:** Add a selectable agent route so voice prompts can go to Hermes or the OpenClaw OpenAI-compatible HTTP server.

## Goal

Mara Voice should keep Hermes as the default path while allowing the user to switch a prompt route to OpenClaw from the GUI. The route switch should be visible in the terminal/GUI status, should not require restarting for ordinary Hermes/OpenClaw switching, and should keep OpenClaw API credentials out of logs and committed config.

## Known OpenClaw Target

- Host: `192.168.0.65`
- Port: `8645`
- Base URL assumption: `http://192.168.0.65:8645/v1`
- Interface assumption: OpenAI-compatible chat completions API, likely `POST /v1/chat/completions`
- Auth assumption: `Authorization: Bearer <API_SERVER_KEY>` supplied locally as `MARA_OPENCLAW_API_KEY`
- Model: `gemini 3.1 pro`
- Remote prerequisite: `API_SERVER_ENABLED=true` in the Dell rig's `~/.hermes/.env`, followed by a gateway restart.
- Diagnostics call `/v1/models` to confirm the gateway is reachable; the request model is configurable in the GUI and defaults to `gemini 3.1 pro`.

## Implementation Steps

### Step 1: Add Shared Agent Routing Adapters

**Files:** `mara_agents.py`, `mara_pipeline.py`, `jarvis_listener.py`, `tests/test_agent_routing.py`

**What:** Introduce a small routing module with two adapters: `HermesSshAgent` and `OpenClawHttpAgent`. Move the duplicated SSH prompt logic from `mara_pipeline.py` and `jarvis_listener.py` behind a shared `ask_agent()` function so both one-shot CLI and live voice listener use the same routing rules.

**Testing:** Unit test that `agent_id=hermes` builds the existing SSH behavior and that unknown agent ids fail with a clear error. Run existing tests to confirm Hermes behavior is unchanged.

### Step 2: Implement OpenClaw HTTP Adapter

**Files:** `mara_agents.py`, `mara_config.py`, `ask_mara.py`, `jarvis_listener.py`, `start_mara.ps1`, `tests/test_agent_routing.py`

**What:** Add OpenClaw settings: `active_agent`, `openclaw_base_url`, `openclaw_model`, and `openclaw_timeout_seconds`. Read the API key from `MARA_OPENCLAW_API_KEY`. The adapter sends `messages=[{"role":"user","content": prompt}]` to `/chat/completions`, parses `choices[0].message.content`, and never logs the bearer token.

**Testing:** Mock the OpenClaw HTTP response and verify the adapter returns the assistant text. Test 401/timeout/empty response error messages. Verify CLI help exposes the new options.

### Step 3: Add Live GUI Route Switching

**Files:** `mara_agent_state.py`, `mara_gui_server.py`, `gui/static/index.html`, `jarvis_listener.py`, `mara_events.py`, `tests/test_gui_server.py`

**What:** Add a tiny runtime route-state file under `config/`, for example `config/mara_agent_route.runtime.json`, ignored by git. The GUI writes `active_agent` for normal switching and `next_agent` for one-shot routing. Before each prompt, the listener reads the route state and consumes `next_agent` if present, so a GUI button can route the next prompt to OpenClaw without restarting.

**Testing:** Unit test route-state load/save/consume. GUI API test for setting default route and one-shot route. Listener unit test can verify a one-shot OpenClaw route clears after use.

### Step 4: Surface Agent State In Terminal And GUI

**Files:** `jarvis_listener.py`, `mara_pipeline.py`, `mara_gui_state.py`, `gui/static/index.html`

**What:** Emit status/events such as `Sending prompt to OpenClaw` or `Sending prompt to Hermes`, include active agent in event details, and add an `Active Agent` field to the GUI runtime panel. Add compact GUI controls: a segmented `Hermes / OpenClaw` switch and a `Next Prompt: OpenClaw` command button.

**Testing:** Browser smoke test desktop/mobile. Verify event JSON includes the selected agent and the GUI updates without layout overlap.

### Step 5: Add Diagnostics And Startup Guardrails

**Files:** `mara_agents.py`, `mara_pipeline.py`, `ask_mara.py`, `start_mara.ps1`, `README.md`

**What:** Keep existing SSH diagnostics for Hermes. Add OpenClaw diagnostics using `/v1/models` when an API key is configured. If OpenClaw is selected but unreachable, print an actionable message: enable `API_SERVER_ENABLED=true`, restart the gateway on the Dell rig, confirm port `8645`, and configure `MARA_OPENCLAW_API_KEY`.

**Testing:** Mock healthy/unhealthy diagnostics. PowerShell parser check. Manual network check only when the remote server is enabled.

### Step 6: Security And Documentation

**Files:** `.gitignore`, `README.md`, optional `config/mara_voice.example.json`

**What:** Document that secrets belong in `MARA_OPENCLAW_API_KEY` or ignored local config, never in committed files. Add example settings without a real API key. Ensure logs redact/mask any configured key if a user accidentally includes it in a URL or config.

**Testing:** Grep for `API_SERVER_KEY` and bearer token logging paths. Confirm ignored config is not staged.

## Acceptance Criteria

- Hermes remains the default and existing voice flow still works.
- GUI can switch default route between Hermes and OpenClaw.
- GUI can route only the next prompt to OpenClaw without restarting.
- Terminal and GUI show which agent is handling the prompt.
- OpenClaw HTTP calls use the OpenAI-compatible API on `192.168.0.65:8645`.
- No API key is committed or printed in logs.
- Existing tests pass, plus new routing and GUI route-control tests.

## Open Questions

- Should OpenClaw be allowed as an automatic fallback when Hermes fails, or should switching always be explicit?
- Should the GUI allow entering the API key, or should it only show whether `MARA_OPENCLAW_API_KEY` is configured?
