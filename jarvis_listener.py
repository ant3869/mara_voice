from __future__ import annotations

import argparse
import io
import os
import time
from queue import Empty, Queue
from pathlib import Path
from threading import Thread

import requests
import sounddevice as sd
import soundfile as sf

from mara_agent_state import DEFAULT_AGENT_ROUTE_STATE_PATH, consume_agent_route, update_agent_route_state
from mara_agents import (
    DEFAULT_ACTIVE_AGENT,
    DEFAULT_OPENCLAW_BASE_URL,
    DEFAULT_OPENCLAW_MODEL,
    DEFAULT_OPENCLAW_TIMEOUT_SECONDS,
    AgentError,
    AgentReply,
    AgentSettings,
    ask_agent,
    redact_url,
)
from mara_events import append_event
from mara_pipeline import (
    DEFAULT_CAPTURE_DIR,
    DEFAULT_HERMES_COMMAND,
    DEFAULT_SSH_TARGET,
    configure_logging,
    emit_status,
    format_output_devices,
)
from mara_streaming import (
    Float32PcmStreamDecoder,
    STREAM_CHANNELS_HEADER,
    STREAM_DTYPE,
    STREAM_DTYPE_HEADER,
    STREAM_MEDIA_TYPE,
    STREAM_SAMPLE_RATE_HEADER,
)

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:
    FileSystemEventHandler = object
    Observer = None
    WATCHDOG_AVAILABLE = False

DEFAULT_VOICEBOX_API = "http://127.0.0.1:17493/transcribe"
DEFAULT_TTS_URL = "http://127.0.0.1:8000/tts"
DEFAULT_TRANSCRIBE_TIMEOUT_SECONDS = float(os.getenv("MARA_TRANSCRIBE_TIMEOUT", "60"))
DEFAULT_TTS_RETRY_ATTEMPTS = int(os.getenv("MARA_TTS_RETRY_ATTEMPTS", "1"))
DEFAULT_STATUS_INTERVAL_SECONDS = float(os.getenv("MARA_STATUS_INTERVAL", "10"))
DEFAULT_SSH_CONNECT_TIMEOUT_SECONDS = float(os.getenv("MARA_SSH_CONNECT_TIMEOUT", "5"))
DEFAULT_SPOKEN_REPLY_CHAR_LIMIT = int(os.getenv("MARA_SPOKEN_REPLY_CHAR_LIMIT", "900"))
DEFAULT_TTS_CHUNK_CHAR_LIMIT = int(os.getenv("MARA_TTS_CHUNK_CHAR_LIMIT", "450"))
DEFAULT_TTS_STREAMING = os.getenv("MARA_TTS_STREAMING", "0") == "1"
DEFAULT_TTS_STREAM_URL = os.getenv("MARA_TTS_STREAM_URL", "http://127.0.0.1:8000/tts/stream")
DEFAULT_TTS_STREAM_CHUNK_CHAR_LIMIT = int(os.getenv("MARA_TTS_STREAM_CHUNK_CHAR_LIMIT", "180"))


def parse_optional_timeout(value: str | None, default: float | None) -> float | None:
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in ("", "0", "none", "off", "false", "disabled", "never"):
        return None

    timeout_seconds = float(normalized)
    if timeout_seconds <= 0:
        return None
    return timeout_seconds


def format_timeout(timeout_seconds: float | None) -> str:
    if timeout_seconds is None:
        return "disabled"
    return f"{timeout_seconds:.1f}s"


def find_natural_text_cutoff(text: str, max_chars: int) -> int:
    minimum_useful_cutoff = min(80, max(12, int(max_chars * 0.25)))
    sentence_cutoff = max(
        text.rfind(".", 0, max_chars),
        text.rfind("?", 0, max_chars),
        text.rfind("!", 0, max_chars),
    )
    if sentence_cutoff >= minimum_useful_cutoff:
        return sentence_cutoff + 1

    word_cutoff = text.rfind(" ", 0, max_chars)
    if word_cutoff >= minimum_useful_cutoff:
        return word_cutoff

    return max_chars


def prepare_spoken_reply(text: str, max_chars: int) -> str:
    normalized = " ".join(text.split())
    if max_chars <= 0 or len(normalized) <= max_chars:
        return normalized

    cutoff = find_natural_text_cutoff(normalized, max_chars)
    shortened = normalized[:cutoff].rstrip(" ,;:-")
    if shortened and shortened[-1] not in ".?!":
        shortened += "."
    return f"{shortened} Full reply is in the terminal."


def split_text_for_tts(text: str, max_chars: int) -> list[str]:
    normalized = " ".join(text.split())
    if not normalized:
        return []
    if max_chars <= 0 or len(normalized) <= max_chars:
        return [normalized]

    chunks: list[str] = []
    remaining = normalized
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break

        cutoff = find_natural_text_cutoff(remaining, max_chars)
        chunk = remaining[:cutoff].strip()
        if not chunk:
            chunk = remaining[:max_chars].strip()
            cutoff = max_chars
        chunks.append(chunk)
        remaining = remaining[cutoff:].strip()

    return chunks


