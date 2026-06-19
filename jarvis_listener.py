from __future__ import annotations

import argparse
import io
import json
import os
import random
import time
from datetime import datetime
from queue import Empty, Queue
from pathlib import Path
from threading import Event, Lock, Thread

from dataclasses import replace

import requests
import sounddevice as sd
import soundfile as sf

from mara_agent_state import DEFAULT_AGENT_ROUTE_STATE_PATH, consume_agent_route, update_agent_route_state
from mara_agents import (
    DEFAULT_ACTIVE_AGENT,
    DEFAULT_AGENT_SESSION_HISTORY_MESSAGES,
    DEFAULT_AGENT_SESSION_HISTORY_PATH,
    DEFAULT_AGENT_SESSION_ID,
    DEFAULT_AGENT_SESSION_PERSISTENCE,
    DEFAULT_HERMES_SESSION_ID,
    DEFAULT_OPENCLAW_BASE_URL,
    DEFAULT_OPENCLAW_MODEL,
    DEFAULT_OPENCLAW_TIMEOUT_SECONDS,
    DEFAULT_OPENCLAW_SESSION_ID,
    AgentError,
    AgentReply,
    AgentSettings,
    ask_agent,
    agent_display_name,
    normalize_agent_id,
    redact_url,
)
from mara_events import append_event, read_recent_events
from mara_pipeline import (
    DEFAULT_CAPTURE_DIR,
    DEFAULT_HERMES_COMMAND,
    DEFAULT_SSH_TARGET,
    configure_logging,
    emit_status,
    format_output_devices,
)
from mara_safety import redact_sensitive_text
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

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_VOICEBOX_API = "http://127.0.0.1:17493/transcribe"
DEFAULT_TTS_URL = "http://127.0.0.1:8000/tts"
DEFAULT_TRANSCRIBE_TIMEOUT_SECONDS = float(os.getenv("MARA_TRANSCRIBE_TIMEOUT", "60"))
DEFAULT_TTS_RETRY_ATTEMPTS = int(os.getenv("MARA_TTS_RETRY_ATTEMPTS", "1"))
DEFAULT_STATUS_INTERVAL_SECONDS = float(os.getenv("MARA_STATUS_INTERVAL", "10"))
DEFAULT_SSH_CONNECT_TIMEOUT_SECONDS = float(os.getenv("MARA_SSH_CONNECT_TIMEOUT", "5"))
DEFAULT_SPOKEN_REPLY_CHAR_LIMIT = int(os.getenv("MARA_SPOKEN_REPLY_CHAR_LIMIT", "900"))
DEFAULT_TTS_CHUNK_CHAR_LIMIT = int(os.getenv("MARA_TTS_CHUNK_CHAR_LIMIT", "450"))
DEFAULT_TTS_STREAMING = os.getenv("MARA_TTS_STREAMING", "1") == "1"
DEFAULT_TTS_STREAM_URL = os.getenv("MARA_TTS_STREAM_URL", "http://127.0.0.1:8000/tts/stream")
DEFAULT_TTS_STREAM_CHUNK_CHAR_LIMIT = int(os.getenv("MARA_TTS_STREAM_CHUNK_CHAR_LIMIT", "1200"))
DEFAULT_TTS_STREAM_PREBUFFER_SECONDS = float(os.getenv("MARA_TTS_STREAM_PREBUFFER_SECONDS", "0.5"))
DEFAULT_TTS_STREAM_PREBUFFER_DYNAMIC = os.getenv("MARA_TTS_STREAM_PREBUFFER_DYNAMIC", "1") == "1"
DEFAULT_TTS_STREAM_PREBUFFER_MAX_SECONDS = float(os.getenv("MARA_TTS_STREAM_PREBUFFER_MAX_SECONDS", "4"))
DEFAULT_PROCESS_EXISTING_SECONDS = float(os.getenv("MARA_PROCESS_EXISTING_SECONDS", "600"))
DEFAULT_ASYNC_AGENT_REPLIES = os.getenv("MARA_ASYNC_AGENT_REPLIES", "1") == "1"
# Agent-generated acks need their own agent round trip, so they arrive about when the
# real answer does and cannot fill dead air. Preset phrases are spoken instantly, so
# they are the useful default; agent acks remain available via MARA_ASYNC_ACK_AGENT=1.
DEFAULT_ASYNC_ACK_AGENT = os.getenv("MARA_ASYNC_ACK_AGENT", "0") == "1"
DEFAULT_ASYNC_ACK_GRACE_SECONDS = float(os.getenv("MARA_ASYNC_ACK_GRACE_SECONDS", "5"))
DEFAULT_ASYNC_ACK_TEXT = os.getenv(
    "MARA_ASYNC_ACK_TEXT",
    "Yeah, let me dig into that real quick.",
)
DEFAULT_ASYNC_ACK_PHRASE_POOL = "\n".join(
    [
        "On it.",
        "Got it, working on that now.",
        "Sure, give me a sec.",
        "Alright, let me look into that.",
        "One moment.",
    ]
)
DEFAULT_ASYNC_ACK_PHRASES = os.getenv("MARA_ASYNC_ACK_PHRASES", DEFAULT_ASYNC_ACK_PHRASE_POOL)
DEFAULT_ASYNC_FOLLOWUP_ENABLED = os.getenv("MARA_ASYNC_FOLLOWUP_ENABLED", "1") == "1"
DEFAULT_ASYNC_FOLLOWUP_INITIAL_DELAY_SECONDS = float(os.getenv("MARA_ASYNC_FOLLOWUP_INITIAL_DELAY_SECONDS", "3"))
DEFAULT_ASYNC_FOLLOWUP_POLL_SECONDS = float(os.getenv("MARA_ASYNC_FOLLOWUP_POLL_SECONDS", "8"))
DEFAULT_ASYNC_FOLLOWUP_MAX_ATTEMPTS = int(os.getenv("MARA_ASYNC_FOLLOWUP_MAX_ATTEMPTS", "60"))
# Idle heartbeat: when enabled, the active agent is periodically asked for an unprompted
# check-in and speaks the result on its own. Default off so it never surprises the user.
DEFAULT_HEARTBEAT_ENABLED = os.getenv("MARA_HEARTBEAT_ENABLED", "0") == "1"
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("MARA_HEARTBEAT_INTERVAL_SECONDS", "1800"))
DEFAULT_HEARTBEAT_IDLE_SECONDS = float(os.getenv("MARA_HEARTBEAT_IDLE_SECONDS", "300"))
DEFAULT_HEARTBEAT_SENTINEL = os.getenv("MARA_HEARTBEAT_SENTINEL", "NOTHING")
DEFAULT_HEARTBEAT_PROMPT = os.getenv(
    "MARA_HEARTBEAT_PROMPT",
    "Automatic check-in, not from Ant. If a background task just finished or you have "
    "something important to tell Ant, say it in ONE short spoken sentence. If there is "
    f"nothing new to report, reply with exactly {DEFAULT_HEARTBEAT_SENTINEL} and nothing else.",
)
DEFAULT_HEARTBEAT_QUIET_HOURS = os.getenv("MARA_HEARTBEAT_QUIET_HOURS", "")
DEFAULT_VOICE_INBOX_PATH = Path(
    os.getenv("MARA_VOICE_INBOX_PATH", str(BASE_DIR / "config" / "mara_voice_inbox.jsonl"))
)
DEFAULT_VOICE_INBOX_POLL_SECONDS = float(os.getenv("MARA_VOICE_INBOX_POLL_SECONDS", "5"))

