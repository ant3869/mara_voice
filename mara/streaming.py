from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


STREAM_MEDIA_TYPE = "audio/x-mara-pcm-f32le"
STREAM_SAMPLE_RATE_HEADER = "x-mara-sample-rate"
STREAM_CHANNELS_HEADER = "x-mara-channels"
STREAM_DTYPE_HEADER = "x-mara-dtype"
STREAM_DTYPE = "float32le"


def audio_to_pcm_f32le(audio: Any) -> bytes:
    array = np.asarray(audio, dtype=np.float32)
    array = np.squeeze(array)
    if array.ndim == 0:
        array = array.reshape(1)
    if array.ndim > 2:
        raise ValueError(f"Expected mono or stereo audio, got shape {array.shape}")
    array = np.nan_to_num(array, nan=0.0, posinf=1.0, neginf=-1.0)
    array = np.clip(array, -1.0, 1.0)
    return np.ascontiguousarray(array.astype("<f4", copy=False)).tobytes()


@dataclass(slots=True)
class Float32PcmStreamDecoder:
    channels: int = 1
    _pending: bytes = b""

    def decode(self, payload: bytes) -> np.ndarray | None:
        if not payload:
            return None

        frame_size = max(1, self.channels) * np.dtype("<f4").itemsize
        data = self._pending + payload
        usable_bytes = (len(data) // frame_size) * frame_size
        if usable_bytes <= 0:
            self._pending = data
            return None

        raw = data[:usable_bytes]
        self._pending = data[usable_bytes:]
        audio = np.frombuffer(raw, dtype="<f4").astype(np.float32, copy=False)
        if self.channels > 1:
            return audio.reshape(-1, self.channels)
        return audio.reshape(-1, 1)

    def has_pending_bytes(self) -> bool:
        return bool(self._pending)