DEFAULT_SSH_TIMEOUT_SECONDS = parse_optional_timeout(os.getenv("MARA_SSH_TIMEOUT"), None)
DEFAULT_TTS_TIMEOUT_SECONDS = parse_optional_timeout(os.getenv("MARA_TTS_TIMEOUT"), 300.0)


class WavCaptureProcessor:
    def __init__(
        self,
        capture_dir: Path,
        ssh_target: str,
        hermes_command: str,
        active_agent: str,
        openclaw_base_url: str,
        openclaw_model: str,
        tts_url: str,
        tts_stream_url: str,
        audio_device: str | None,
        logger,
        agent_route_state_path: Path = DEFAULT_AGENT_ROUTE_STATE_PATH,
        dedupe_window_seconds: float = 2.0,
        transcribe_timeout_seconds: float = DEFAULT_TRANSCRIBE_TIMEOUT_SECONDS,
        ssh_timeout_seconds: float | None = DEFAULT_SSH_TIMEOUT_SECONDS,
        openclaw_timeout_seconds: float | None = DEFAULT_OPENCLAW_TIMEOUT_SECONDS,
        tts_timeout_seconds: float | None = DEFAULT_TTS_TIMEOUT_SECONDS,
        tts_retry_attempts: int = DEFAULT_TTS_RETRY_ATTEMPTS,
        status_interval_seconds: float = DEFAULT_STATUS_INTERVAL_SECONDS,
        ssh_connect_timeout_seconds: float = DEFAULT_SSH_CONNECT_TIMEOUT_SECONDS,
        spoken_reply_char_limit: int = DEFAULT_SPOKEN_REPLY_CHAR_LIMIT,
        tts_chunk_char_limit: int = DEFAULT_TTS_CHUNK_CHAR_LIMIT,
        tts_streaming: bool = DEFAULT_TTS_STREAMING,
        tts_stream_chunk_char_limit: int = DEFAULT_TTS_STREAM_CHUNK_CHAR_LIMIT,
    ) -> None:
        self.capture_dir = capture_dir
        self.ssh_target = ssh_target
        self.hermes_command = hermes_command
        self.active_agent = active_agent
        self.openclaw_base_url = openclaw_base_url
        self.openclaw_model = openclaw_model
        self.tts_url = tts_url
        self.tts_stream_url = tts_stream_url
        self.audio_device = audio_device
        self.logger = logger
        self.agent_route_state_path = agent_route_state_path
        self.dedupe_window_seconds = dedupe_window_seconds
        self.transcribe_timeout_seconds = transcribe_timeout_seconds
        self.ssh_timeout_seconds = ssh_timeout_seconds
        self.openclaw_timeout_seconds = openclaw_timeout_seconds
        self.tts_timeout_seconds = tts_timeout_seconds
        self.tts_retry_attempts = max(0, tts_retry_attempts)
        self.status_interval_seconds = max(0.0, status_interval_seconds)
        self.ssh_connect_timeout_seconds = max(1.0, ssh_connect_timeout_seconds)
        self.spoken_reply_char_limit = max(0, spoken_reply_char_limit)
        self.tts_chunk_char_limit = max(0, tts_chunk_char_limit)
        self.tts_streaming = tts_streaming
        self.tts_stream_chunk_char_limit = max(40, tts_stream_chunk_char_limit)
        self.voicebox_session = requests.Session()
        self.tts_session = requests.Session()
        self.agent_session = requests.Session()
        self.recent_files: dict[Path, float] = {}
        self.processed_files: dict[Path, float] = {}
        self.processed_file_retention_seconds = 24 * 60 * 60

    def process_path(self, file_path: Path) -> None:
        if file_path.suffix.lower() != ".wav":
            return

        if not file_path.exists():
            self.logger.debug("Audio file disappeared before processing: %s", file_path)
            return

        now = time.monotonic()
        # Normalize the path to ensure consistent deduplication
        file_path_normalized = file_path.resolve()

        if file_path_normalized in self.processed_files:
            self.logger.debug("Skipping already processed audio file: %s", file_path.name)
            return

        last_seen = self.recent_files.get(file_path_normalized)
        
        if last_seen is not None and now - last_seen < self.dedupe_window_seconds:
            self.logger.debug("Skipping duplicate audio event for %s (seen %.1f seconds ago)", file_path.name, now - last_seen)
            return

        self.logger.info("New .wav detected: %s", file_path.name)
        self.recent_files[file_path_normalized] = now
        self.processed_files[file_path_normalized] = now
        self._purge_old_files(now)
        self._process_wav(file_path)

    def _purge_old_files(self, now: float) -> None:
        stale_keys = [
            key
            for key, timestamp in self.recent_files.items()
            if now - timestamp > self.dedupe_window_seconds
        ]
        for key in stale_keys:
            self.recent_files.pop(key, None)

        stale_processed_keys = [
            key
            for key, timestamp in self.processed_files.items()
            if now - timestamp > self.processed_file_retention_seconds
        ]
        for key in stale_processed_keys:
            self.processed_files.pop(key, None)

    def _wait_for_stable_file(
        self,
        file_path: Path,
        settle_seconds: float = 0.25,
        max_wait_seconds: float = 3.0,
    ) -> bool:
        start_time = time.monotonic()
        last_size = -1

        while time.monotonic() - start_time < max_wait_seconds:
            try:
                current_size = file_path.stat().st_size
            except FileNotFoundError:
                return False

            if current_size > 0 and current_size == last_size:
                return True

            last_size = current_size
            time.sleep(settle_seconds)

        self.logger.warning("Audio file did not settle before processing: %s", file_path.name)
        return file_path.exists()

    def _run_with_heartbeat(self, operation, status: str, message: str):
        results: Queue[tuple[str, object]] = Queue(maxsize=1)

        def worker() -> None:
            try:
                results.put(("ok", operation()))
            except Exception as exc:
                results.put(("error", exc))

        thread = Thread(target=worker, daemon=True)
        start_time = time.monotonic()
        next_status_time = self.status_interval_seconds
        thread.start()

        while True:
            try:
                kind, value = results.get(timeout=0.25)
            except Empty:
                elapsed = time.monotonic() - start_time
                if self.status_interval_seconds and elapsed >= next_status_time:
                    emit_status(self.logger, status, f"{message} ({elapsed:.0f}s elapsed)")
                    next_status_time += self.status_interval_seconds
                continue

            if kind == "error":
                raise value
            return value

    def _append_event(
        self,
        event_type: str,
        status: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        try:
            append_event(event_type, status, message, details=details)
        except Exception as exc:
            self.logger.debug("Could not write event: %s", exc)

    def _process_wav(self, wav_path: Path) -> None:
        pipeline_start = time.monotonic()
        self.logger.info("Processing audio capture: %s", wav_path.name)
        emit_status(self.logger, "THINKING", f"Processing {wav_path.name}")
        if not self._wait_for_stable_file(wav_path):
            self.logger.warning("Audio capture disappeared before it was stable: %s", wav_path.name)
            try:
                file_path_normalized = wav_path.resolve()
                self.processed_files.pop(file_path_normalized, None)
                self.recent_files.pop(file_path_normalized, None)
            except OSError:
                pass
            emit_status(self.logger, "LISTENING", "Waiting for next capture")
            return

        transcribe_start = time.monotonic()
        emit_status(self.logger, "LISTENING", "Transcribing speech")
        transcribed_text = self._transcribe_wav(wav_path)
        transcribe_time = time.monotonic() - transcribe_start
        if not transcribed_text:
            emit_status(self.logger, "LISTENING", "Waiting for next capture")
            return

        print(f"[You]: {transcribed_text}")
        self._append_event(
            "conversation",
            "USER",
            "User transcription",
            {"text": transcribed_text, "transcription_seconds": round(transcribe_time, 3)},
        )
        self.logger.info("User said: %s (transcription: %.2fs)", transcribed_text, transcribe_time)

        ssh_start = time.monotonic()
        emit_status(self.logger, "THINKING", "Sending text to Mara")
        agent_reply = self._send_to_agent(transcribed_text)
        ssh_time = time.monotonic() - ssh_start
        if agent_reply is None:
            emit_status(self.logger, "LISTENING", "Waiting for next capture")
            return
        mara_reply = agent_reply.text

        print(f"[Mara]: {mara_reply}")
        self._append_event(
            "conversation",
            "MARA",
            f"{agent_reply.agent_name} reply",
            {
                "text": mara_reply,
                "agent": agent_reply.agent_id,
                "agent_name": agent_reply.agent_name,
                "agent_seconds": round(agent_reply.elapsed_seconds, 3),
                "pipeline_agent_seconds": round(ssh_time, 3),
            },
        )
        self.logger.info(
            "%s replied: %s (agent: %.2fs)",
            agent_reply.agent_name,
            mara_reply,
            agent_reply.elapsed_seconds,
        )
        spoken_reply = prepare_spoken_reply(mara_reply, self.spoken_reply_char_limit)
        if spoken_reply != " ".join(mara_reply.split()):
            self.logger.info(
                "Shortened spoken reply from %d to %d characters for faster TTS",
                len(" ".join(mara_reply.split())),
                len(spoken_reply),
            )

        tts_start = time.monotonic()
        emit_status(self.logger, "TALKING", "Generating and playing reply")
        self._play_tts_reply(spoken_reply, voice_id=agent_reply.agent_id)
        tts_time = time.monotonic() - tts_start
        total_time = time.monotonic() - pipeline_start
        self.logger.info("Pipeline complete (TTS: %.2fs, total: %.2fs)", tts_time, total_time)
        emit_status(self.logger, "LISTENING", "Waiting for next capture")

    def _transcribe_wav(self, wav_path: Path) -> str:
        self.logger.info("Sending audio to Voicebox transcription at %s", DEFAULT_VOICEBOX_API)
        try:
            with open(wav_path, "rb") as wav_file:
                files = {
                    "file": (wav_path.name, wav_file, "audio/wav"),
                }
                data = {"language": "en"}
                response = self.voicebox_session.post(
                    DEFAULT_VOICEBOX_API,
                    files=files,
                    data=data,
                    timeout=self.transcribe_timeout_seconds,
                )
                response.raise_for_status()
        except Exception as exc:
            self.logger.error("Voicebox transcription failed: %s", exc)
            return ""

        try:
            result = response.json()
            text = result.get("text", "").strip()
        except Exception as exc:
            self.logger.error("Failed to parse Voicebox response: %s", exc)
            return ""

        if not text:
            self.logger.info("No speech detected in audio.")
            return ""

        return text

    def _send_to_agent(self, text: str) -> AgentReply | None:
        try:
            route = consume_agent_route(
                self.agent_route_state_path,
                fallback_active_agent=self.active_agent,
            )
        except Exception as exc:
            self.logger.error("Could not read agent route state: %s", exc)
            return None

        settings = AgentSettings(
            active_agent=route.agent_id,
            ssh_target=self.ssh_target,
            hermes_command=self.hermes_command,
            ssh_timeout_seconds=self.ssh_timeout_seconds,
            ssh_connect_timeout_seconds=self.ssh_connect_timeout_seconds,
            openclaw_base_url=self.openclaw_base_url,
            openclaw_model=self.openclaw_model,
            openclaw_timeout_seconds=self.openclaw_timeout_seconds,
        )
        if route.one_shot:
            self.logger.info("Using one-shot agent route: %s", route.agent_id)

        try:
            return ask_agent(
                text,
                settings,
                self.logger,
                status_callback=lambda status, message, details=None: emit_status(
                    self.logger, status, message, details
                ),
                status_interval_seconds=self.status_interval_seconds,
                requests_session=self.agent_session,
            )
        except AgentError as exc:
            self.logger.error("Agent request failed: %s", exc)
            return None

    def _request_tts_audio(self, text: str, timeout_seconds: float | None, voice_id: str) -> bytes | None:
        response = self.tts_session.post(
            self.tts_url,
            json={"text": text, "voice_id": voice_id},
            timeout=timeout_seconds,
        )
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()
        if content_type != "audio/wav":
            raise RuntimeError(f"TTS did not return audio/wav content (content-type={content_type or '<empty>'})")

        return response.content

    def _request_tts_audio_with_retries(
        self,
        text: str,
        chunk_index: int = 1,
        chunk_count: int = 1,
        voice_id: str = "hermes",
    ) -> bytes | None:
        attempt_count = self.tts_retry_attempts + 1
        descriptor = "voice reply" if chunk_count == 1 else f"voice chunk {chunk_index}/{chunk_count}"

        for attempt in range(1, attempt_count + 1):
            timeout_seconds = self.tts_timeout_seconds
            if attempt > 1 and timeout_seconds is not None:
                timeout_seconds *= 2
            self.logger.info(
                "Sending %s to TTS server (attempt %d/%d, timeout=%s)",
                descriptor,
                attempt,
                attempt_count,
                format_timeout(timeout_seconds),
            )
            try:
                emit_status(self.logger, "WRITING", f"Rendering {descriptor}")
                return self._run_with_heartbeat(
                    lambda: self._request_tts_audio(text, timeout_seconds, voice_id),
                    "WRITING",
                    f"Still rendering {descriptor}",
                )
            except requests.Timeout as exc:
                if attempt < attempt_count:
                    self.logger.warning(
                        "TTS request for %s timed out on attempt %d/%d: %s",
                        descriptor,
                        attempt,
                        attempt_count,
                        exc,
                    )
                    continue
                self.logger.error("TTS request for %s timed out after %d attempts: %s", descriptor, attempt_count, exc)
                return None
            except requests.RequestException as exc:
                if attempt < attempt_count:
                    self.logger.warning(
                        "TTS request for %s failed on attempt %d/%d: %s",
                        descriptor,
                        attempt,
                        attempt_count,
                        exc,
                    )
                    continue
                self.logger.error("TTS request for %s failed after %d attempts: %s", descriptor, attempt_count, exc)
                return None
            except Exception as exc:
                self.logger.error("TTS request for %s failed: %s", descriptor, exc)
                return None

        return None

    def _decode_tts_audio(self, audio_bytes: bytes) -> tuple[object, int] | None:
        if audio_bytes is None:
            self.logger.error("TTS did not return audio bytes")
            return None

        try:
            wav_buffer = io.BytesIO(audio_bytes)
            audio_data, sample_rate = sf.read(wav_buffer, dtype="float32")
        except Exception as exc:
            self.logger.error("Failed to decode TTS audio: %s", exc)
            return None

        return audio_data, sample_rate

    def _play_audio_data(
        self,
        audio_data: object,
        sample_rate: int,
        chunk_index: int = 1,
        chunk_count: int = 1,
    ) -> bool:
        descriptor = "response" if chunk_count == 1 else f"response chunk {chunk_index}/{chunk_count}"
        self.logger.info("Playing audio %s (%d samples at %d Hz)", descriptor, len(audio_data), sample_rate)
        try:
            if chunk_count == 1:
                print("[Playing Mara's response...]")
            else:
                print(f"[Playing Mara's response chunk {chunk_index}/{chunk_count}...]")
            sd.play(audio_data, samplerate=sample_rate, device=self.audio_device)
            sd.wait()
            self.logger.info("Audio %s playback complete", descriptor)
            return True
        except Exception as exc:
            self.logger.error("Audio playback failed: %s", exc)
            return False

    def _prefetch_tts_audio(self, text: str, chunk_index: int, chunk_count: int, voice_id: str):
        result_queue: Queue[bytes | None] = Queue(maxsize=1)

        def worker() -> None:
            result_queue.put(self._request_tts_audio_with_retries(text, chunk_index, chunk_count, voice_id))

        Thread(target=worker, daemon=True).start()
        return result_queue

    def _open_tts_stream(self, text: str, timeout_seconds: float | None, voice_id: str):
        response = self.tts_session.post(
            self.tts_stream_url,
            json={"text": text, "voice_id": voice_id},
            timeout=timeout_seconds,
            stream=True,
        )
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").split(";")[0].lower()
        if content_type != STREAM_MEDIA_TYPE:
            response.close()
            raise RuntimeError(
                f"TTS stream did not return {STREAM_MEDIA_TYPE} content "
                f"(content-type={content_type or '<empty>'})"
            )

        dtype = response.headers.get(STREAM_DTYPE_HEADER, "")
        if dtype != STREAM_DTYPE:
            response.close()
            raise RuntimeError(f"TTS stream returned unsupported dtype={dtype or '<empty>'}")

        sample_rate = int(response.headers.get(STREAM_SAMPLE_RATE_HEADER, "0"))
        channels = int(response.headers.get(STREAM_CHANNELS_HEADER, "1"))
        if sample_rate <= 0 or channels <= 0:
            response.close()
            raise RuntimeError(
                f"TTS stream returned invalid audio headers: sample_rate={sample_rate}, channels={channels}"
            )

        return response, sample_rate, channels

    def _play_tts_reply_streaming(
        self,
        text: str,
        chunk_index: int = 1,
        segment_count: int = 1,
        voice_id: str = "hermes",
    ) -> bool:
        attempt_count = self.tts_retry_attempts + 1
        response = None
        sample_rate = 0
        channels = 1
        descriptor = "streaming voice reply" if segment_count == 1 else f"streaming voice chunk {chunk_index}/{segment_count}"

        for attempt in range(1, attempt_count + 1):
            timeout_seconds = self.tts_timeout_seconds
            if attempt > 1 and timeout_seconds is not None:
                timeout_seconds *= 2
            self.logger.info(
                "Opening %s (attempt %d/%d, timeout=%s, characters=%d)",
                descriptor,
                attempt,
                attempt_count,
                format_timeout(timeout_seconds),
                len(text),
                )
            try:
                emit_status(self.logger, "WRITING", f"Opening {descriptor}")
                response, sample_rate, channels = self._open_tts_stream(text, timeout_seconds, voice_id)
                break
            except requests.Timeout as exc:
                if attempt < attempt_count:
                    self.logger.warning("Streaming TTS open timed out for %s on attempt %d/%d: %s", descriptor, attempt, attempt_count, exc)
                    continue
                self.logger.error("Streaming TTS open timed out for %s after %d attempts: %s", descriptor, attempt_count, exc)
                return False
            except Exception as exc:
                if attempt < attempt_count:
                    self.logger.warning("Streaming TTS open failed for %s on attempt %d/%d: %s", descriptor, attempt, attempt_count, exc)
                    continue
                self.logger.error("Streaming TTS open failed for %s after %d attempts: %s", descriptor, attempt_count, exc)
                return False

        if response is None:
            return False

        decoder = Float32PcmStreamDecoder(channels=channels)
        stream = None
        audio_chunk_count = 0
        sample_count = 0
        start_time = time.monotonic()
        next_status_time = self.status_interval_seconds

        try:
            emit_status(self.logger, "TALKING", f"Streaming {descriptor}")
            with response:
                for payload in response.iter_content(chunk_size=None):
                    audio_chunk = decoder.decode(payload)
                    if audio_chunk is None:
                        continue

                    if stream is None:
                        if segment_count == 1:
                            print("[Streaming Mara's response...]")
                        else:
                            print(f"[Streaming Mara's response chunk {chunk_index}/{segment_count}...]")
                        stream = sd.OutputStream(
                            samplerate=sample_rate,
                            channels=channels,
                            dtype="float32",
                            device=self.audio_device,
                        )
                        stream.start()

                    stream.write(audio_chunk)
                    audio_chunk_count += 1
                    sample_count += len(audio_chunk)

                    elapsed = time.monotonic() - start_time
                    if self.status_interval_seconds and elapsed >= next_status_time:
                        emit_status(
                            self.logger,
                            "TALKING",
                            f"Streaming {descriptor} ({audio_chunk_count} audio chunks, {elapsed:.0f}s elapsed)",
                        )
                        next_status_time += self.status_interval_seconds

            if stream is None:
                self.logger.error("Streaming TTS returned no playable audio chunks for %s", descriptor)
                return False
            if decoder.has_pending_bytes():
                self.logger.warning("Streaming TTS ended with a partial PCM frame; trailing bytes were ignored")

            duration_seconds = sample_count / float(sample_rate)
            self.logger.info(
                "Streaming audio playback complete (%d chunks, %.2fs at %d Hz)",
                audio_chunk_count,
                duration_seconds,
                sample_rate,
            )
            return True
        except Exception as exc:
            self.logger.error("Streaming audio playback failed: %s", exc)
            return False
        finally:
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception as exc:
                    self.logger.debug("Could not close streaming audio output cleanly: %s", exc)

    def _play_tts_reply_streaming_segments(self, text: str, voice_id: str) -> bool:
        stream_chunks = split_text_for_tts(text, self.tts_stream_chunk_char_limit)
        if not stream_chunks:
            self.logger.warning("No text available for streaming TTS playback")
            return False

        stream_chunk_count = len(stream_chunks)
        if stream_chunk_count > 1:
            self.logger.info(
                "Split spoken reply into %d streaming TTS chunks (stream_chunk_limit=%d characters)",
                stream_chunk_count,
                self.tts_stream_chunk_char_limit,
            )
            emit_status(self.logger, "TALKING", f"Streaming reply in {stream_chunk_count} chunks")

        for index, chunk in enumerate(stream_chunks, start=1):
            if not self._play_tts_reply_streaming(chunk, index, stream_chunk_count, voice_id):
                return False

        return True

    def _play_tts_reply(self, text: str, voice_id: str = "hermes") -> None:
        if self.tts_streaming:
            if self._play_tts_reply_streaming_segments(text, voice_id):
                return
            self.logger.warning("Falling back to WAV/chunked TTS playback after streaming failure")

        chunks = split_text_for_tts(text, self.tts_chunk_char_limit)
        if not chunks:
            self.logger.warning("No text available for TTS playback")
            return

        chunk_count = len(chunks)
        if chunk_count > 1:
            self.logger.info(
                "Split spoken reply into %d TTS chunks (chunk_limit=%d characters)",
                chunk_count,
                self.tts_chunk_char_limit,
            )
            emit_status(self.logger, "TALKING", f"Playing reply in {chunk_count} chunks")

        audio_queue = self._prefetch_tts_audio(chunks[0], 1, chunk_count, voice_id)
        for index in range(1, chunk_count + 1):
            audio_bytes = audio_queue.get()

            next_audio_queue = None
            if index < chunk_count:
                next_audio_queue = self._prefetch_tts_audio(chunks[index], index + 1, chunk_count, voice_id)

            if audio_bytes is None:
                self.logger.error("TTS did not return audio bytes for chunk %d/%d", index, chunk_count)
                return

            decoded_audio = self._decode_tts_audio(audio_bytes)
            if decoded_audio is None:
                return

            audio_data, sample_rate = decoded_audio
            if not self._play_audio_data(audio_data, sample_rate, index, chunk_count):
                return

            if next_audio_queue is not None:
                audio_queue = next_audio_queue


class PollingCaptureWatcher:
    def __init__(
        self,
        capture_dir: Path,
        processor: WavCaptureProcessor,
        logger,
        poll_interval_seconds: float,
        reason: str,
    ) -> None:
        self.capture_dir = capture_dir
        self.processor = processor
        self.logger = logger
        self.poll_interval_seconds = poll_interval_seconds
        self.reason = reason
        self.known_mtimes: dict[Path, int] = {}

    def run_forever(self) -> None:
        self.logger.warning("%s", self.reason)
        emit_status(self.logger, "LISTENING", "Watching for Voicebox captures")
        while True:
            for file_path in sorted(self.capture_dir.glob("*.wav")):
                try:
                    mtime = file_path.stat().st_mtime_ns
                except FileNotFoundError:
                    continue

                file_path_normalized = file_path.resolve()
                if self.known_mtimes.get(file_path_normalized) == mtime:
                    continue

                self.known_mtimes[file_path_normalized] = mtime
                self.processor.process_path(file_path)

            time.sleep(self.poll_interval_seconds)


if WATCHDOG_AVAILABLE:

    class VoiceboxEventHandler(FileSystemEventHandler):
        def __init__(self, processor: WavCaptureProcessor, logger) -> None:
            self.processor = processor
            self.logger = logger

        def on_created(self, event) -> None:
            self._handle_event(event)

        def on_modified(self, event) -> None:
            self._handle_event(event)

        def _handle_event(self, event) -> None:
            if event.is_directory:
                return

            file_path = Path(event.src_path)
            self.logger.debug("Voicebox %s event for %s", event.event_type, file_path.name)
            emit_status(self.logger, "THINKING", f"File event: {event.event_type} {file_path.name}")
            self.processor.process_path(file_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Watch Voicebox .wav captures, transcribe locally, send to Mara over SSH, and play TTS reply."
    )
    parser.add_argument("--capture-dir", default=str(DEFAULT_CAPTURE_DIR))
    parser.add_argument("--audio-device", help="Sounddevice output device index or name")
    parser.add_argument(
        "--force-polling",
        action="store_true",
        help="Use directory polling even if watchdog is installed",
    )
    parser.add_argument(
        "--list-audio-devices",
        action="store_true",
        help="Print audio output devices and exit",
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--ssh-target", default=DEFAULT_SSH_TARGET)
    parser.add_argument("--hermes-command", default=DEFAULT_HERMES_COMMAND)
    parser.add_argument("--active-agent", choices=("hermes", "openclaw"), default=DEFAULT_ACTIVE_AGENT)
    parser.add_argument("--agent-route-state-path", default=str(DEFAULT_AGENT_ROUTE_STATE_PATH))
    parser.add_argument("--openclaw-base-url", default=DEFAULT_OPENCLAW_BASE_URL)
    parser.add_argument("--openclaw-model", default=DEFAULT_OPENCLAW_MODEL)
    parser.add_argument(
        "--openclaw-timeout",
        type=lambda value: parse_optional_timeout(value, DEFAULT_OPENCLAW_TIMEOUT_SECONDS),
        default=DEFAULT_OPENCLAW_TIMEOUT_SECONDS,
        help="Seconds to wait for OpenClaw HTTP response. Use 0/none to disable it.",
    )
    parser.add_argument("--tts-url", default=DEFAULT_TTS_URL)
    parser.add_argument(
        "--tts-stream-url",
        default=DEFAULT_TTS_STREAM_URL,
        help="Streaming TTS endpoint used when --tts-streaming is enabled.",
    )
    parser.add_argument(
        "--transcribe-timeout",
        type=float,
        default=DEFAULT_TRANSCRIBE_TIMEOUT_SECONDS,
        help="Seconds to wait for Voicebox transcription API response",
    )
    parser.add_argument(
        "--ssh-timeout",
        type=lambda value: parse_optional_timeout(value, None),
        default=DEFAULT_SSH_TIMEOUT_SECONDS,
        help="Seconds to wait for Mara SSH response. Use 0/none to wait indefinitely.",
    )
    parser.add_argument(
        "--ssh-connect-timeout",
        type=float,
        default=DEFAULT_SSH_CONNECT_TIMEOUT_SECONDS,
        help="Seconds to wait while opening the SSH connection",
    )
    parser.add_argument(
        "--tts-timeout",
        type=lambda value: parse_optional_timeout(value, 300.0),
        default=DEFAULT_TTS_TIMEOUT_SECONDS,
        help="Seconds to wait for local TTS audio generation. Use 0/none to disable it.",
    )
    parser.add_argument(
        "--tts-retry-attempts",
        type=int,
        default=DEFAULT_TTS_RETRY_ATTEMPTS,
        help="Number of extra TTS attempts after a timeout or request failure",
    )
    parser.add_argument(
        "--status-interval",
        type=float,
        default=DEFAULT_STATUS_INTERVAL_SECONDS,
        help="Seconds between terminal heartbeat updates while Mara/TTS are still working. Use 0 to disable.",
    )
    parser.add_argument(
        "--spoken-reply-char-limit",
        type=int,
        default=DEFAULT_SPOKEN_REPLY_CHAR_LIMIT,
        help="Maximum characters to send to TTS. Use 0 to speak the full reply.",
    )
    parser.add_argument(
        "--tts-chunk-char-limit",
        type=int,
        default=DEFAULT_TTS_CHUNK_CHAR_LIMIT,
        help="Maximum characters per TTS request/playback chunk. Use 0 to render one WAV before playback.",
    )
    parser.add_argument(
        "--tts-stream-chunk-char-limit",
        type=int,
        default=DEFAULT_TTS_STREAM_CHUNK_CHAR_LIMIT,
        help="Maximum characters per true-streaming TTS segment. Smaller values start and complete segments faster.",
    )
    streaming_group = parser.add_mutually_exclusive_group()
    streaming_group.add_argument(
        "--tts-streaming",
        dest="tts_streaming",
        action="store_true",
        default=DEFAULT_TTS_STREAMING,
        help="Use the raw PCM streaming TTS endpoint before falling back to WAV/chunked playback.",
    )
    streaming_group.add_argument(
        "--no-tts-streaming",
        dest="tts_streaming",
        action="store_false",
        help="Disable streaming TTS even if MARA_TTS_STREAMING=1.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    logger = configure_logging(args.log_level).getChild("listener")

    if args.list_audio_devices:
        print(format_output_devices())
        return 0

    capture_dir = Path(args.capture_dir)
    if not capture_dir.exists():
        logger.error(
            "Voicebox capture directory does not exist: %s",
            capture_dir,
        )
        return 1

    logger.info("Watching for Voicebox .wav captures in %s", capture_dir)
    logger.info("Voicebox transcription API: %s", DEFAULT_VOICEBOX_API)
    logger.info("TTS server: %s", args.tts_url)
    logger.info("TTS streaming endpoint: %s", args.tts_stream_url)
    logger.info("Mara SSH target: %s", args.ssh_target)
    logger.info("Hermes command: %s", args.hermes_command)
    logger.info("Default agent route: %s", args.active_agent)
    logger.info("Agent route state path: %s", args.agent_route_state_path)
    logger.info("OpenClaw API: %s (model=%s)", redact_url(args.openclaw_base_url), args.openclaw_model)
    logger.info(
        "Timeouts: transcribe=%s ssh=%s openclaw=%s tts=%s connect=%s status_interval=%.1fs",
        format_timeout(args.transcribe_timeout),
        format_timeout(args.ssh_timeout),
        format_timeout(args.openclaw_timeout),
        format_timeout(args.tts_timeout),
        format_timeout(args.ssh_connect_timeout),
        args.status_interval,
    )
    logger.info("Spoken reply character limit: %s", args.spoken_reply_char_limit or "disabled")
    logger.info("TTS playback chunk character limit: %s", args.tts_chunk_char_limit or "disabled")
    logger.info("TTS streaming chunk character limit: %s", args.tts_stream_chunk_char_limit)
    logger.info("TTS streaming: %s", "enabled" if args.tts_streaming else "disabled")
    if args.audio_device:
        logger.info("Audio device: %s", args.audio_device)
    try:
        update_agent_route_state(
            Path(args.agent_route_state_path),
            fallback_active_agent=args.active_agent,
            active_agent=args.active_agent,
            next_agent="",
        )
    except Exception as exc:
        logger.warning("Could not initialize agent route state: %s", exc)
    emit_status(logger, "LISTENING", "Ready")

    processor = WavCaptureProcessor(
        capture_dir=capture_dir,
        ssh_target=args.ssh_target,
        hermes_command=args.hermes_command,
        active_agent=args.active_agent,
        openclaw_base_url=args.openclaw_base_url,
        openclaw_model=args.openclaw_model,
        tts_url=args.tts_url,
        tts_stream_url=args.tts_stream_url,
        audio_device=args.audio_device,
        logger=logger,
        agent_route_state_path=Path(args.agent_route_state_path),
        transcribe_timeout_seconds=args.transcribe_timeout,
        ssh_timeout_seconds=args.ssh_timeout,
        openclaw_timeout_seconds=args.openclaw_timeout,
        tts_timeout_seconds=args.tts_timeout,
        tts_retry_attempts=args.tts_retry_attempts,
        status_interval_seconds=args.status_interval,
        ssh_connect_timeout_seconds=args.ssh_connect_timeout,
        spoken_reply_char_limit=args.spoken_reply_char_limit,
        tts_chunk_char_limit=args.tts_chunk_char_limit,
        tts_streaming=args.tts_streaming,
        tts_stream_chunk_char_limit=args.tts_stream_chunk_char_limit,
    )

    if WATCHDOG_AVAILABLE and not args.force_polling:
        observer = Observer()
        observer.schedule(VoiceboxEventHandler(processor, logger), str(capture_dir), recursive=False)
        observer.start()
        logger.info("watchdog observer started")

        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            logger.info("Stopping Voicebox listener")
            observer.stop()
        observer.join()
        return 0

    try:
        if args.force_polling:
            polling_reason = (
                f"Polling mode forced. Scanning {capture_dir} every {args.poll_interval:.2f}s."
            )
        else:
            polling_reason = (
                "watchdog is not installed, using polling mode every "
                f"{args.poll_interval:.2f}s. Install watchdog for faster file detection."
            )
        PollingCaptureWatcher(
            capture_dir,
            processor,
            logger,
            args.poll_interval,
            polling_reason,
        ).run_forever()
    except KeyboardInterrupt:
        logger.info("Stopping Voicebox listener")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
