# Mara Voice

Local voice pipeline for Voicebox dictation, remote agent routing, and local TTS playback.

## Start

```powershell
start_mara.cmd
```

That launches the voice pipeline and the GUI in dark mode.

## Notes

- `start_mara.cmd -NoGui` skips the GUI.
- `start_mara.cmd -TtsStreaming` forces raw streaming playback on.
- Hermes is the default agent route.
- The GUI can switch the default route to OpenClaw or queue only the next prompt for OpenClaw.
- OpenClaw defaults to `http://192.168.0.65:8645/v1` with model `gemini 3.1 pro`.
- Set `MARA_OPENCLAW_API_KEY` locally before routing prompts to OpenClaw. Do not put the Dell API key in committed files or logs.
- The Dell-side gateway must have `API_SERVER_ENABLED=true` and be restarted before OpenClaw routing can answer.
- Local runtime state lives in `config/mara_voice.local.json` and is ignored by git.
- Live agent route state lives in `config/mara_agent_route.runtime.json` and is ignored by git.
- Large VoxCPM2 weights are not committed to this repo. Place them under `models/VoxCPM2/` before running.
