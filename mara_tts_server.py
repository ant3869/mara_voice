from __future__ import annotations

import argparse
import io
import json
import os
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from mara_streaming import (
    STREAM_CHANNELS_HEADER,
    STREAM_DTYPE,
    STREAM_DTYPE_HEADER,
    STREAM_MEDIA_TYPE,
    STREAM_SAMPLE_RATE_HEADER,
    audio_to_pcm_f32le,
)
from mara_pipeline import BASE_DIR, configure_logging

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
DEFAULT_DEVICE = os.getenv("MARA_TTS_DEVICE", "auto")
DEFAULT_OPTIMIZE = os.getenv("MARA_TTS_OPTIMIZE", "1") != "0"
DEFAULT_INFERENCE_TIMESTEPS = int(os.getenv("MARA_TTS_INFERENCE_TIMESTEPS", "8"))
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

logger = configure_logging(os.getenv("MARA_LOG_LEVEL", "INFO")).getChild("tts_server")
MODEL = None
MODEL_ERROR: str | None = None
MODEL_PATH = DEFAULT_MODEL_PATH
MODEL_DEVICE = DEFAULT_DEVICE
MODEL_OPTIMIZE = DEFAULT_OPTIMIZE
MODEL_INFERENCE_TIMESTEPS = DEFAULT_INFERENCE_TIMESTEPS
MODEL_LOCK = RLock()
VOICE_REFERENCE_ENABLED = DEFAULT_USE_VOICE_REFERENCE
VOICE_REFERENCE_AUTO_CREATE = DEFAULT_AUTO_CREATE_VOICE_REFERENCE
VOICE_REFERENCE_REGENERATE = DEFAULT_REGENERATE_VOICE_REFERENCE
VOICE_REFERENCE_PATH = DEFAULT_VOICE_REFERENCE_PATH
VOICE_REFERENCE_TEXT = DEFAULT_VOICE_REFERENCE_TEXT
VOICE_PROMPT_CACHE = None
VOICE_PROMPT_CACHES: dict[str, object] = {}
VOICE_REFERENCE_ERROR: str | None = None
VOICE_REFERENCE_ERRORS: dict[str, str | None] = {}

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


def seed_generation() -> None:
    random.seed(DEFAULT_TTS_SEED)
    np.random.seed(DEFAULT_TTS_SEED)
    if TORCH_AVAILABLE:
        torch.manual_seed(DEFAULT_TTS_SEED)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(DEFAULT_TTS_SEED)


def ensure_model_loaded():
    global MODEL, MODEL_ERROR

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

        logger.info(
            "Loading VoxCPM2 from %s with device=%s optimize=%s",
            MODEL_PATH,
            MODEL_DEVICE,
            MODEL_OPTIMIZE,
        )
        start_time = time.perf_counter()
        try:
            requested_device = None if MODEL_DEVICE in ("", "auto", None) else MODEL_DEVICE
            MODEL = VoxCPM.from_pretrained(
                str(MODEL_PATH),
                load_denoiser=False,
                device=requested_device,
                optimize=MODEL_OPTIMIZE,
            )
        except Exception as exc:
            MODEL_ERROR = str(exc)
            logger.exception("Failed to load VoxCPM2")
            raise RuntimeError(MODEL_ERROR) from exc

        MODEL_ERROR = None
        logger.info("VoxCPM2 is ready after %.2fs", time.perf_counter() - start_time)
        return MODEL


def ensure_voice_reference_file(model, profile: VoiceProfile) -> None:
    if not VOICE_REFERENCE_ENABLED:
        return

    metadata_path = profile.reference_path.with_suffix(f"{profile.reference_path.suffix}.json")
    if profile.reference_path.exists() and metadata_path.exists() and not VOICE_REFERENCE_REGENERATE:
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

    elif profile.reference_path.exists() and not VOICE_REFERENCE_REGENERATE:
        logger.info("Persistent voice reference metadata is missing; regenerating %s", profile.reference_path)

    if profile.reference_path.exists() and not VOICE_REFERENCE_REGENERATE and not VOICE_REFERENCE_AUTO_CREATE:
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
    global VOICE_PROMPT_CACHE, VOICE_REFERENCE_ERROR, VOICE_REFERENCE_REGENERATE

    if not VOICE_REFERENCE_ENABLED:
        VOICE_REFERENCE_ERROR = None
        return None

    profile = get_voice_profile(voice_id)
    prompt_cache = VOICE_PROMPT_CACHES.get(profile.voice_id)
    if prompt_cache is not None and not VOICE_REFERENCE_REGENERATE:
        return prompt_cache

    try:
        ensure_voice_reference_file(model, profile)
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
    VOICE_REFERENCE_REGENERATE = False
    logger.info("Persistent %s voice reference cache is ready: %s", profile.voice_id, profile.reference_path)
    return prompt_cache


@asynccontextmanager
async def lifespan(_app: FastAPI):
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
        "model_loaded": MODEL is not None,
        "model_path": str(MODEL_PATH),
        "device": MODEL_DEVICE,
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
        "stream_media_type": STREAM_MEDIA_TYPE,
    }


@app.post("/tts")
def generate_tts(req: TTSRequest) -> Response:
    normalized_text = " ".join(req.text.split())
    if not normalized_text:
        raise HTTPException(status_code=400, detail="Text is empty after normalization.")

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
        help="Diffusion sampling steps. Lower is faster; 8 is the launcher default, 10 was the previous default.",
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
    parser.add_argument("--log-level", default=os.getenv("MARA_LOG_LEVEL", "INFO"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    configure_logging(args.log_level)
    set_model_path(Path(args.model_path).expanduser())
    set_runtime_options(args.device, not args.no_optimize)
    set_generation_options(args.inference_timesteps)
    set_voice_reference_options(
        Path(args.voice_reference_path).expanduser(),
        args.voice_reference_text,
        not args.disable_voice_reference,
        args.regenerate_voice_reference,
    )
    logger.info("Starting Mara TTS server on %s:%s", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())











# import io
# import soundfile as sf
# from fastapi import FastAPI
# from pydantic import BaseModel
# from fastapi.responses import StreamingResponse
# import uvicorn
# from voxcpm import VoxCPM

# app = FastAPI()

# print("Loading VoxCPM2 into VRAM... (This takes a minute on first boot)")
# model = VoxCPM.from_pretrained("openbmb/VoxCPM2", load_denoiser=False)
# print("Mara's vocal cords are online.")

# class TTSRequest(BaseModel):
#     text: str

# @app.post("/tts")
# def generate_tts(req: TTSRequest):
#     # This is the magic prompt that designs Mara's voice on the fly
#     voice_prompt = f"(A sultry, confident female operator voice, slightly bossy but warm){req.text}"

#     wav = model.generate(
#         text=voice_prompt,        cfg_value=2.0,
#         inference_timesteps=10
#     )

#     # Convert audio array to WAV format in memory
#     buf = io.BytesIO()
#     sf.write(buf, wav, model.tts_model.sample_rate, format='WAV')
#     buf.seek(0)
#     return StreamingResponse(buf, media_type="audio/wav")

# if __name__ == "__main__":
#     uvicorn.run(app, host="127.0.0.1", port=8000)
