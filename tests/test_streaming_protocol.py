from __future__ import annotations

import unittest

import numpy as np
from fastapi.testclient import TestClient

import mara_tts_server
from mara_streaming import (
    STREAM_CHANNELS_HEADER,
    STREAM_DTYPE,
    STREAM_DTYPE_HEADER,
    STREAM_MEDIA_TYPE,
    STREAM_SAMPLE_RATE_HEADER,
    Float32PcmStreamDecoder,
    audio_to_pcm_f32le,
)


class FakeTensor:
    def __init__(self, audio: np.ndarray) -> None:
        self.audio = audio

    def squeeze(self, _axis: int) -> "FakeTensor":
        return self

    def cpu(self) -> "FakeTensor":
        return self

    def numpy(self) -> np.ndarray:
        return self.audio


class FakeTtsModel:
    sample_rate = 24000

    def generate_with_prompt_cache_streaming(self, **_kwargs):
        yield FakeTensor(np.array([[0.0, 0.25]], dtype=np.float32)), None, None
        yield FakeTensor(np.array([[0.5, 0.75]], dtype=np.float32)), None, None


class FakeModel:
    tts_model = FakeTtsModel()


class StreamingProtocolTests(unittest.TestCase):
    def test_pcm_decoder_handles_partial_frame_boundaries(self) -> None:
        samples = np.array([0.0, 0.5, -0.5, 1.0], dtype=np.float32)
        payload = audio_to_pcm_f32le(samples)
        decoder = Float32PcmStreamDecoder(channels=1)

        chunks = [
            decoder.decode(payload[:3]),
            decoder.decode(payload[3:9]),
            decoder.decode(payload[9:]),
        ]
        decoded = np.concatenate([chunk.reshape(-1) for chunk in chunks if chunk is not None])

        self.assertFalse(decoder.has_pending_bytes())
        np.testing.assert_allclose(decoded, samples)

    def test_stream_tts_endpoint_emits_float32_pcm(self) -> None:
        original_ensure_model_loaded = mara_tts_server.ensure_model_loaded
        original_ensure_voice_prompt_cache = mara_tts_server.ensure_voice_prompt_cache
        original_seed_generation = mara_tts_server.seed_generation

        mara_tts_server.ensure_model_loaded = lambda: FakeModel()
        mara_tts_server.ensure_voice_prompt_cache = lambda _model, _voice_id=None: {"mode": "reference"}
        mara_tts_server.seed_generation = lambda: None
        try:
            client = TestClient(mara_tts_server.app)
            response = client.post("/tts/stream", json={"text": "hello"})
        finally:
            mara_tts_server.ensure_model_loaded = original_ensure_model_loaded
            mara_tts_server.ensure_voice_prompt_cache = original_ensure_voice_prompt_cache
            mara_tts_server.seed_generation = original_seed_generation

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"].split(";")[0], STREAM_MEDIA_TYPE)
        self.assertEqual(response.headers[STREAM_SAMPLE_RATE_HEADER], "24000")
        self.assertEqual(response.headers[STREAM_CHANNELS_HEADER], "1")
        self.assertEqual(response.headers[STREAM_DTYPE_HEADER], STREAM_DTYPE)
        decoded = np.frombuffer(response.content, dtype="<f4")
        np.testing.assert_allclose(decoded, np.array([0.0, 0.25, 0.5, 0.75], dtype=np.float32))

    def test_stream_tts_passes_openclaw_voice_id_to_prompt_cache(self) -> None:
        original_ensure_model_loaded = mara_tts_server.ensure_model_loaded
        original_ensure_voice_prompt_cache = mara_tts_server.ensure_voice_prompt_cache
        original_seed_generation = mara_tts_server.seed_generation
        seen_voice_ids: list[str | None] = []

        mara_tts_server.ensure_model_loaded = lambda: FakeModel()
        mara_tts_server.ensure_voice_prompt_cache = lambda _model, voice_id=None: seen_voice_ids.append(voice_id) or {"mode": "reference"}
        mara_tts_server.seed_generation = lambda: None
        try:
            client = TestClient(mara_tts_server.app)
            response = client.post("/tts/stream", json={"text": "hello", "voice_id": "openclaw"})
        finally:
            mara_tts_server.ensure_model_loaded = original_ensure_model_loaded
            mara_tts_server.ensure_voice_prompt_cache = original_ensure_voice_prompt_cache
            mara_tts_server.seed_generation = original_seed_generation

        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen_voice_ids, ["openclaw"])


if __name__ == "__main__":
    unittest.main()
