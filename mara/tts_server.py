from __future__ import annotations

import argparse
import io
import json
import os
import random
import struct
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from mara.streaming import (
    STREAM_CHANNELS_HEADER,
    STREAM_DTYPE,
    STREAM_DTYPE_HEADER,
    STREAM_MEDIA_TYPE,
    STREAM_SAMPLE_RATE_HEADER,
    audio_to_pcm_f32le,
)
from mara.pipeline import BASE_DIR, configure_logging

try:
    from voxcpm import VoxCPM

    VOXCPM_IMPORT_ERROR = None
except Exception as exc:
    VoxCPM = None
    VOXCPM_IMPORT_ERROR = exc


DEFAULT_MODEL_PATH = Path(
    os.getenv("MARA_MODEL_PATH", str(BASE_DIR / "models" / "VoxCPM2"))
)
DEFAULT_VOICE_STYLE = os.getenv(
    "MARA_VOICE_STYLE",
    "A clear adult female speaking voice with a warm lower tone, calm conversational pace, natural articulation, and no humming, sighing, singing, or nonverbal sounds",
)
VOICE_REFERENCE_STYLE_VERSION = "clear_speech_v2"
DEFAULT_LOCK_VOICE_STYLE = os.getenv("MARA_LOCK_VOICE_STYLE", "1") != "0"
DEFAULT_TTS_SEED = int(os.getenv("MARA_TTS_SEED", "4242"))
DEFAULT_DEVICE = os.getenv("MARA_TTS_DEVICE", "cuda")
DEFAULT_OPTIMIZE = os.getenv("MARA_TTS_OPTIMIZE", "1") != "0"
DEFAULT_INFERENCE_TIMESTEPS = int(os.getenv("MARA_TTS_INFERENCE_TIMESTEPS", "4"))
DEFAULT_USE_VOICE_REFERENCE = os.getenv("MARA_USE_VOICE_REFERENCE", "1") != "0"
DEFAULT_AUTO_CREATE_VOICE_REFERENCE = os.getenv("MARA_AUTO_CREATE_VOICE_REFERENCE", "1") != "0"
DEFAULT_REGENERATE_VOICE_REFERENCE = os.getenv("MARA_REGENERATE_VOICE_REFERENCE", "0") == "1"
DEFAULT_VOICE_REFERENCE_PATH = Path(
    os.getenv(
        "MARA_VOICE_REFERENCE_PATH",
        str(BASE_DIR / "models" / "mara_voice_reference.wav"),
    )
)
DEFAULT_VOICE_REFERENCE_TEXT = os.getenv(
    "MARA_VOICE_REFERENCE_TEXT",
    "Mara is online, calm, warm, and ready to answer clearly.",
)
DEFAULT_OPENCLAW_VOICE_STYLE = os.getenv(
    "MARA_OPENCLAW_VOICE_STYLE",
    "A clear adult male speaking voice with a steady lower register, calm conversational pace, natural articulation, and no humming, sighing, singing, or nonverbal sounds",
)
DEFAULT_OPENCLAW_VOICE_REFERENCE_PATH = Path(
    os.getenv(
        "MARA_OPENCLAW_VOICE_REFERENCE_PATH",
        str(BASE_DIR / "models" / "openclaw_voice_reference.wav"),
    )
)
DEFAULT_OPENCLAW_VOICE_REFERENCE_TEXT = os.getenv(
    "MARA_OPENCLAW_VOICE_REFERENCE_TEXT",
    "OpenClaw is online, steady, direct, and ready to answer clearly.",
)
VOICE_GENERATION_MODE = "persistent_reference_only_v2"

# --- VoiceBox backend ---
TTS_BACKEND = os.getenv("MARA_TTS_BACKEND", "voxcpm2")  # "voxcpm2" | "voicebox"
VOICEBOX_URL = os.getenv("MARA_VOICEBOX_URL", "http://127.0.0.1:17493")
VOICEBOX_PROFILE_IDS: dict[str, str] = {
    "hermes": os.getenv("MARA_HERMES_VOICEBOX_PROFILE_ID", ""),
    "openclaw": os.getenv("MARA_OPENCLAW_VOICEBOX_PROFILE_ID", ""),
}

logger = configure_logging(os.getenv("MARA_LOG_LEVEL", "INFO")).getChild("tts_server")