DEFERRED_REPLY_MARKERS = (
    "background agent",
    "background task",
    "running asynchronously",
    "it's running",
    "it is running",
    "when it finishes",
    "whenever it finishes",
    "once it finishes",
    "when it's done",
    "once it's done",
    "i'll follow up",
    "i will follow up",
    "i'll update",
    "i will update",
    "i'll let you know",
    "i will let you know",
    "i'll pop back",
    "pop right back",
    # NOTE: bare "sub-agent"/"subagent" are intentionally NOT markers - simply mentioning
    # sub-agents in a finished answer must not be mistaken for deferred/ongoing work. A
    # genuine deferral still matches the future/ongoing phrases above (e.g. "I'll follow
    # up", "when it finishes").
)

PENDING_FOLLOWUP_EXACT = {"STILL_WORKING", "NOT_READY", "PENDING"}
COMPLETION_REPLY_MARKERS = ("complete", "completed", "done", "finished", "final result", "result is ready")


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


def looks_like_deferred_reply(text: str) -> bool:
    normalized = " ".join(text.lower().split())
    return any(marker in normalized for marker in DEFERRED_REPLY_MARKERS)


def is_pending_followup_reply(text: str) -> bool:
    normalized = " ".join(text.strip().split())
    if normalized.upper().rstrip(".!") in PENDING_FOLLOWUP_EXACT:
        return True
    lowered = normalized.lower()
    if any(marker in lowered for marker in COMPLETION_REPLY_MARKERS):
        return False
    if len(normalized) <= 280 and looks_like_deferred_reply(normalized):
        return True
    return False


def find_existing_capture_files(
    capture_dir: Path,
    max_age_seconds: float,
    now_seconds: float | None = None,
    newer_than_seconds: float | None = None,
) -> list[Path]:
    if max_age_seconds <= 0:
        return []

    now = time.time() if now_seconds is None else now_seconds
    cutoff = now - max_age_seconds
    if newer_than_seconds is not None:
        cutoff = max(cutoff, newer_than_seconds)
    captures: list[tuple[float, Path]] = []
    for file_path in capture_dir.glob("*.wav"):
        try:
            modified = file_path.stat().st_mtime
        except OSError:
            continue
        if modified >= cutoff:
            captures.append((modified, file_path))

    return [file_path for _modified, file_path in sorted(captures)]


