---
goal: True Streaming TTS and Mara Voice GUI
version: 1.1
date_created: 2026-06-17
last_updated: 2026-06-22
owner: local project
status: 'Implemented and evolving'
tags:
  - feature
  - streaming
  - gui
  - voice
---

# Introduction

![Status: In progress](https://img.shields.io/badge/status-In%20progress-yellow)

This plan implements true streaming TTS for Mara voice replies and a local GUI that displays the same runtime state currently shown in the command window, plus tweakable runtime options. The current default playback path remains the stable WAV/chunked path until streaming is validated with the local VoxCPM2 model.

Current note: shared modules now live under the `mara/` package, and the GUI/TTS stack has since grown typed chat plus an alternate VoiceBox backend. File references below use the current package layout.

## 1. Requirements & Constraints

- **REQ-001**: Preserve the current non-streaming `/tts` WAV endpoint in `mara/tts_server.py`.
- **REQ-002**: Add a true streaming endpoint that emits playable audio before full TTS generation completes.
- **REQ-003**: Preserve persistent reference-only voice cloning mode `persistent_reference_only_v2` for streaming and non-streaming TTS.
- **REQ-004**: Add listener playback code that can consume streamed audio incrementally and write it to `sounddevice`.
- **REQ-005**: Keep streaming opt-in until live audio validation confirms stable voice quality and no dropouts.
- **REQ-006**: GUI must show current state, recent status messages, user transcription text, Hermes reply text, TTS/playback state, timing metrics, and errors.
- **REQ-007**: GUI must expose tweakable options for TTS streaming, spoken reply character limit, TTS chunk character limit, SSH timeout, TTS timeout, status interval, TTS inference timesteps, audio device, Hermes command, and voice reference regeneration.
- **CON-001**: Do not require scraping ANSI-colored terminal text for GUI state.
- **CON-002**: Do not add heavyweight desktop dependencies unless the web GUI approach proves insufficient.
- **CON-003**: Do not make destructive changes to the launcher or background processes.
- **PAT-001**: Use existing Python/FastAPI/requests/sounddevice stack before adding new frameworks.
- **PAT-002**: Keep the command window path functional for diagnosis even after the GUI exists.

## 2. Implementation Steps

### Implementation Phase 1

- GOAL-001: Establish structured runtime state and true-streaming protocol groundwork.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Add `mara/events.py` with `append_event()` and `read_recent_events()` writing JSONL events to `logs/mara_events.jsonl`. | ✅ | 2026-06-17 |
| TASK-002 | Update `mara_pipeline.emit_status()` to write `status` events while preserving existing terminal logging. | ✅ | 2026-06-17 |
| TASK-003 | Update `jarvis_listener.py` to write `conversation` events for user transcriptions and Hermes replies. | ✅ | 2026-06-17 |
| TASK-004 | Add `mara/gui_state.py` to build a GUI-ready runtime snapshot from `logs/mara_events.jsonl`. | ✅ | 2026-06-17 |
| TASK-005 | Add `mara/streaming.py` with `STREAM_MEDIA_TYPE=audio/x-mara-pcm-f32le`, stream headers, encoder, and float32 PCM decoder. | ✅ | 2026-06-17 |
| TASK-006 | Add `mara/config.py` with a schema-backed safe local options file at `config/mara_voice.local.json`. | ✅ | 2026-06-17 |

### Implementation Phase 2

- GOAL-002: Add opt-in true streaming endpoint and listener playback.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-007 | Add `POST /tts/stream` in `mara/tts_server.py` using `generate_with_prompt_cache_streaming()` when a persistent reference cache is available. | ✅ | 2026-06-17 |
| TASK-008 | Add `streaming_supported` and `stream_media_type` to `GET /healthz` in `mara/tts_server.py`. | ✅ | 2026-06-17 |
| TASK-009 | Add `--tts-streaming`, `--no-tts-streaming`, and `--tts-stream-url` to `jarvis_listener.py`. | ✅ | 2026-06-17 |
| TASK-010 | Add streaming playback in `jarvis_listener.py` using `requests.post(..., stream=True)` and `sounddevice.OutputStream`. | ✅ | 2026-06-17 |
| TASK-011 | Add `-TtsStreaming` and `-TtsStreamUrl` to `start_mara.ps1` and pass the corresponding listener flags. | ✅ | 2026-06-17 |
| TASK-012 | Add fallback from streaming playback to the existing WAV/chunked playback path when the stream cannot be opened or played. | ✅ | 2026-06-17 |
| TASK-013 | Update `start_mara.ps1` health reuse checks so `-TtsStreaming` rejects older TTS servers that do not report `streaming_supported=true`. | ✅ | 2026-06-17 |
| TASK-013A | Add `-NoTtsStreaming` and always pass either `--tts-streaming` or `--no-tts-streaming` to make pure streaming explicitly on/off per launch. | ✅ | 2026-06-17 |

### Implementation Phase 3

- GOAL-003: Validate and harden true streaming before considering it as a default.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-014 | Add a fake streaming unit test for `Float32PcmStreamDecoder.decode()` that verifies arbitrary byte boundaries do not corrupt frames. | ✅ | 2026-06-17 |
| TASK-015 | Add a fake server/client smoke test for `POST /tts/stream` using a stub generator that emits two float32 PCM chunks. | ✅ | 2026-06-17 |
| TASK-016 | Run live local test: `start_mara.cmd -TtsStreaming -SpokenReplyCharLimit 900 -TtsChunkCharLimit 0` and verify first audio starts before full generation completes. |  |  |
| TASK-017 | Compare streamed voice quality against `/tts` WAV output using the same text, same persistent voice reference, and same inference timesteps. |  |  |
| TASK-018 | If streaming has repeated dropouts, add a jitter buffer in `jarvis_listener.py` before `OutputStream.write()`. |  |  |
| TASK-019 | After live validation, decide whether `MARA_TTS_STREAMING=1` should become the default in `start_mara.ps1`. |  |  |

### Implementation Phase 4

- GOAL-004: Implement a local web GUI backed by structured events and config.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-020 | Add `mara/gui_server.py` using FastAPI to serve a local dashboard on `127.0.0.1:8765`. | ✅ | 2026-06-17 |
| TASK-021 | Add `GET /api/state` in `mara/gui_server.py` that returns `mara/gui_state.build_state()` output. | ✅ | 2026-06-17 |
| TASK-022 | Add `GET /api/events` in `mara/gui_server.py` that returns recent JSONL events with a `limit` query parameter. | ✅ | 2026-06-17 |
| TASK-023 | Add `GET /api/options` returning current tweakable defaults from environment/config and available audio output devices. | ✅ | 2026-06-17 |
| TASK-024 | Add `POST /api/options` writing safe local settings to `config/mara_voice.local.json`; do not write secrets or remote credentials. | ✅ | 2026-06-17 |
| TASK-025 | Add a static dashboard page under `gui/static/index.html` that displays status, conversation, TTS health, timing, logs, and options. | ✅ | 2026-06-17 |
| TASK-026 | Add GUI launcher support in `start_mara.ps1` with `-Gui` and `-GuiPort 8765`, starting the GUI server hidden and printing the URL. | ✅ | 2026-06-17 |
| TASK-027 | Keep the command window status messages active when GUI mode is enabled. | ✅ | 2026-06-17 |

### Implementation Phase 5

- GOAL-005: Add GUI controls that safely influence the next listener run.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-028 | Add controls for `TtsStreaming`, `SpokenReplyCharLimit`, `TtsChunkCharLimit`, `TtsInferenceTimesteps`, `TtsTimeoutSeconds`, and `StatusIntervalSeconds`. | ✅ | 2026-06-17 |
| TASK-029 | Add audio device selector populated from `mara_pipeline.list_output_devices()`. | ✅ | 2026-06-17 |
| TASK-030 | Add read-only display for `ssh_target`, `hermes_command`, `voice_generation_mode`, `voice_reference_ready`, and TTS health. | ✅ | 2026-06-17 |
| TASK-031 | Add a guarded `Regenerate voice reference on next start` toggle that writes config but does not delete files directly. | ✅ | 2026-06-17 |
| TASK-032 | Add GUI indication when settings require listener/TTS restart before taking effect. | ✅ | 2026-06-17 |
| TASK-033 | Update `start_mara.ps1` to read `config/mara_voice.local.json` so GUI-saved options apply on the next launcher run unless command-line parameters override them. | ✅ | 2026-06-17 |

## 3. Alternatives

- **ALT-001**: Stream WAV bytes from `/tts/stream`. Rejected because a WAV header requires knowing the final data length or using nonstandard placeholder lengths; raw PCM is simpler for real-time playback.
- **ALT-002**: Use WebSocket audio streaming immediately. Rejected for initial implementation because HTTP chunked streaming is enough for one-way TTS audio and fits the current `requests` client.
- **ALT-003**: Build a native Tkinter GUI first. Rejected for initial implementation because the existing stack already includes FastAPI and a local web GUI can reuse HTTP APIs and JSONL events.
- **ALT-004**: Replace the command window with the GUI. Rejected because terminal logs remain useful during TTS/GPU failures and startup diagnostics.

## 4. Dependencies

- **DEP-001**: `fastapi` and `uvicorn` already exist in `requirements.txt` and support the TTS server and planned GUI server.
- **DEP-002**: `requests` already exists in `requirements.txt` and supports streaming HTTP client playback.
- **DEP-003**: `sounddevice` already exists in `requirements.txt` and supports `OutputStream` playback.
- **DEP-004**: `numpy` is required by the existing TTS server and `mara/streaming.py`.
- **DEP-005**: VoxCPM2 local package must continue exposing `generate_with_prompt_cache_streaming()`.

## 5. Files

- **FILE-001**: `mara/tts_server.py` contains `/tts`, `/tts/stream`, `/healthz`, and voice-reference generation logic.
- **FILE-002**: `jarvis_listener.py` contains Voicebox capture processing, Hermes SSH calls, TTS playback, chunked playback, and opt-in streaming playback.
- **FILE-003**: `mara/pipeline.py` contains shared logging, status emission, diagnostics, and device enumeration.
- **FILE-004**: `mara/events.py` contains structured JSONL event writing and reading.
- **FILE-005**: `mara/streaming.py` contains the true-streaming PCM wire protocol helpers.
- **FILE-006**: `mara/gui_state.py` contains runtime state aggregation for the GUI.
- **FILE-007**: `mara/config.py` contains safe GUI-editable option schema and JSON load/save helpers.
- **FILE-008**: `start_mara.ps1` and `start_mara.cmd` contain launcher behavior and user-facing switches.
- **FILE-009**: `plan/feature-streaming-gui-1.md` contains this implementation plan.
- **FILE-010**: `config/mara_voice.local.json` will store safe GUI-editable options.
- **FILE-011**: `mara/gui_server.py` and `gui/static/index.html` implement the local web GUI.

## 6. Testing

- **TEST-001**: Run `venv\Scripts\python.exe -m py_compile ask_mara.py jarvis_listener.py mara/pipeline.py mara/tts_server.py mara/events.py mara/streaming.py mara/gui_state.py mara/config.py`.
- **TEST-002**: Run `venv\Scripts\python.exe jarvis_listener.py --help` and confirm streaming flags are displayed.
- **TEST-003**: Run `venv\Scripts\python.exe -m mara.tts_server --help` and confirm the server still starts with existing options.
- **TEST-004**: Run PowerShell parser check for `start_mara.ps1`.
- **TEST-005**: Run `venv\Scripts\python.exe -m mara.gui_state --limit 5` and confirm valid JSON output.
- **TEST-006**: Run a fake decoder test that splits a float32 PCM payload at non-frame byte boundaries and validates decoded samples.
- **TEST-007**: Run live `start_mara.cmd -TtsStreaming` and verify streamed playback works or falls back cleanly.
- **TEST-008**: Run GUI smoke test after `mara/gui_server.py` exists: open `http://127.0.0.1:8765`, verify state refresh and option rendering.

## 7. Risks & Assumptions

- **RISK-001**: VoxCPM streaming can still delay the first chunk for model-inference reasons; streaming improves playback overlap but does not make inference instantaneous.
- **RISK-002**: Streaming mode disables VoxCPM badcase retry, so long or unusual replies may need chunking or fallback if quality regresses.
- **RISK-003**: Audio dropouts may occur if chunks arrive slower than playback; a jitter buffer may be required.
- **RISK-004**: Writing full user/Hermes text into `logs/mara_events.jsonl` increases local log visibility; this matches existing text logging but must remain local-only.
- **ASSUMPTION-001**: A local web GUI is acceptable as the first GUI because it avoids new dependencies and can run from the same Python environment.
- **ASSUMPTION-002**: The GUI will configure future launches first; live mutation of a running listener is a later phase.

## 8. Related Specifications / Further Reading

- `mara/streaming.py`
- `mara/events.py`
- `mara/gui_state.py`
- `mara/tts_server.py`
- `jarvis_listener.py`
- `start_mara.ps1`