def _voicebox_request(method: str, path: str, body: bytes | None = None, timeout: float = 10.0) -> bytes:
    req = urllib.request.Request(
        f"{VOICEBOX_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _voicebox_healthy() -> dict[str, object]:
    try:
        data = _voicebox_request("GET", "/health", timeout=5.0)
        return json.loads(data)
    except Exception as exc:
        return {"status": "unreachable", "error": str(exc)}


def _voicebox_list_profiles() -> list[dict]:
    try:
        data = _voicebox_request("GET", "/profiles", timeout=5.0)
        return json.loads(data)
    except Exception as exc:
        logger.warning("Could not list VoiceBox profiles: %s", exc)
        return []


def _voicebox_generate(profile_id: str, text: str, timeout: float = 300.0) -> bytes:
    body = json.dumps({"profile_id": profile_id, "text": text}).encode()
    return _voicebox_request("POST", "/generate/stream", body=body, timeout=timeout)


def _parse_wav_header(data: bytes) -> tuple[int, int, int, int]:
    """Parse a WAV/RIFF header from a byte buffer.

    Returns (sample_rate, channels, bits_per_sample, data_start_offset).
    Raises ValueError if the buffer doesn't contain a recognisable WAV header
    with a data chunk.
    """
    if len(data) < 12 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        raise ValueError("Not a valid RIFF/WAVE file")
    offset = 12
    sample_rate = channels = bits_per_sample = 0
    while offset + 8 <= len(data):
        chunk_id = data[offset:offset + 4]
        chunk_size = struct.unpack_from('<I', data, offset + 4)[0]
        if chunk_id == b'fmt ' and chunk_size >= 16:
            channels = struct.unpack_from('<H', data, offset + 10)[0]
            sample_rate = struct.unpack_from('<I', data, offset + 12)[0]
            bits_per_sample = struct.unpack_from('<H', data, offset + 22)[0]
        elif chunk_id == b'data':
            return sample_rate, channels, bits_per_sample, offset + 8
        offset += 8 + chunk_size
    raise ValueError("WAV 'data' chunk not found in buffered header bytes")


def _wav_samples_to_f32le(raw: bytes, bits_per_sample: int, pending: bytearray) -> bytes:
    """Convert a chunk of raw WAV sample bytes to float32-LE PCM bytes.

    *pending* is a bytearray that carries over sub-frame-aligned leftover bytes
    between calls so the caller never loses partial samples.
    """
    bytes_per_sample = max(1, bits_per_sample // 8)
    data = bytes(pending) + raw
    usable = (len(data) // bytes_per_sample) * bytes_per_sample
    pending.clear()
    pending.extend(data[usable:])
    if not usable:
        return b""
    chunk = data[:usable]
    if bits_per_sample == 32:
        arr = np.frombuffer(chunk, dtype="<f4")
    elif bits_per_sample == 16:
        arr = np.frombuffer(chunk, dtype="<i2").astype(np.float32) / 32768.0
    else:
        raise ValueError(f"Unsupported bits_per_sample={bits_per_sample} from VoiceBox")
    return np.clip(arr, -1.0, 1.0).astype("<f4").tobytes()


def _voicebox_open_pcm_stream(
    profile_id: str, text: str, timeout: float = 300.0
) -> tuple[int, int, int, bytes, object]:
    """Open a streaming HTTP connection to VoiceBox /generate/stream.

    Reads just enough bytes to parse the WAV header (sample_rate, channels,
    bits_per_sample) so those can be returned in the HTTP response headers
    before audio data starts flowing.

    Returns (sample_rate, channels, bits_per_sample, leftover_pcm_bytes, response).
    The caller MUST close *response* when done.
    """
    import requests as _req

    resp = _req.post(
        f"{VOICEBOX_URL}/generate/stream",
        json={"profile_id": profile_id, "text": text},
        timeout=timeout,
        stream=True,
    )
    resp.raise_for_status()

    header_buf = bytearray()
    for raw_chunk in resp.iter_content(chunk_size=None):
        if raw_chunk:
            header_buf.extend(raw_chunk)
        try:
            sample_rate, channels, bits_per_sample, data_offset = _parse_wav_header(bytes(header_buf))
            return sample_rate, channels, bits_per_sample, bytes(header_buf[data_offset:]), resp
        except ValueError:
            if len(header_buf) > 8192:
                resp.close()
                raise ValueError(
                    f"Could not parse WAV header from first {len(header_buf)} bytes of VoiceBox response"
                )

    resp.close()
    raise ValueError("VoiceBox stream ended before a complete WAV header was received")


MODEL = None
MODEL_ERROR: str | None = None
MODEL_PATH = DEFAULT_MODEL_PATH
MODEL_DEVICE = DEFAULT_DEVICE
MODEL_EFFECTIVE_DEVICE: str | None = None
MODEL_OPTIMIZE = DEFAULT_OPTIMIZE
MODEL_INFERENCE_TIMESTEPS = DEFAULT_INFERENCE_TIMESTEPS
# A plain (non-reentrant) Lock, NOT an RLock: the streaming endpoint holds this across
# the response generator's yields, and Starlette resumes that generator on different
# threadpool threads. An RLock is thread-owned and raises "cannot release un-acquired
# lock" when released from another thread; a plain Lock can be released by any thread.
MODEL_LOCK = Lock()
VOICE_REFERENCE_ENABLED = DEFAULT_USE_VOICE_REFERENCE
VOICE_REFERENCE_AUTO_CREATE = DEFAULT_AUTO_CREATE_VOICE_REFERENCE
VOICE_REFERENCE_REGENERATE = DEFAULT_REGENERATE_VOICE_REFERENCE
VOICE_REFERENCE_PATH = DEFAULT_VOICE_REFERENCE_PATH
VOICE_REFERENCE_TEXT = DEFAULT_VOICE_REFERENCE_TEXT
VOICE_PROMPT_CACHE = None
VOICE_PROMPT_CACHES: dict[str, object] = {}
VOICE_REFERENCE_ERROR: str | None = None
VOICE_REFERENCE_ERRORS: dict[str, str | None] = {}
_REGENERATED_VOICE_IDS: set[str] = set()

try:
    import torch

    TORCH_AVAILABLE = True
except Exception:
    torch = None
    TORCH_AVAILABLE = False


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=12000)
    voice_style: str | None = Field(default=None, max_length=500)
    voice_id: str | None = Field(default=None, max_length=64)


@dataclass(slots=True)
class VoiceProfile:
    voice_id: str
    voice_style: str
    reference_path: Path
    reference_text: str


def normalize_voice_id(voice_id: str | None) -> str:
    normalized = (voice_id or "hermes").strip().lower()
    if normalized in ("mara", "default", "hermes"):
        return "hermes"
    if normalized == "openclaw":
        return "openclaw"
    return "hermes"


def get_voice_profile(voice_id: str | None) -> VoiceProfile:
    normalized = normalize_voice_id(voice_id)
    if normalized == "openclaw":
        return VoiceProfile(
            voice_id="openclaw",
            voice_style=DEFAULT_OPENCLAW_VOICE_STYLE,
            reference_path=DEFAULT_OPENCLAW_VOICE_REFERENCE_PATH,
            reference_text=" ".join(DEFAULT_OPENCLAW_VOICE_REFERENCE_TEXT.split()),
        )
    return VoiceProfile(
        voice_id="hermes",
        voice_style=DEFAULT_VOICE_STYLE,
        reference_path=VOICE_REFERENCE_PATH,
        reference_text=VOICE_REFERENCE_TEXT,
    )


def set_model_path(model_path: Path) -> None:
    global MODEL_PATH
    MODEL_PATH = model_path


def set_runtime_options(device: str, optimize: bool) -> None:
    global MODEL_DEVICE, MODEL_OPTIMIZE
    MODEL_DEVICE = device
    MODEL_OPTIMIZE = optimize


def set_generation_options(inference_timesteps: int) -> None:
    global MODEL_INFERENCE_TIMESTEPS
    MODEL_INFERENCE_TIMESTEPS = max(1, inference_timesteps)


def set_voice_reference_options(
    reference_path: Path,
    reference_text: str,
    enabled: bool,
    regenerate: bool,
) -> None:
    global VOICE_REFERENCE_PATH, VOICE_REFERENCE_TEXT, VOICE_REFERENCE_ENABLED, VOICE_REFERENCE_REGENERATE
    VOICE_REFERENCE_PATH = reference_path
    VOICE_REFERENCE_TEXT = " ".join(reference_text.split())
    VOICE_REFERENCE_ENABLED = enabled
    VOICE_REFERENCE_REGENERATE = regenerate


def set_voice_style_options(
    hermes_voice_style: str | None = None,
    openclaw_voice_style: str | None = None,
) -> None:
    global DEFAULT_VOICE_STYLE, DEFAULT_OPENCLAW_VOICE_STYLE
    if hermes_voice_style and hermes_voice_style.strip():
        DEFAULT_VOICE_STYLE = hermes_voice_style.strip()
    if openclaw_voice_style and openclaw_voice_style.strip():
        DEFAULT_OPENCLAW_VOICE_STYLE = openclaw_voice_style.strip()


def seed_generation() -> None:
    random.seed(DEFAULT_TTS_SEED)
    np.random.seed(DEFAULT_TTS_SEED)
    if TORCH_AVAILABLE:
        torch.manual_seed(DEFAULT_TTS_SEED)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(DEFAULT_TTS_SEED)


def cuda_available() -> bool:
    return bool(TORCH_AVAILABLE and torch is not None and torch.cuda.is_available())


def resolve_device(requested: str | None) -> str:
    """Resolve the configured device to a concrete one, forcing the GPU when present.

    "auto" (and blank) become "cuda" whenever CUDA is available. If a CUDA device is
    requested but unavailable we fall back to CPU so the server still runs, but we log
    loudly because CPU inference is dramatically slower.
    """
    normalized = (requested or "").strip().lower()
    if normalized in ("", "auto"):
        if cuda_available():
            return "cuda"
        logger.warning(
            "TTS device is 'auto' but CUDA is not available; running on CPU, which is very slow. "
            "Install a CUDA-enabled PyTorch build and confirm the GPU is visible to use it."
        )
        return "cpu"
    if normalized.startswith("cuda"):
        if cuda_available():
            return requested  # preserve an explicit index like cuda:0
        logger.error(
            "TTS device '%s' was requested but CUDA is not available; falling back to CPU (very slow). "
            "Check that a CUDA-enabled PyTorch is installed and the GPU is visible (nvidia-smi).",
            requested,
        )
        return "cpu"
    return requested


def ensure_model_loaded():
    global MODEL, MODEL_ERROR, MODEL_EFFECTIVE_DEVICE

    with MODEL_LOCK:
        if MODEL is not None:
            return MODEL

        if VOXCPM_IMPORT_ERROR is not None:
            MODEL_ERROR = (
                "voxcpm could not be imported. Check the active Python environment and PyTorch/Torchaudio compatibility. "
                f"Original error: {VOXCPM_IMPORT_ERROR}"
            )
            raise RuntimeError(MODEL_ERROR) from VOXCPM_IMPORT_ERROR

        if not MODEL_PATH.exists():
            MODEL_ERROR = f"Model directory was not found at {MODEL_PATH}"
            raise RuntimeError(MODEL_ERROR)

        effective_device = resolve_device(MODEL_DEVICE)
        MODEL_EFFECTIVE_DEVICE = effective_device
        logger.info(
            "Loading VoxCPM2 from %s with device=%s (effective=%s) optimize=%s",
            MODEL_PATH,
            MODEL_DEVICE,
            effective_device,
            MODEL_OPTIMIZE,
        )
        start_time = time.perf_counter()
        try:
            MODEL = VoxCPM.from_pretrained(
                str(MODEL_PATH),
                load_denoiser=False,
                device=effective_device,
                optimize=MODEL_OPTIMIZE,
            )
        except Exception as exc:
            MODEL_ERROR = str(exc)
            logger.exception("Failed to load VoxCPM2")
            raise RuntimeError(MODEL_ERROR) from exc

        MODEL_ERROR = None
        logger.info("VoxCPM2 is ready after %.2fs", time.perf_counter() - start_time)
        return MODEL


def ensure_voice_reference_file(model, profile: VoiceProfile, regenerate: bool) -> None:
    if not VOICE_REFERENCE_ENABLED:
        return

    metadata_path = profile.reference_path.with_suffix(f"{profile.reference_path.suffix}.json")
    if profile.reference_path.exists() and metadata_path.exists() and not regenerate:
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            metadata = {}

        if (
            metadata.get("style_version") == VOICE_REFERENCE_STYLE_VERSION
            and metadata.get("voice_id") == profile.voice_id
            and metadata.get("voice_style") == profile.voice_style
            and metadata.get("reference_text") == profile.reference_text
        ):
            return

        logger.info("Persistent voice reference metadata changed; regenerating %s", profile.reference_path)

    elif profile.reference_path.exists() and not regenerate:
        logger.info("Persistent voice reference metadata is missing; regenerating %s", profile.reference_path)

    if profile.reference_path.exists() and not regenerate and not VOICE_REFERENCE_AUTO_CREATE:
        return

    if not VOICE_REFERENCE_AUTO_CREATE:
        raise RuntimeError(f"Voice reference file does not exist: {profile.reference_path}")

    profile.reference_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Creating persistent %s voice reference at %s", profile.voice_id, profile.reference_path)
    reference_prompt = f"({profile.voice_style}) {profile.reference_text}"
    seed_generation()
    wav = model.generate(
        text=reference_prompt,
        cfg_value=2.0,
        inference_timesteps=MODEL_INFERENCE_TIMESTEPS,
        retry_badcase=False,
    )
    sf.write(profile.reference_path, wav, model.tts_model.sample_rate, format="WAV")
    metadata_path.write_text(
        json.dumps(
            {
                "style_version": VOICE_REFERENCE_STYLE_VERSION,
                "voice_id": profile.voice_id,
                "voice_style": profile.voice_style,
                "reference_text": profile.reference_text,
                "sample_rate": model.tts_model.sample_rate,
                "inference_timesteps": MODEL_INFERENCE_TIMESTEPS,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("Persistent %s voice reference created: %s", profile.voice_id, profile.reference_path)


def ensure_voice_prompt_cache(model, voice_id: str | None = None):
    global VOICE_PROMPT_CACHE, VOICE_REFERENCE_ERROR

    if not VOICE_REFERENCE_ENABLED:
        VOICE_REFERENCE_ERROR = None
        return None

    profile = get_voice_profile(voice_id)
    # Honor a regeneration request once per voice id. OpenClaw's cache is built
    # lazily on its first request, after Hermes was already regenerated at startup,
    # so a single global flag would skip every profile but the first.
    regenerate = VOICE_REFERENCE_REGENERATE and profile.voice_id not in _REGENERATED_VOICE_IDS
    prompt_cache = VOICE_PROMPT_CACHES.get(profile.voice_id)
    if prompt_cache is not None and not regenerate:
        return prompt_cache

    try:
        ensure_voice_reference_file(model, profile, regenerate)
        prompt_cache = model.tts_model.build_prompt_cache(
            reference_wav_path=str(profile.reference_path),
        )
        VOICE_PROMPT_CACHES[profile.voice_id] = prompt_cache
        if profile.voice_id == "hermes":
            VOICE_PROMPT_CACHE = prompt_cache
    except Exception as exc:
        VOICE_REFERENCE_ERROR = str(exc)
        VOICE_REFERENCE_ERRORS[profile.voice_id] = VOICE_REFERENCE_ERROR
        logger.exception("Failed to prepare persistent %s voice reference", profile.voice_id)
        raise RuntimeError(VOICE_REFERENCE_ERROR) from exc

    VOICE_REFERENCE_ERROR = None
    VOICE_REFERENCE_ERRORS[profile.voice_id] = None
    if regenerate:
        _REGENERATED_VOICE_IDS.add(profile.voice_id)
    logger.info("Persistent %s voice reference cache is ready: %s", profile.voice_id, profile.reference_path)
    return prompt_cache


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if TTS_BACKEND == "voicebox":
        vb_health = _voicebox_healthy()
        if vb_health.get("status") == "healthy":
            logger.info("VoiceBox backend is available at %s (model_loaded=%s)", VOICEBOX_URL, vb_health.get("model_loaded"))
        else:
            logger.warning("VoiceBox backend at %s is not healthy at startup: %s", VOICEBOX_URL, vb_health.get("error") or vb_health.get("status"))
    else:
        try:
            model = ensure_model_loaded()
            with MODEL_LOCK:
                ensure_voice_prompt_cache(model)
        except Exception:
            logger.warning(
                "TTS server is running in degraded mode. The health endpoint will report the load error."
            )
    yield


app = FastAPI(title="Mara TTS Server", lifespan=lifespan)


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "mara_tts_server", "health": "/healthz", "tts": "/tts", "tts_stream": "/tts/stream"}


@app.get("/healthz")
def healthz() -> dict[str, object]:
    if TTS_BACKEND == "voicebox":
        vb_health = _voicebox_healthy()
        vb_ok = vb_health.get("status") == "healthy"
        hermes_id = VOICEBOX_PROFILE_IDS.get("hermes", "")
        openclaw_id = VOICEBOX_PROFILE_IDS.get("openclaw", "")
        profiles_configured = bool(hermes_id) and bool(openclaw_id)
        return {
            "status": "ok" if vb_ok and profiles_configured else "degraded",
            "backend": "voicebox",
            "voicebox_url": VOICEBOX_URL,
            "voicebox_health": vb_health,
            "voicebox_profiles": {
                vid: {"profile_id": pid, "configured": bool(pid)}
                for vid, pid in VOICEBOX_PROFILE_IDS.items()
            },
            "streaming_supported": True,
            "stream_pcm_sanitized": True,
            "stream_media_type": STREAM_MEDIA_TYPE,
            "error": None if vb_ok else (vb_health.get("error") or vb_health.get("status")),
        }
    profiles = {
        profile.voice_id: {
            "voice_style": profile.voice_style,
            "voice_reference_path": str(profile.reference_path),
            "voice_reference_text": profile.reference_text,
            "voice_reference_cached": profile.voice_id in VOICE_PROMPT_CACHES,
            "voice_reference_error": VOICE_REFERENCE_ERRORS.get(profile.voice_id),
        }
        for profile in (get_voice_profile("hermes"), get_voice_profile("openclaw"))
    }
    stable_voice_ready = (not VOICE_REFERENCE_ENABLED) or (
        VOICE_PROMPT_CACHE is not None and VOICE_REFERENCE_ERROR is None
    )
    return {
        "status": "ok" if MODEL is not None and MODEL_ERROR is None and stable_voice_ready else "degraded",
        "backend": "voxcpm2",
        "model_loaded": MODEL is not None,
        "model_path": str(MODEL_PATH),
        "device": MODEL_DEVICE,
        "effective_device": MODEL_EFFECTIVE_DEVICE,
        "cuda_available": cuda_available(),
        "optimize": MODEL_OPTIMIZE,
        "inference_timesteps": MODEL_INFERENCE_TIMESTEPS,
        "error": MODEL_ERROR,
        "voice_reference_enabled": VOICE_REFERENCE_ENABLED,
        "voice_reference_ready": stable_voice_ready,
        "voice_reference_path": str(VOICE_REFERENCE_PATH),
        "voice_reference_text": VOICE_REFERENCE_TEXT,
        "voice_reference_error": VOICE_REFERENCE_ERROR,
        "voice_profiles": profiles,
        "voice_generation_mode": VOICE_GENERATION_MODE if VOICE_REFERENCE_ENABLED else "voice_design",
        "streaming_supported": True,
        "stream_pcm_sanitized": True,
        "stream_media_type": STREAM_MEDIA_TYPE,
    }


class VoiceStyleUpdate(BaseModel):
    hermes_voice_style: str | None = Field(default=None, max_length=1000)
    openclaw_voice_style: str | None = Field(default=None, max_length=1000)


@app.post("/voice-style")
def update_voice_style(req: VoiceStyleUpdate) -> dict[str, object]:
    global DEFAULT_VOICE_STYLE, DEFAULT_OPENCLAW_VOICE_STYLE
    updated = []
    with MODEL_LOCK:
        if req.hermes_voice_style and req.hermes_voice_style.strip():
            DEFAULT_VOICE_STYLE = req.hermes_voice_style.strip()
            VOICE_PROMPT_CACHES.pop("hermes", None)
            _REGENERATED_VOICE_IDS.discard("hermes")
            VOICE_REFERENCE_ERRORS.pop("hermes", None)
            updated.append("hermes")
        if req.openclaw_voice_style and req.openclaw_voice_style.strip():
            DEFAULT_OPENCLAW_VOICE_STYLE = req.openclaw_voice_style.strip()
            VOICE_PROMPT_CACHES.pop("openclaw", None)
            _REGENERATED_VOICE_IDS.discard("openclaw")
            VOICE_REFERENCE_ERRORS.pop("openclaw", None)
            updated.append("openclaw")
    logger.info("Voice styles updated for: %s", updated)
    return {
        "updated": updated,
        "hermes_voice_style": DEFAULT_VOICE_STYLE,
        "openclaw_voice_style": DEFAULT_OPENCLAW_VOICE_STYLE,
    }


class VoiceboxProfileMapUpdate(BaseModel):
    hermes_profile_id: str | None = Field(default=None, max_length=128)
    openclaw_profile_id: str | None = Field(default=None, max_length=128)


@app.get("/voicebox/profiles")
def voicebox_profiles() -> list:
    return _voicebox_list_profiles()


@app.post("/voicebox/profile-map")
def update_voicebox_profile_map(req: VoiceboxProfileMapUpdate) -> dict[str, object]:
    updated = []
    if req.hermes_profile_id is not None:
        VOICEBOX_PROFILE_IDS["hermes"] = req.hermes_profile_id.strip()
        updated.append("hermes")
    if req.openclaw_profile_id is not None:
        VOICEBOX_PROFILE_IDS["openclaw"] = req.openclaw_profile_id.strip()
        updated.append("openclaw")
    logger.info("VoiceBox profile map updated for: %s", updated)
    return {"updated": updated, "profile_ids": dict(VOICEBOX_PROFILE_IDS)}


@app.post("/tts")
def generate_tts(req: TTSRequest) -> Response:
    normalized_text = " ".join(req.text.split())
    if not normalized_text:
        raise HTTPException(status_code=400, detail="Text is empty after normalization.")

    if TTS_BACKEND == "voicebox":
        voice_id = normalize_voice_id(req.voice_id)
        profile_id = VOICEBOX_PROFILE_IDS.get(voice_id, "")
        if not profile_id:
            raise HTTPException(
                status_code=503,
                detail=f"No VoiceBox profile configured for voice_id={voice_id!r}. Set one in GUI Settings > Voice > VoiceBox Voices.",
            )
        start_time = time.perf_counter()
        try:
            wav_bytes = _voicebox_generate(profile_id, normalized_text)
        except Exception as exc:
            logger.exception("VoiceBox /tts generation failed")
            raise HTTPException(status_code=500, detail=f"VoiceBox generation failed: {exc}") from exc
        logger.info(
            "VoiceBox generated %d bytes in %.2fs for %d characters (voice_id=%s, profile_id=%s)",
            len(wav_bytes), time.perf_counter() - start_time, len(normalized_text), voice_id, profile_id,
        )
        return Response(content=wav_bytes, media_type="audio/wav")

    try:
        model = ensure_model_loaded()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"TTS unavailable: {exc}") from exc

    profile = get_voice_profile(req.voice_id)
    if DEFAULT_LOCK_VOICE_STYLE:
        voice_style = profile.voice_style
    else:
        voice_style = (req.voice_style or profile.voice_style).strip()
    voice_prompt = f"({voice_style}) {normalized_text}"

    start_time = time.perf_counter()
    voice_mode = "voice_design"
    try:
        with MODEL_LOCK:
            prompt_cache = ensure_voice_prompt_cache(model, profile.voice_id)
            seed_generation()
            if prompt_cache is None:
                wav = model.generate(
                    text=voice_prompt,
                    cfg_value=2.0,
                    inference_timesteps=MODEL_INFERENCE_TIMESTEPS,
                )
            else:
                wav_tensor, _, _ = model.tts_model.generate_with_prompt_cache(
                    target_text=normalized_text,
                    prompt_cache=prompt_cache,
                    cfg_value=2.0,
                    inference_timesteps=MODEL_INFERENCE_TIMESTEPS,
                    retry_badcase=True,
                )
                wav = wav_tensor.squeeze(0).cpu().numpy()
                voice_mode = VOICE_GENERATION_MODE
    except Exception as exc:
        logger.exception("VoxCPM2 generation failed")
        raise HTTPException(status_code=500, detail=f"TTS generation failed: {exc}") from exc

    buffer = io.BytesIO()
    sf.write(buffer, wav, model.tts_model.sample_rate, format="WAV")
    audio_bytes = buffer.getvalue()

    logger.info(
        "Generated %d bytes of audio in %.2fs for %d characters (voice_id=%s, seed=%d, locked_style=%s, voice_mode=%s, inference_timesteps=%d)",
        len(audio_bytes),
        time.perf_counter() - start_time,
        len(normalized_text),
        profile.voice_id,
        DEFAULT_TTS_SEED,
        DEFAULT_LOCK_VOICE_STYLE,
        voice_mode,
        MODEL_INFERENCE_TIMESTEPS,
    )
    return Response(content=audio_bytes, media_type="audio/wav")


@app.post("/tts/stream")
def stream_tts(req: TTSRequest) -> StreamingResponse:
    normalized_text = " ".join(req.text.split())
    if not normalized_text:
        raise HTTPException(status_code=400, detail="Text is empty after normalization.")

    if TTS_BACKEND == "voicebox":
        voice_id = normalize_voice_id(req.voice_id)
        profile_id = VOICEBOX_PROFILE_IDS.get(voice_id, "")
        if not profile_id:
            raise HTTPException(
                status_code=503,
                detail=f"No VoiceBox profile configured for voice_id={voice_id!r}.",
            )
        start_time = time.perf_counter()
        try:
            sample_rate, channels, bits_per_sample, remainder, vb_resp = \
                _voicebox_open_pcm_stream(profile_id, normalized_text)
        except Exception as exc:
            logger.exception("VoiceBox /tts/stream open failed")
            raise HTTPException(status_code=500, detail=f"VoiceBox stream failed: {exc}") from exc

        pending: bytearray = bytearray()

        def _stream_voicebox_pcm():
            chunk_count = 0
            try:
                if remainder:
                    pcm = _wav_samples_to_f32le(remainder, bits_per_sample, pending)
                    if pcm:
                        chunk_count += 1
                        yield pcm
                for raw in vb_resp.iter_content(chunk_size=8192):
                    if raw:
                        pcm = _wav_samples_to_f32le(raw, bits_per_sample, pending)
                        if pcm:
                            chunk_count += 1
                            yield pcm
            except Exception:
                logger.exception("VoiceBox PCM stream error")
                raise
            finally:
                vb_resp.close()
                logger.info(
                    "VoiceBox streamed %d chunks in %.2fs for %d characters (voice_id=%s, profile_id=%s, %dHz %dch %dbit)",
                    chunk_count, time.perf_counter() - start_time, len(normalized_text),
                    voice_id, profile_id, sample_rate, channels, bits_per_sample,
                )

        return StreamingResponse(
            _stream_voicebox_pcm(),
            media_type=STREAM_MEDIA_TYPE,
            headers={
                STREAM_SAMPLE_RATE_HEADER: str(sample_rate),
                STREAM_CHANNELS_HEADER: str(channels),
                STREAM_DTYPE_HEADER: STREAM_DTYPE,
            },
        )

    try:
        model = ensure_model_loaded()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"TTS unavailable: {exc}") from exc

    profile = get_voice_profile(req.voice_id)
    if DEFAULT_LOCK_VOICE_STYLE:
        voice_style = profile.voice_style
    else:
        voice_style = (req.voice_style or profile.voice_style).strip()
    voice_prompt = f"({voice_style}) {normalized_text}"
    sample_rate = int(model.tts_model.sample_rate)

    def generate_pcm_chunks():
        start_time = time.perf_counter()
        chunk_count = 0
        sample_count = 0
        voice_mode = "voice_design_streaming"
        try:
            with MODEL_LOCK:
                prompt_cache = ensure_voice_prompt_cache(model, profile.voice_id)
                seed_generation()
                if prompt_cache is None:
                    for wav in model.generate_streaming(
                        text=voice_prompt,
                        cfg_value=2.0,
                        inference_timesteps=MODEL_INFERENCE_TIMESTEPS,
                        retry_badcase=False,
                    ):
                        chunk_count += 1
                        sample_count += int(np.asarray(wav).size)
                        yield audio_to_pcm_f32le(wav)
                else:
                    voice_mode = f"{VOICE_GENERATION_MODE}_streaming"
                    for wav_tensor, _, _ in model.tts_model.generate_with_prompt_cache_streaming(
                        target_text=normalized_text,
                        prompt_cache=prompt_cache,
                        cfg_value=2.0,
                        inference_timesteps=MODEL_INFERENCE_TIMESTEPS,
                        retry_badcase=False,
                    ):
                        wav = wav_tensor.squeeze(0).cpu().numpy()
                        chunk_count += 1
                        sample_count += int(np.asarray(wav).size)
                        yield audio_to_pcm_f32le(wav)
        except Exception:
            logger.exception("VoxCPM2 streaming generation failed")
            raise
        finally:
            logger.info(
                "Streamed %d chunks / %d samples in %.2fs for %d characters "
                "(voice_id=%s, seed=%d, locked_style=%s, voice_mode=%s, inference_timesteps=%d)",
                chunk_count,
                sample_count,
                time.perf_counter() - start_time,
                len(normalized_text),
                profile.voice_id,
                DEFAULT_TTS_SEED,
                DEFAULT_LOCK_VOICE_STYLE,
                voice_mode,
                MODEL_INFERENCE_TIMESTEPS,
            )

    return StreamingResponse(
        generate_pcm_chunks(),
        media_type=STREAM_MEDIA_TYPE,
        headers={
            STREAM_SAMPLE_RATE_HEADER: str(sample_rate),
            STREAM_CHANNELS_HEADER: "1",
            STREAM_DTYPE_HEADER: STREAM_DTYPE,
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local VoxCPM2 TTS server.")
    parser.add_argument("--host", default=os.getenv("MARA_TTS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MARA_TTS_PORT", "8000")))
    parser.add_argument("--model-path", default=str(DEFAULT_MODEL_PATH))
    parser.add_argument(
        "--device",
        default=DEFAULT_DEVICE,
        help="Device override for VoxCPM: auto, cpu, cuda, cuda:0, etc.",
    )
    parser.add_argument(
        "--no-optimize",
        action="store_true",
        help="Disable VoxCPM warmup/compile optimizations for debugging or fallback runs.",
    )
    parser.add_argument(
        "--inference-timesteps",
        type=int,
        default=DEFAULT_INFERENCE_TIMESTEPS,
        help="Diffusion sampling steps. Lower is faster; 4 is the launcher default, try 4 for max speed or 8-10 for best quality.",
    )
    parser.add_argument(
        "--voice-reference-path",
        default=str(DEFAULT_VOICE_REFERENCE_PATH),
        help="Persistent reference WAV used to keep Mara's voice identity stable.",
    )
    parser.add_argument(
        "--voice-reference-text",
        default=DEFAULT_VOICE_REFERENCE_TEXT,
        help="Exact transcript of the persistent reference WAV.",
    )
    parser.add_argument(
        "--disable-voice-reference",
        action="store_true",
        help="Use text-only voice design instead of the persistent reference voice.",
    )
    parser.add_argument(
        "--regenerate-voice-reference",
        action="store_true",
        help="Recreate the persistent reference WAV on startup.",
    )
    parser.add_argument(
        "--hermes-voice-style",
        default=os.getenv("MARA_VOICE_STYLE", ""),
        help="Voice style description for Mara (Hermes). Overrides the built-in default.",
    )
    parser.add_argument(
        "--openclaw-voice-style",
        default=os.getenv("MARA_OPENCLAW_VOICE_STYLE", ""),
        help="Voice style description for OpenClaw. Overrides the built-in default.",
    )
    parser.add_argument("--log-level", default=os.getenv("MARA_LOG_LEVEL", "INFO"))
    parser.add_argument(
        "--tts-backend",
        default=os.getenv("MARA_TTS_BACKEND", "voxcpm2"),
        choices=["voxcpm2", "voicebox"],
        help="TTS backend: 'voxcpm2' (default local model) or 'voicebox' (proxy to VoiceBox app).",
    )
    parser.add_argument(
        "--voicebox-url",
        default=os.getenv("MARA_VOICEBOX_URL", "http://127.0.0.1:17493"),
        help="Base URL for the VoiceBox API server.",
    )
    parser.add_argument(
        "--hermes-voicebox-profile-id",
        default=os.getenv("MARA_HERMES_VOICEBOX_PROFILE_ID", ""),
        help="VoiceBox profile UUID to use for Hermes/Mara.",
    )
    parser.add_argument(
        "--openclaw-voicebox-profile-id",
        default=os.getenv("MARA_OPENCLAW_VOICEBOX_PROFILE_ID", ""),
        help="VoiceBox profile UUID to use for OpenClaw.",
    )
    return parser


def main() -> int:
    global TTS_BACKEND, VOICEBOX_URL
    args = build_parser().parse_args()
    configure_logging(args.log_level)
    TTS_BACKEND = args.tts_backend
    VOICEBOX_URL = args.voicebox_url.rstrip("/")
    if args.hermes_voicebox_profile_id:
        VOICEBOX_PROFILE_IDS["hermes"] = args.hermes_voicebox_profile_id
    if args.openclaw_voicebox_profile_id:
        VOICEBOX_PROFILE_IDS["openclaw"] = args.openclaw_voicebox_profile_id
    if TTS_BACKEND == "voicebox":
        logger.info(
            "TTS backend: voicebox @ %s (hermes=%s, openclaw=%s)",
            VOICEBOX_URL,
            VOICEBOX_PROFILE_IDS.get("hermes") or "not configured",
            VOICEBOX_PROFILE_IDS.get("openclaw") or "not configured",
        )
    else:
        set_model_path(Path(args.model_path).expanduser())
        set_runtime_options(args.device, not args.no_optimize)
        set_generation_options(args.inference_timesteps)
        set_voice_reference_options(
            Path(args.voice_reference_path).expanduser(),
            args.voice_reference_text,
            not args.disable_voice_reference,
            args.regenerate_voice_reference,
        )
        set_voice_style_options(args.hermes_voice_style, args.openclaw_voice_style)
    logger.info("Starting Mara TTS server on %s:%s", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
