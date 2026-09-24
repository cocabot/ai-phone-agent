"""PCM helpers: Gemini output (24 kHz) -> Vonage rate, and fixed 20 ms framing."""

from __future__ import annotations

import warnings

# stdlib up to Python 3.12; on 3.13+ the same module comes from `audioop-lts`.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop

GEMINI_OUTPUT_RATE = 24000
SAMPLE_WIDTH = 2  # 16-bit little-endian mono


class Resampler:
    """Stateful linear PCM16 resampler (keeps filter state across chunks)."""

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src = src_rate
        self.dst = dst_rate
        self._state = None

    def reset(self) -> None:
        self._state = None

    def __call__(self, pcm: bytes) -> bytes:
        if self.src == self.dst or not pcm:
            return pcm
        if len(pcm) % SAMPLE_WIDTH:
            pcm = pcm[:-1]
        out, self._state = audioop.ratecv(pcm, SAMPLE_WIDTH, 1, self.src, self.dst, self._state)
        return out


class Framer:
    """Split an arbitrary PCM byte stream into fixed 20 ms frames (what Vonage expects)."""

    def __init__(self, rate: int, frame_ms: int = 20) -> None:
        self.frame_bytes = rate * SAMPLE_WIDTH * frame_ms // 1000
        self._buf = bytearray()

    def push(self, pcm: bytes) -> list[bytes]:
        self._buf.extend(pcm)
        n = len(self._buf) // self.frame_bytes
        frames = [bytes(self._buf[i * self.frame_bytes : (i + 1) * self.frame_bytes]) for i in range(n)]
        del self._buf[: n * self.frame_bytes]
        return frames

    def flush(self) -> list[bytes]:
        """Return the remainder padded with silence."""
        if not self._buf:
            return []
        frame = bytes(self._buf) + b"\x00" * (self.frame_bytes - len(self._buf))
        self._buf.clear()
        return [frame]

    def clear(self) -> None:
        self._buf.clear()