def latest_event_timestamp_seconds() -> float | None:
    for event in reversed(read_recent_events(limit=200)):
        timestamp = event.get("timestamp")
        if not isinstance(timestamp, str) or not timestamp:
            continue
        try:
            return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
    return None


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
        tts_stream_prebuffer_seconds: float = DEFAULT_TTS_STREAM_PREBUFFER_SECONDS,
        tts_stream_prebuffer_dynamic: bool = DEFAULT_TTS_STREAM_PREBUFFER_DYNAMIC,
        tts_stream_prebuffer_max_seconds: float = DEFAULT_TTS_STREAM_PREBUFFER_MAX_SECONDS,
        async_agent_replies: bool = DEFAULT_ASYNC_AGENT_REPLIES,
        async_ack_agent: bool = DEFAULT_ASYNC_ACK_AGENT,
        async_ack_grace_seconds: float = DEFAULT_ASYNC_ACK_GRACE_SECONDS,
        async_ack_text: str = DEFAULT_ASYNC_ACK_TEXT,
        async_ack_phrases: str = DEFAULT_ASYNC_ACK_PHRASES,
        async_followup_enabled: bool = DEFAULT_ASYNC_FOLLOWUP_ENABLED,
        async_followup_initial_delay_seconds: float = DEFAULT_ASYNC_FOLLOWUP_INITIAL_DELAY_SECONDS,
        async_followup_poll_seconds: float = DEFAULT_ASYNC_FOLLOWUP_POLL_SECONDS,
        async_followup_max_attempts: int = DEFAULT_ASYNC_FOLLOWUP_MAX_ATTEMPTS,
        heartbeat_enabled: bool = DEFAULT_HEARTBEAT_ENABLED,
        heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        heartbeat_idle_seconds: float = DEFAULT_HEARTBEAT_IDLE_SECONDS,
        heartbeat_prompt: str = DEFAULT_HEARTBEAT_PROMPT,
        heartbeat_sentinel: str = DEFAULT_HEARTBEAT_SENTINEL,
        heartbeat_quiet_hours: str = DEFAULT_HEARTBEAT_QUIET_HOURS,
        agent_session_id: str = DEFAULT_AGENT_SESSION_ID,
        hermes_session_id: str = DEFAULT_HERMES_SESSION_ID,
        openclaw_session_id: str = DEFAULT_OPENCLAW_SESSION_ID,
        agent_session_history_path: Path = DEFAULT_AGENT_SESSION_HISTORY_PATH,
        agent_session_history_messages: int = DEFAULT_AGENT_SESSION_HISTORY_MESSAGES,
        agent_session_persistence: bool = DEFAULT_AGENT_SESSION_PERSISTENCE,
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
        self.tts_stream_prebuffer_seconds = max(0.0, tts_stream_prebuffer_seconds)
        self.tts_stream_prebuffer_dynamic = tts_stream_prebuffer_dynamic
        self.tts_stream_prebuffer_max_seconds = max(self.tts_stream_prebuffer_seconds, tts_stream_prebuffer_max_seconds)
        self.async_agent_replies = async_agent_replies
        self.async_ack_agent = bool(async_ack_agent)
        self.async_ack_grace_seconds = max(0.0, async_ack_grace_seconds)
        self.async_ack_text = " ".join(str(async_ack_text or "").split())
        self.async_ack_phrases = self._parse_ack_phrases(async_ack_phrases)
        self.async_followup_enabled = async_followup_enabled
        self.async_followup_initial_delay_seconds = max(0.0, async_followup_initial_delay_seconds)
        self.async_followup_poll_seconds = max(1.0, async_followup_poll_seconds)
        self.async_followup_max_attempts = max(0, int(async_followup_max_attempts))
        self.heartbeat_enabled = bool(heartbeat_enabled)
        self.heartbeat_interval_seconds = max(0.0, heartbeat_interval_seconds)
        self.heartbeat_idle_seconds = max(0.0, heartbeat_idle_seconds)
        self.heartbeat_prompt = " ".join(str(heartbeat_prompt or "").split())
        self.heartbeat_sentinel = " ".join(str(heartbeat_sentinel or "NOTHING").split()) or "NOTHING"
        self._heartbeat_quiet_hours = self._parse_quiet_hours(heartbeat_quiet_hours)
        self.agent_session_id = agent_session_id
        self.hermes_session_id = hermes_session_id
        self.openclaw_session_id = openclaw_session_id
        self.agent_session_history_path = agent_session_history_path
        self.agent_session_history_messages = max(0, int(agent_session_history_messages))
        self.agent_session_persistence = agent_session_persistence
        self.voicebox_session = requests.Session()
        self.tts_session = requests.Session()
        self.agent_session = requests.Session()
        self.playback_lock = Lock()
        self.recent_files: dict[Path, float] = {}
        self.processed_files: dict[Path, float] = {}
        self.processed_file_retention_seconds = 24 * 60 * 60
        # Idle tracking for the heartbeat: a turn is "in flight" from the moment a
        # capture starts processing until any async worker/follow-up for it finishes.
        self._activity_lock = Lock()
        self._inflight = 0
        self._last_activity_monotonic = time.monotonic()
        self._heartbeat_stop = Event()
        self._heartbeat_thread: Thread | None = None
        self._heartbeat_last_run_monotonic = 0.0

    @staticmethod
    def _parse_quiet_hours(raw: str) -> tuple[int, int] | None:
        text = str(raw or "").strip()
        if not text or "-" not in text:
            return None
        start_text, _, end_text = text.partition("-")
        try:
            start = int(start_text) % 24
            end = int(end_text) % 24
        except ValueError:
            return None
        if start == end:
            return None
        return start, end

    def _begin_activity(self) -> None:
        with self._activity_lock:
            self._inflight += 1
            self._last_activity_monotonic = time.monotonic()

    def _end_activity(self) -> None:
        with self._activity_lock:
            self._inflight = max(0, self._inflight - 1)
            self._last_activity_monotonic = time.monotonic()

    def _is_idle(self, min_idle_seconds: float) -> bool:
        with self._activity_lock:
            if self._inflight > 0:
                return False
            return (time.monotonic() - self._last_activity_monotonic) >= min_idle_seconds

    def _in_quiet_hours(self) -> bool:
        if not self._heartbeat_quiet_hours:
            return False
        start, end = self._heartbeat_quiet_hours
        hour = time.localtime().tm_hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _is_heartbeat_noop(self, text: str) -> bool:
        normalized = " ".join(text.split()).strip().rstrip(".!").upper()
        if not normalized or normalized == self.heartbeat_sentinel.upper():
            return True
        return is_pending_followup_reply(text)

    def start_heartbeat(self) -> None:
        if not self.heartbeat_enabled or self.heartbeat_interval_seconds <= 0:
            return
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_stop.clear()
        self._heartbeat_thread = Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        self.logger.info(
            "Heartbeat enabled: checking in every %.0fs when idle >= %.0fs",
            self.heartbeat_interval_seconds,
            self.heartbeat_idle_seconds,
        )

    def stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()

    def _heartbeat_loop(self) -> None:
        check_seconds = max(5.0, min(self.heartbeat_interval_seconds, 30.0))
        while not self._heartbeat_stop.wait(check_seconds):
            try:
                self._heartbeat_tick()
            except Exception as exc:  # never let the heartbeat thread die
                self.logger.warning("Heartbeat tick failed: %s", exc)

    def _heartbeat_tick(self) -> None:
        now = time.monotonic()
        if now - self._heartbeat_last_run_monotonic < self.heartbeat_interval_seconds:
            return
        if self._in_quiet_hours():
            return
        if not self._is_idle(self.heartbeat_idle_seconds):
            return
        settings = self._agent_settings_for_next_route()
        if settings is None:
            return
        if not self.heartbeat_prompt:
            return

        self._heartbeat_last_run_monotonic = now
        # Run the check-in under a throwaway, non-persisted session so the meta-prompt
        # never leaks into (or is remembered by) the real conversation history.
        hb_settings = replace(
            settings,
            agent_session_persistence=False,
            agent_session_id=(settings.agent_session_id or DEFAULT_AGENT_SESSION_ID) + "-heartbeat",
            hermes_session_id="",
            openclaw_session_id="",
        )
        self._begin_activity()
        session = requests.Session()
        try:
            reply = self._request_agent_reply(self.heartbeat_prompt, hb_settings, requests_session=session)
        finally:
            session.close()
            self._end_activity()

        if reply is None:
            return
        if self._is_heartbeat_noop(reply.text):
            self.logger.info("Heartbeat: %s had nothing to report", reply.agent_name)
            return
        self.logger.info("Heartbeat: %s has an unprompted update", reply.agent_name)
        self._handle_agent_reply(
            reply,
            pipeline_start=None,
            pipeline_agent_seconds=None,
            source="heartbeat",
            terminal_label="[Mara/heartbeat]",
        )

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

    def _agent_settings_for_next_route(self) -> AgentSettings | None:
        try:
            route = consume_agent_route(
                self.agent_route_state_path,
                fallback_active_agent=self.active_agent,
            )
        except Exception as exc:
            self.logger.error("Could not read agent route state: %s", exc)
            self._append_event("error", "ERROR", "Could not read agent route state", {"error": str(exc)})
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
            agent_session_id=self.agent_session_id,
            hermes_session_id=self.hermes_session_id,
            openclaw_session_id=self.openclaw_session_id,
            agent_session_history_path=self.agent_session_history_path,
            agent_session_history_messages=self.agent_session_history_messages,
            agent_session_persistence=self.agent_session_persistence,
        )
        if route.one_shot:
            self.logger.info("Using one-shot agent route: %s", route.agent_id)
        return settings

    def _request_agent_reply(
        self,
        text: str,
        settings: AgentSettings,
        requests_session: requests.Session | None = None,
    ) -> AgentReply | None:
        try:
            return ask_agent(
                text,
                settings,
                self.logger,
                status_callback=lambda status, message, details=None: emit_status(
                    self.logger, status, message, details
                ),
                status_interval_seconds=self.status_interval_seconds,
                requests_session=requests_session or self.agent_session,
            )
        except AgentError as exc:
            self.logger.error("Agent request failed: %s", exc)
            self._append_event(
                "error",
                "ERROR",
                "Agent request failed",
                {"agent": settings.active_agent, "error": str(exc)},
            )
            return None

    def _send_to_agent(self, text: str, settings: AgentSettings | None = None) -> AgentReply | None:
        if settings is None:
            settings = self._agent_settings_for_next_route()
        if settings is None:
            return None
        return self._request_agent_reply(text, settings)

    def _play_spoken_reply(self, spoken_reply: str, voice_id: str, message: str) -> float:
        tts_start = time.monotonic()
        with self.playback_lock:
            emit_status(self.logger, "TALKING", message)
            self._play_tts_reply(spoken_reply, voice_id=voice_id)
        return time.monotonic() - tts_start

    def _handle_agent_reply(
        self,
        agent_reply: AgentReply,
        pipeline_start: float | None,
        pipeline_agent_seconds: float | None,
        source: str = "agent",
        terminal_label: str = "[Mara]",
    ) -> None:
        safe_mara_reply = redact_sensitive_text(agent_reply.text)
        print(f"{terminal_label}: {safe_mara_reply}")
        details: dict[str, object] = {
            "text": safe_mara_reply,
            "agent": agent_reply.agent_id,
            "agent_name": agent_reply.agent_name,
            "agent_seconds": round(agent_reply.elapsed_seconds, 3),
            "source": source,
        }
        if pipeline_agent_seconds is not None:
            details["pipeline_agent_seconds"] = round(pipeline_agent_seconds, 3)
        if source in ("async", "async_followup"):
            details["async"] = True

        event_message = f"{agent_reply.agent_name} reply"
        if source == "async":
            event_message = f"{agent_reply.agent_name} async reply"
        elif source == "async_followup":
            event_message = f"{agent_reply.agent_name} async follow-up"
        elif source == "voice_inbox":
            event_message = "Voice inbox reply"
        elif source == "heartbeat":
            event_message = f"{agent_reply.agent_name} check-in"

        self._append_event("conversation", "MARA", event_message, details)
        self.logger.info(
            "%s replied: %s (agent: %.2fs, source=%s)",
            agent_reply.agent_name,
            safe_mara_reply,
            agent_reply.elapsed_seconds,
            source,
        )
        spoken_reply = prepare_spoken_reply(safe_mara_reply, self.spoken_reply_char_limit)
        if spoken_reply != " ".join(safe_mara_reply.split()):
            self.logger.info(
                "Shortened spoken reply from %d to %d characters for faster TTS",
                len(" ".join(safe_mara_reply.split())),
                len(spoken_reply),
            )

        tts_time = self._play_spoken_reply(
            spoken_reply,
            agent_reply.agent_id,
            "Generating and playing reply",
        )
        spoken_chars = len(spoken_reply)
        chars_per_second = round(spoken_chars / tts_time, 1) if tts_time > 0 else 0.0
        timing_details: dict[str, object] = {
            "agent": agent_reply.agent_id,
            "agent_name": agent_reply.agent_name,
            "agent_seconds": round(agent_reply.elapsed_seconds, 3),
            "tts_seconds": round(tts_time, 3),
            "spoken_chars": spoken_chars,
            "chars_per_second": chars_per_second,
            "source": source,
        }
        if pipeline_start is not None:
            total_time = time.monotonic() - pipeline_start
            timing_details["total_seconds"] = round(total_time, 3)
            self.logger.info("Pipeline complete (TTS: %.2fs, total: %.2fs)", tts_time, total_time)
        else:
            self.logger.info("Voice inbox playback complete (TTS: %.2fs)", tts_time)
        self._append_event(
            "metric",
            "TIMING",
            f"{agent_reply.agent_name} spoke in {tts_time:.2f}s ({chars_per_second:.1f} chars/s)",
            timing_details,
        )
        emit_status(self.logger, "LISTENING", "Waiting for next capture")

    @staticmethod
    def _parse_ack_phrases(raw: object) -> list[str]:
        items = raw if isinstance(raw, (list, tuple)) else str(raw or "").splitlines()
        phrases: list[str] = []
        for item in items:
            cleaned = " ".join(str(item).split())
            if cleaned:
                phrases.append(cleaned)
        return phrases

    def _pick_static_ack(self) -> str:
        if self.async_ack_phrases:
            return random.choice(self.async_ack_phrases)
        return self.async_ack_text

    def _build_ack_prompt(self, original_text: str) -> str:
        return "\n".join(
            [
                "You are the user's voice assistant, replying out loud.",
                "In ONE very short spoken sentence of 10 words or fewer, acknowledge that you heard the request and are starting on it.",
                "Do not answer or solve the request yet, and do not state or change your name.",
                "No preamble, no lists, no markdown - just the brief spoken sentence.",
                "",
                "User request:",
                original_text,
            ]
        )

    def _generate_agent_ack(self, original_text: str, settings: AgentSettings) -> str | None:
        # Acknowledgement turns are throwaway: do not persist them, and run them under a
        # separate "-ack" session id so the meta-prompt cannot leak into (or be remembered
        # by) the real conversation on the agent/gateway side.
        ack_base = (settings.agent_session_id or DEFAULT_AGENT_SESSION_ID) + "-ack"
        ack_settings = replace(
            settings,
            agent_session_persistence=False,
            agent_session_id=ack_base,
            hermes_session_id="",
            openclaw_session_id="",
        )
        session = requests.Session()
        try:
            reply = self._request_agent_reply(
                self._build_ack_prompt(original_text), ack_settings, requests_session=session
            )
        finally:
            session.close()
        if reply is None:
            return None
        text = " ".join(reply.text.split())
        return text or None

    def _speak_ack(self, original_text: str, settings: AgentSettings, reply_started: Event | None = None) -> None:
        voice_id = settings.active_agent
        if self.async_ack_agent:
            emit_status(
                self.logger,
                "THINKING",
                "Composing a quick acknowledgement",
                {"agent": voice_id, "async": True},
            )
            generated: str | None = None
            try:
                generated = self._generate_agent_ack(original_text, settings)
            except Exception as exc:
                self.logger.warning("Agent acknowledgement generation failed: %s", exc)
            # Fall back to a preset phrase if generation fails or returns nothing.
            spoken_text = generated or self._pick_static_ack()
            # Agent acks take their own full round trip, which often finishes right as
            # the real answer does. Give the answer priority: if it lands now or within
            # a moment, drop the ack so we never speak a redundant, out-of-order reply.
            if reply_started is not None and reply_started.wait(timeout=1.5):
                self.logger.info("Skipping acknowledgement; the agent reply already arrived")
                return
        else:
            spoken_text = self._pick_static_ack()
            # Preset acks are instant, so only skip if the answer is already in hand.
            if reply_started is not None and reply_started.is_set():
                self.logger.info("Skipping acknowledgement; the agent reply already arrived")
                return

        safe_ack = redact_sensitive_text(spoken_text)
        if not safe_ack:
            return

        print(f"[Mara]: {safe_ack}")
        self._append_event(
            "conversation",
            "MARA",
            "Mara acknowledgement",
            {
                "text": safe_ack,
                "agent": voice_id,
                "agent_name": agent_display_name(voice_id),
                "async": True,
                "source": "ack",
                "ack_mode": "agent" if self.async_ack_agent else "preset",
            },
        )
        self._play_spoken_reply(safe_ack, voice_id, "Playing acknowledgement")

    def _build_async_followup_prompt(self, original_text: str, attempt: int) -> str:
        return "\n".join(
            [
                "Automatic Mara Voice follow-up check.",
                "Your previous reply said background work was still running.",
                "Do not start a new task. Check the existing work in this same session.",
                "If the result is ready, return only the final answer or useful update for voice playback.",
                "If it is not ready yet, return exactly: STILL_WORKING",
                f"Follow-up attempt: {attempt}",
                "",
                "Original user request:",
                original_text,
            ]
        )

    def _poll_async_followup(
        self,
        original_text: str,
        settings: AgentSettings,
        pipeline_start: float,
    ) -> None:
        if not self.async_followup_enabled or self.async_followup_max_attempts <= 0:
            return

        if self.async_followup_initial_delay_seconds:
            time.sleep(self.async_followup_initial_delay_seconds)

        for attempt in range(1, self.async_followup_max_attempts + 1):
            emit_status(
                self.logger,
                "THINKING",
                f"Checking background result ({attempt}/{self.async_followup_max_attempts})",
                {"agent": settings.active_agent, "async_followup": True, "attempt": attempt},
            )
            session = requests.Session()
            try:
                followup_reply = self._request_agent_reply(
                    self._build_async_followup_prompt(original_text, attempt),
                    settings,
                    requests_session=session,
                )
            finally:
                session.close()

            if followup_reply is not None and not is_pending_followup_reply(followup_reply.text):
                self._handle_agent_reply(
                    followup_reply,
                    pipeline_start=pipeline_start,
                    pipeline_agent_seconds=None,
                    source="async_followup",
                    terminal_label="[Mara/follow-up]",
                )
                return

            emit_status(
                self.logger,
                "LISTENING",
                "Background task still working",
                {"agent": settings.active_agent, "async_followup": True, "attempt": attempt},
            )
            if attempt < self.async_followup_max_attempts:
                time.sleep(self.async_followup_poll_seconds)

        timeout_reply = AgentReply(
            settings.active_agent,
            agent_display_name(settings.active_agent),
            "I am still waiting on that background work and could not retrieve a final result yet.",
            0.0,
        )
        self._handle_agent_reply(
            timeout_reply,
            pipeline_start=pipeline_start,
            pipeline_agent_seconds=None,
            source="async_followup",
            terminal_label="[Mara/follow-up]",
        )

    def _complete_async_agent_reply(
        self,
        text: str,
        settings: AgentSettings,
        pipeline_start: float,
        reply_started: Event | None = None,
    ) -> None:
        try:
            request_start = time.monotonic()
            session = requests.Session()
            try:
                agent_reply = self._request_agent_reply(text, settings, requests_session=session)
            finally:
                session.close()
            agent_time = time.monotonic() - request_start
            # Tell the acknowledgement path the real answer is in, so a fast reply can
            # skip the ack entirely instead of speaking ack + answer.
            if reply_started is not None:
                reply_started.set()
            if agent_reply is None:
                emit_status(self.logger, "LISTENING", "Waiting for next capture")
                return

            self._handle_agent_reply(
                agent_reply,
                pipeline_start=pipeline_start,
                pipeline_agent_seconds=agent_time,
                source="async",
            )
            # Only poll for a later result if the reply actually says work is still
            # running. is_pending_followup_reply also requires the reply to be short and
            # free of completion words, so a complete answer that merely *mentions*
            # sub-agents (e.g. "I spun up two sub-agents and they found ...") is not
            # mistaken for a deferral and does not trigger a redundant second answer.
            if is_pending_followup_reply(agent_reply.text):
                self._poll_async_followup(text, settings, pipeline_start)
        finally:
            # Release the in-flight marker taken in _start_async_agent_reply, including
            # any follow-up polling above, so the idle heartbeat only fires when truly idle.
            self._end_activity()
            # Ensure a stuck/ack-only reply never leaves the ack path waiting.
            if reply_started is not None:
                reply_started.set()

    def _start_async_agent_reply(
        self,
        text: str,
        pipeline_start: float,
        settings: AgentSettings | None = None,
    ) -> Thread | None:
        if settings is None:
            settings = self._agent_settings_for_next_route()
        if settings is None:
            emit_status(self.logger, "LISTENING", "Waiting for next capture")
            return None

        reply_started = Event()
        # Mark the turn in flight synchronously so the heartbeat can't slip in during
        # the brief gap before the worker thread starts; the worker releases it.
        self._begin_activity()
        thread = Thread(
            target=self._complete_async_agent_reply,
            args=(text, settings, pipeline_start, reply_started),
            daemon=True,
        )
        thread.start()
        emit_status(
            self.logger,
            "THINKING",
            f"{agent_display_name(settings.active_agent)} is working in the background",
            {"agent": settings.active_agent, "async": True},
        )
        self._maybe_speak_ack(text, settings, reply_started)
        emit_status(self.logger, "LISTENING", "Waiting for next capture while Mara works")
        return thread

    def _maybe_speak_ack(self, text: str, settings: AgentSettings, reply_started: Event) -> None:
        # Only fill dead air if the real answer is slow. If it arrives within the grace
        # window, skip the acknowledgement so short prompts get a single spoken response.
        if self.async_ack_grace_seconds > 0 and reply_started.wait(timeout=self.async_ack_grace_seconds):
            return
        self._speak_ack(text, settings, reply_started)

    def process_voice_inbox_payload(self, payload: object) -> bool:
        if isinstance(payload, str):
            data: dict[str, object] = {"text": payload}
        elif isinstance(payload, dict):
            data = payload
        else:
            self.logger.warning("Ignoring voice inbox payload with unsupported type: %s", type(payload).__name__)
            return False

        text = str(data.get("text") or data.get("message") or "").strip()
        if not text:
            self.logger.warning("Ignoring voice inbox payload without text")
            return False

        raw_voice_id = str(data.get("voice_id") or data.get("agent") or self.active_agent)
        try:
            voice_id = normalize_agent_id(raw_voice_id)
        except ValueError:
            self.logger.warning("Voice inbox requested unsupported voice_id=%s; using Hermes", raw_voice_id)
            voice_id = "hermes"

        agent_name = str(data.get("agent_name") or agent_display_name(voice_id))
        try:
            elapsed_seconds = float(data.get("elapsed_seconds") or 0.0)
        except (TypeError, ValueError):
            elapsed_seconds = 0.0

        reply = AgentReply(voice_id, agent_name, text, elapsed_seconds)
        self._handle_agent_reply(
            reply,
            pipeline_start=None,
            pipeline_agent_seconds=None,
            source="voice_inbox",
            terminal_label="[Mara/inbox]",
        )
        return True

    def _process_wav(self, wav_path: Path) -> None:
        self._begin_activity()
        try:
            self._process_wav_impl(wav_path)
        finally:
            self._end_activity()

    def _process_wav_impl(self, wav_path: Path) -> None:
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

        safe_transcribed_text = redact_sensitive_text(transcribed_text)
        print(f"[You]: {safe_transcribed_text}")
        settings = self._agent_settings_for_next_route()
        if settings is None:
            emit_status(self.logger, "LISTENING", "Waiting for next capture")
            return
        self._append_event(
            "conversation",
            "USER",
            "User transcription",
            {
                "text": safe_transcribed_text,
                "transcription_seconds": round(transcribe_time, 3),
                "agent": settings.active_agent,
                "agent_name": agent_display_name(settings.active_agent),
            },
        )
        self.logger.info("User said: %s (transcription: %.2fs)", safe_transcribed_text, transcribe_time)

        if self.async_agent_replies:
            self._start_async_agent_reply(transcribed_text, pipeline_start, settings)
            return

        ssh_start = time.monotonic()
        emit_status(
            self.logger,
            "THINKING",
            f"Sending text to {agent_display_name(settings.active_agent)}",
            {"agent": settings.active_agent},
        )
        agent_reply = self._send_to_agent(transcribed_text, settings)
        ssh_time = time.monotonic() - ssh_start
        if agent_reply is None:
            emit_status(self.logger, "LISTENING", "Waiting for next capture")
            return
        self._handle_agent_reply(agent_reply, pipeline_start, ssh_time)

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
        prebuffer_chunks = []
        prebuffer_samples = 0
        base_prebuffer_seconds = self.tts_stream_prebuffer_seconds
        max_prebuffer_seconds = max(base_prebuffer_seconds, self.tts_stream_prebuffer_max_seconds)
        dynamic_prebuffer = self.tts_stream_prebuffer_dynamic
        prebuffer_target_samples = int(sample_rate * base_prebuffer_seconds)
        audio_chunk_count = 0
        sample_count = 0
        start_time = time.monotonic()
        next_status_time = self.status_interval_seconds

        def should_start_stream() -> bool:
            # Decide when enough audio is buffered to begin playback without choppy
            # underruns. In dynamic mode the cushion grows when generation runs
            # slower than real time, so longer/slower replies buffer more before
            # they start, then ride on a matching device latency. Bounded by the
            # configured base (floor) and max (ceiling) so startup never stalls.
            produced_seconds = prebuffer_samples / float(sample_rate)
            if produced_seconds <= 0:
                return False
            if not dynamic_prebuffer:
                return prebuffer_target_samples <= 0 or prebuffer_samples >= prebuffer_target_samples
            if produced_seconds < base_prebuffer_seconds:
                return False
            elapsed = max(1e-6, time.monotonic() - start_time)
            generation_ratio = produced_seconds / elapsed
            if generation_ratio >= 1.0:
                # Generation comfortably keeps up with playback; the base cushion is enough.
                return True
            target_seconds = min(max_prebuffer_seconds, base_prebuffer_seconds / max(generation_ratio, 0.1))
            return produced_seconds >= target_seconds

        def start_stream_if_needed() -> None:
            nonlocal stream, prebuffer_chunks, prebuffer_samples
            if stream is not None:
                return
            if segment_count == 1:
                print("[Streaming Mara's response...]")
            else:
                print(f"[Streaming Mara's response chunk {chunk_index}/{segment_count}...]")
            cushion_seconds = prebuffer_samples / float(sample_rate)
            if dynamic_prebuffer:
                # Size the device buffer to the cushion we actually built so a mid-stream
                # generation stall has that many seconds of audio to coast on.
                latency = max(0.15, min(max_prebuffer_seconds, max(base_prebuffer_seconds, cushion_seconds)))
            else:
                latency = "high"
            stream = sd.OutputStream(
                samplerate=sample_rate,
                channels=channels,
                dtype="float32",
                device=self.audio_device,
                latency=latency,
            )
            stream.start()
            if prebuffer_samples:
                self.logger.info(
                    "Starting %s after buffering %.2fs of audio (latency=%s)",
                    descriptor,
                    cushion_seconds,
                    f"{latency:.2f}s" if isinstance(latency, float) else latency,
                )
            for buffered_chunk in prebuffer_chunks:
                stream.write(buffered_chunk)
            prebuffer_chunks = []
            prebuffer_samples = 0

        try:
            emit_status(self.logger, "TALKING", f"Streaming {descriptor}")
            with response:
                for payload in response.iter_content(chunk_size=None):
                    audio_chunk = decoder.decode(payload)
                    if audio_chunk is None:
                        continue

                    audio_chunk_count += 1
                    sample_count += len(audio_chunk)
                    if stream is None:
                        prebuffer_chunks.append(audio_chunk)
                        prebuffer_samples += len(audio_chunk)
                        if should_start_stream():
                            start_stream_if_needed()
                    else:
                        stream.write(audio_chunk)

                    elapsed = time.monotonic() - start_time
                    if self.status_interval_seconds and elapsed >= next_status_time:
                        emit_status(
                            self.logger,
                            "TALKING",
                            f"Streaming {descriptor} ({audio_chunk_count} audio chunks, {elapsed:.0f}s elapsed)",
                        )
                        next_status_time += self.status_interval_seconds

            if stream is None and prebuffer_chunks:
                start_stream_if_needed()

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


