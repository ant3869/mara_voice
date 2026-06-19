# Mara Voice

Local voice pipeline: Voicebox WAV captures → transcription → agent routing (Hermes or OpenClaw) → VoxCPM2 TTS → audio playback, with a Mission Control GUI.

## Start

```powershell
start_mara.cmd
```

Launches the TTS server, voice listener, and the GUI dashboard in dark mode.

## Local env

Copy `.env.example` to `.env` and fill in `MARA_OPENCLAW_API_KEY` before using OpenClaw. `.env` is ignored by git and is loaded by the launcher and all Python tools.

## Requirements

- Python 3.11+
- CUDA-capable GPU (RTX series recommended — CPU inference is very slow)
- VoxCPM2 model weights under `models/VoxCPM2/` (not committed)
- Voicebox installed and running for WAV capture/transcription
- Hermes accessible over SSH, or OpenClaw API accessible over HTTP

## Notes

### Pipeline flags

- `start_mara.cmd -NoGui` — skip the dashboard.
- `start_mara.cmd -NoTtsStreaming` — fall back to WAV/chunked TTS (safer, higher latency).
- `start_mara.cmd -NoAsyncAgentReplies` — wait for each agent reply before returning control (no ack, no follow-up polling).
- `start_mara.cmd -NoAsyncFollowup` — disable background follow-up polling for deferred replies.

### TTS device

TTS defaults to `cuda`. To override: `MARA_TTS_DEVICE=cpu` in `.env` or `-TtsDevice cpu` on the launcher. If the requested CUDA device is unavailable, the server logs a loud warning and falls back to CPU.

### TTS speed

`MARA_TTS_INFERENCE_TIMESTEPS` (default 6) controls diffusion quality vs. speed. Lower is faster: try 4 for maximum speed, 8–10 for best quality. The trade-off is audio fidelity on long generations; very low values can introduce artifacts. Tune with `-TtsInferenceTimesteps N` or the GUI.

### Streaming prebuffer

True streaming TTS is on by default. The prebuffer is dynamic: it starts from a **0.5 s floor** and grows up to **4 s** when generation runs slower than real-time, keeping replies clear regardless of audio length. Configure with:

- `-TtsStreamPrebufferSeconds` — floor (default 0.5 s)
- `-TtsStreamPrebufferMaxSeconds` — ceiling (default 4 s)
- `-NoTtsStreamPrebufferDynamic` — use a fixed prebuffer instead

### Async agent replies & acknowledgement

Async replies are on by default. Mara sends the agent request in the background and plays the final answer when it arrives. If the answer takes longer than the **grace window** (`-AsyncAckGraceSeconds`, default 5 s), an acknowledgement plays first so you know the request was heard.

**Ack mode (default: preset phrases).** A random phrase from the built-in pool plays immediately — no extra agent round trip, no dead air. Manage the phrase pool in the GUI (one per line) or with `-AsyncAckPhrases`. Set `-AsyncAckText` for a single fallback phrase when no pool is configured.

**Agent-generated ack (off by default).** Enable with `-AsyncAckAgent` for a natural spoken sentence each time. Note: agent acks require their own round trip and typically arrive around the same time as the real answer, so they rarely fill dead air effectively. The preset mode is recommended.

**Grace window.** Set `-AsyncAckGraceSeconds 0` to always speak an acknowledgement even on quick prompts. With the default 5 s, fast replies play once without an ack.

**Follow-up polling.** If the agent signals that background work is still running, Mara polls the same session up to 60 times (every 8 s) until a final answer arrives. Disable with `-NoAsyncFollowup`.

### Mission Control GUI

The dashboard shows live pipeline state: agent route, TTS server health (device, CUDA status), streaming mode, timing telemetry (dictation / generation / speech speeds), and the conversation log separated from raw event output.

**Auto-launch.** If you open the GUI while the voice stack is down and `start_mara.cmd` is present, the GUI automatically runs `start_mara.cmd -NoGui` once to bring up the TTS server and listener. The server guards against duplicate launches.

### Agent routing & sessions

- Hermes is the default route (SSH subprocess).
- OpenClaw is the alternate route (OpenAI-compatible HTTP).
- Switch routes from the GUI or with `MARA_ACTIVE_AGENT=openclaw`.
- Persistent sessions are on by default for every prompt. Hermes uses session ID `voice-session-hermes`, OpenClaw uses `voice-session-openclaw`. History is stored in `config/mara_agent_sessions.json` (ignored by git). Change IDs with `MARA_HERMES_SESSION_ID` / `MARA_OPENCLAW_SESSION_ID` or the GUI fields.

### Voice profiles

OpenClaw uses a separate male TTS voice profile with reference audio at `models/openclaw_voice_reference.wav`. The reference is regenerated automatically when the profile changes. Hermes uses the default Mara voice.

### Voice inbox

Out-of-band replies can be pushed into the pipeline by appending JSONL to `config/mara_voice_inbox.jsonl`:

```json
{"text": "Done researching it.", "agent": "hermes"}
```

The listener polls the inbox every 5 seconds (`MARA_VOICE_INBOX_POLL_SECONDS`).

### Runtime state files (git-ignored)

| File | Purpose |
|---|---|
| `config/mara_voice.local.json` | GUI-managed option overrides |
| `config/mara_agent_route.runtime.json` | Live agent route state |
| `config/mara_agent_sessions.json` | Per-agent conversation history |
| `config/mara_voice_inbox.jsonl` | Out-of-band voice reply inbox |

### OpenClaw

- Default endpoint: `http://192.168.0.65:8645/v1`, model `gemini 3.1 pro`
- Set `MARA_OPENCLAW_API_KEY` in `.env` before routing to OpenClaw
- The Dell-side gateway needs `API_SERVER_ENABLED=true` and a restart before OpenClaw can answer

### Model weights

Large VoxCPM2 weights are not committed. Place them under `models/VoxCPM2/` before running.
