# Mara Voice

Local voice pipeline for Voicebox dictation, a remote Hermes agent, and local TTS playback.

## Start

```powershell
start_mara.cmd
```

That launches the voice pipeline and the GUI in dark mode.

## Notes

- `start_mara.cmd -NoGui` skips the GUI.
- `start_mara.cmd -TtsStreaming` forces raw streaming playback on.
- Local runtime state lives in `config/mara_voice.local.json` and is ignored by git.
- Large VoxCPM2 weights are not committed to this repo. Place them under `models/VoxCPM2/` before running.