class VoiceInboxWatcher:
    def __init__(
        self,
        inbox_path: Path,
        processor: WavCaptureProcessor,
        logger,
        poll_interval_seconds: float,
        start_at_end: bool = True,
        offset_path: Path | None = None,
    ) -> None:
        self.inbox_path = inbox_path
        self.processor = processor
        self.logger = logger
        self.poll_interval_seconds = max(0.1, poll_interval_seconds)
        self.offset_path = offset_path
        self.offset = self._initial_offset(start_at_end)

    def _initial_offset(self, start_at_end: bool) -> int:
        if self.offset_path is not None and self.offset_path.exists():
            try:
                return max(0, int(self.offset_path.read_text(encoding="utf-8").strip() or "0"))
            except (OSError, ValueError):
                return 0
        if not start_at_end or not self.inbox_path.exists():
            return 0
        try:
            with self.inbox_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(0, os.SEEK_END)
                return handle.tell()
        except OSError:
            return 0

    def _save_offset(self) -> None:
        if self.offset_path is None:
            return
        try:
            self.offset_path.parent.mkdir(parents=True, exist_ok=True)
            self.offset_path.write_text(f"{self.offset}\n", encoding="utf-8")
        except OSError as exc:
            self.logger.debug("Could not save voice inbox offset: %s", exc)

    def start(self) -> Thread:
        thread = Thread(target=self.run_forever, daemon=True)
        thread.start()
        return thread

    def poll_once(self) -> int:
        if not self.inbox_path.exists():
            return 0

        try:
            if self.inbox_path.stat().st_size < self.offset:
                self.offset = 0
            with self.inbox_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offset)
                lines = handle.readlines()
                self.offset = handle.tell()
            self._save_offset()
        except OSError as exc:
            self.logger.warning("Could not read voice inbox %s: %s", self.inbox_path, exc)
            return 0

        processed = 0
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                self.logger.warning("Ignoring invalid voice inbox JSON line: %s", exc)
                continue
            if self.processor.process_voice_inbox_payload(payload):
                processed += 1
        return processed

    def run_forever(self) -> None:
        self.logger.info(
            "Watching voice inbox %s every %.1fs",
            self.inbox_path,
            self.poll_interval_seconds,
        )
        while True:
            self.poll_once()
            time.sleep(self.poll_interval_seconds)


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
            current_paths: set[Path] = set()
            for file_path in sorted(self.capture_dir.glob("*.wav")):
                try:
                    mtime = file_path.stat().st_mtime_ns
                except FileNotFoundError:
                    continue

                file_path_normalized = file_path.resolve()
                current_paths.add(file_path_normalized)
                if self.known_mtimes.get(file_path_normalized) == mtime:
                    continue

                self.known_mtimes[file_path_normalized] = mtime
                self.processor.process_path(file_path)

            # Forget files that no longer exist so this cache cannot grow without bound
            # during long-running sessions in a busy capture directory.
            for stale_path in [path for path in self.known_mtimes if path not in current_paths]:
                self.known_mtimes.pop(stale_path, None)

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
        "--agent-session-id",
        default=DEFAULT_AGENT_SESSION_ID,
        help="Base voice session id. Per-agent ids default to <base>-hermes and <base>-openclaw.",
    )
    parser.add_argument(
        "--hermes-session-id",
        default=DEFAULT_HERMES_SESSION_ID,
        help="Stable session id sent to Hermes. Blank derives from --agent-session-id.",
    )
    parser.add_argument(
        "--openclaw-session-id",
        default=DEFAULT_OPENCLAW_SESSION_ID,
        help="Stable user/session id sent to OpenClaw. Blank derives from --agent-session-id.",
    )
    parser.add_argument(
        "--agent-session-history-path",
        default=str(DEFAULT_AGENT_SESSION_HISTORY_PATH),
        help="JSON file storing persistent per-agent conversation history.",
    )
    parser.add_argument(
        "--agent-session-history-messages",
        type=int,
        default=DEFAULT_AGENT_SESSION_HISTORY_MESSAGES,
        help="Maximum prior user/assistant messages retained per agent session.",
    )
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
    parser.add_argument(
        "--tts-stream-prebuffer-seconds",
        type=float,
        default=DEFAULT_TTS_STREAM_PREBUFFER_SECONDS,
        help="Minimum seconds of streaming audio to buffer before playback to avoid static/choppy underruns.",
    )
    parser.add_argument(
        "--tts-stream-prebuffer-max-seconds",
        type=float,
        default=DEFAULT_TTS_STREAM_PREBUFFER_MAX_SECONDS,
        help="Maximum prebuffer/cushion seconds when dynamic prebuffering grows the buffer for slow generation.",
    )
    prebuffer_group = parser.add_mutually_exclusive_group()
    prebuffer_group.add_argument(
        "--tts-stream-prebuffer-dynamic",
        dest="tts_stream_prebuffer_dynamic",
        action="store_true",
        default=DEFAULT_TTS_STREAM_PREBUFFER_DYNAMIC,
        help="Grow the prebuffer/cushion automatically based on measured generation speed.",
    )
    prebuffer_group.add_argument(
        "--no-tts-stream-prebuffer-dynamic",
        dest="tts_stream_prebuffer_dynamic",
        action="store_false",
        help="Use a fixed prebuffer of --tts-stream-prebuffer-seconds instead of adapting it.",
    )
    parser.add_argument(
        "--process-existing-seconds",
        type=float,
        default=DEFAULT_PROCESS_EXISTING_SECONDS,
        help="On startup, process existing Voicebox WAV files modified within this many seconds. Use 0 to disable.",
    )
    ack_group = parser.add_mutually_exclusive_group()
    ack_group.add_argument(
        "--async-ack-agent",
        dest="async_ack_agent",
        action="store_true",
        default=DEFAULT_ASYNC_ACK_AGENT,
        help="Speak a natural, agent-generated acknowledgement.",
    )
    ack_group.add_argument(
        "--no-async-ack-agent",
        dest="async_ack_agent",
        action="store_false",
        help="Use preset acknowledgement phrases instead of an agent-generated one.",
    )
    parser.add_argument(
        "--async-ack-grace-seconds",
        type=float,
        default=DEFAULT_ASYNC_ACK_GRACE_SECONDS,
        help="Seconds to wait for the real answer before speaking an acknowledgement. If the answer arrives first, no acknowledgement is spoken. 0 always speaks one immediately.",
    )
    parser.add_argument(
        "--async-ack-text",
        default=DEFAULT_ASYNC_ACK_TEXT,
        help="Single acknowledgement phrase used when no --async-ack-phrases are set, and as the final fallback.",
    )
    parser.add_argument(
        "--async-ack-phrases",
        default=DEFAULT_ASYNC_ACK_PHRASES,
        help="Newline-separated acknowledgement phrases chosen at random when the agent acknowledgement is off (or as fallback).",
    )
    parser.add_argument(
        "--async-followup-initial-delay-seconds",
        type=float,
        default=DEFAULT_ASYNC_FOLLOWUP_INITIAL_DELAY_SECONDS,
        help="Seconds to wait before the first automatic follow-up check after a deferred async reply.",
    )
    parser.add_argument(
        "--async-followup-poll-seconds",
        type=float,
        default=DEFAULT_ASYNC_FOLLOWUP_POLL_SECONDS,
        help="Seconds between automatic background-result follow-up checks.",
    )
    parser.add_argument(
        "--async-followup-max-attempts",
        type=int,
        default=DEFAULT_ASYNC_FOLLOWUP_MAX_ATTEMPTS,
        help="Maximum automatic follow-up checks for a deferred async reply.",
    )
    parser.add_argument(
        "--voice-inbox-path",
        default=str(DEFAULT_VOICE_INBOX_PATH),
        help="JSONL file polled for out-of-band voice replies. Each line should contain text or message.",
    )
    parser.add_argument(
        "--voice-inbox-poll-seconds",
        type=float,
        default=DEFAULT_VOICE_INBOX_POLL_SECONDS,
        help="Seconds between voice inbox polls. Use 0 to disable voice inbox polling.",
    )
    async_group = parser.add_mutually_exclusive_group()
    async_group.add_argument(
        "--async-agent-replies",
        dest="async_agent_replies",
        action="store_true",
        default=DEFAULT_ASYNC_AGENT_REPLIES,
        help="Speak a short acknowledgement immediately, then finish the agent request in the background.",
    )
    async_group.add_argument(
        "--no-async-agent-replies",
        dest="async_agent_replies",
        action="store_false",
        help="Wait for the agent reply before speaking, preserving the original synchronous flow.",
    )
    followup_group = parser.add_mutually_exclusive_group()
    followup_group.add_argument(
        "--async-followup",
        dest="async_followup_enabled",
        action="store_true",
        default=DEFAULT_ASYNC_FOLLOWUP_ENABLED,
        help="Automatically poll the same agent session if the async reply says background work is still running.",
    )
    followup_group.add_argument(
        "--no-async-followup",
        dest="async_followup_enabled",
        action="store_false",
        help="Do not automatically poll for final background results.",
    )
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument(
        "--agent-session-persistence",
        dest="agent_session_persistence",
        action="store_true",
        default=DEFAULT_AGENT_SESSION_PERSISTENCE,
        help="Persist and reuse per-agent conversation history across turns.",
    )
    session_group.add_argument(
        "--no-agent-session-persistence",
        dest="agent_session_persistence",
        action="store_false",
        help="Disable persistent per-agent conversation history.",
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
    logger.info(
        "Agent session: base=%s hermes=%s openclaw=%s persistence=%s history=%s messages=%d",
        args.agent_session_id,
        args.hermes_session_id or f"{args.agent_session_id}-hermes",
        args.openclaw_session_id or f"{args.agent_session_id}-openclaw",
        "enabled" if args.agent_session_persistence else "disabled",
        args.agent_session_history_path,
        args.agent_session_history_messages,
    )
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
    logger.info(
        "TTS streaming prebuffer: base=%.2fs max=%.2fs dynamic=%s",
        args.tts_stream_prebuffer_seconds,
        args.tts_stream_prebuffer_max_seconds,
        "enabled" if args.tts_stream_prebuffer_dynamic else "disabled",
    )
    logger.info("TTS streaming: %s", "enabled" if args.tts_streaming else "disabled")
    logger.info("Process existing captures window: %.1fs", args.process_existing_seconds)
    ack_phrase_count = len(WavCaptureProcessor._parse_ack_phrases(args.async_ack_phrases))
    logger.info(
        "Async agent replies: %s (ack: %s, preset phrases: %d, grace: %.1fs)",
        "enabled" if args.async_agent_replies else "disabled",
        "agent-generated" if args.async_ack_agent else "preset",
        ack_phrase_count,
        args.async_ack_grace_seconds,
    )
    logger.info(
        "Async follow-up: %s (initial_delay=%.1fs poll=%.1fs max_attempts=%d)",
        "enabled" if args.async_followup_enabled else "disabled",
        args.async_followup_initial_delay_seconds,
        args.async_followup_poll_seconds,
        args.async_followup_max_attempts,
    )
    if args.voice_inbox_poll_seconds > 0 and args.voice_inbox_path:
        logger.info(
            "Voice inbox: %s (poll %.1fs)",
            args.voice_inbox_path,
            args.voice_inbox_poll_seconds,
        )
    else:
        logger.info("Voice inbox: disabled")
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
    startup_backlog_newer_than = latest_event_timestamp_seconds()
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
        tts_stream_prebuffer_seconds=args.tts_stream_prebuffer_seconds,
        tts_stream_prebuffer_dynamic=args.tts_stream_prebuffer_dynamic,
        tts_stream_prebuffer_max_seconds=args.tts_stream_prebuffer_max_seconds,
        async_agent_replies=args.async_agent_replies,
        async_ack_agent=args.async_ack_agent,
        async_ack_grace_seconds=args.async_ack_grace_seconds,
        async_ack_text=args.async_ack_text,
        async_ack_phrases=args.async_ack_phrases,
        async_followup_enabled=args.async_followup_enabled,
        async_followup_initial_delay_seconds=args.async_followup_initial_delay_seconds,
        async_followup_poll_seconds=args.async_followup_poll_seconds,
        async_followup_max_attempts=args.async_followup_max_attempts,
        agent_session_id=args.agent_session_id,
        hermes_session_id=args.hermes_session_id,
        openclaw_session_id=args.openclaw_session_id,
        agent_session_history_path=Path(args.agent_session_history_path),
        agent_session_history_messages=args.agent_session_history_messages,
        agent_session_persistence=args.agent_session_persistence,
    )

    existing_captures = find_existing_capture_files(
        capture_dir,
        args.process_existing_seconds,
        newer_than_seconds=startup_backlog_newer_than,
    )
    if existing_captures:
        logger.info(
            "Processing %d existing recent Voicebox capture(s) from startup backlog",
            len(existing_captures),
        )
        emit_status(
            logger,
            "THINKING",
            f"Processing {len(existing_captures)} recent capture(s) from downtime",
        )
        for file_path in existing_captures:
            processor.process_path(file_path)
        emit_status(logger, "LISTENING", "Ready")

    if args.voice_inbox_poll_seconds > 0 and args.voice_inbox_path:
        voice_inbox_path = Path(args.voice_inbox_path)
        VoiceInboxWatcher(
            voice_inbox_path,
            processor,
            logger,
            args.voice_inbox_poll_seconds,
            offset_path=voice_inbox_path.with_name(f"{voice_inbox_path.name}.offset"),
        ).start()

    processor.start_heartbeat()

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
            processor.stop_heartbeat()
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
        processor.stop_heartbeat()
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
