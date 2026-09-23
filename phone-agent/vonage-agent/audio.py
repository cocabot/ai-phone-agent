"""PCM helpers for the Vonage <-> Gemini audio path.

Gemini Live always emits 16-bit PCM at 24 kHz; Vonage plays whatever rate the
NCCO negotiated (8/16/24 kHz) in fixed 20 ms frames. Telephone audio is
band-limited well below 4 kHz, so a stateful linear-interpolation resampler is
sufficient and keeps the dependency footprint to numpy.
"""

from __future__ import annotations

import numpy as np

GEMINI_OUTPUT_RATE = 24000
FRAME_MS = 20


def frame_bytes(rate: int, frame_ms: int = FRAME_MS) -> int:
    """Bytes per mono 16-bit frame of ``frame_ms`` at ``rate``."""
    return rate * frame_ms // 1000 * 2


class LinearResampler:
    """Stream-safe linear resampler for little-endian int16 mono PCM."""

    def __init__(self, src_rate: int, dst_rate: int):
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError("rates must be positive")
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self.ratio = src_rate / dst_rate
        self._tail: float | None = None  # last input sample of the previous chunk
        self._pos = 0.0  # next output position, relative to the tail sample
        self._carry = b""  # dangling byte when a chunk ends mid-sample

    def reset(self) -> None:
        self._tail = None
        self._pos = 0.0
        self._carry = b""

    def process(self, pcm: bytes) -> bytes:
        if self.src_rate == self.dst_rate or not pcm:
            return pcm
        if self._carry:
            pcm = self._carry + pcm
        usable = len(pcm) - (len(pcm) % 2)
        self._carry = pcm[usable:]
        samples = np.frombuffer(pcm[:usable], dtype="<i2").astype(np.float64)
        if samples.size == 0:
            return b""

        if self._tail is not None:
            x = np.concatenate(([self._tail], samples))
            start = self._pos
        else:
            x = samples
            start = 0.0

        last_index = x.size - 1
        if last_index < 1 or start > last_index:
            # Not enough input to produce a sample yet; carry state forward.
            self._tail = float(x[-1])
            self._pos = max(start - last_index, 0.0)
            return b""

        n_out = int(np.floor((last_index - start) / self.ratio)) + 1
        positions = start + np.arange(n_out) * self.ratio
        idx = np.minimum(np.floor(positions).astype(np.int64), last_index - 1)
        frac = positions - idx
        out = x[idx] * (1.0 - frac) + x[idx + 1] * frac

        self._pos = (start + n_out * self.ratio) - last_index
        self._tail = float(x[-1])
        return np.clip(np.rint(out), -32768, 32767).astype("<i2").tobytes()


class FrameChunker:
    """Re-packetise a byte stream into fixed-size frames."""

    def __init__(self, size: int):
        if size <= 0:
            raise ValueError("frame size must be positive")
        self.size = size
        self._buf = bytearray()

    def push(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        frames: list[bytes] = []
        while len(self._buf) >= self.size:
            frames.append(bytes(self._buf[: self.size]))
            del self._buf[: self.size]
        return frames

    def flush(self) -> bytes | None:
        """Return the remaining partial frame zero-padded to a full frame."""
        if not self._buf:
            return None
        frame = bytes(self._buf) + b"\x00" * (self.size - len(self._buf))
        self._buf.clear()
        return frame

    def clear(self) -> None:
        self._buf.clear()

    @property
    def pending(self) -> int:
        return len(self._buf)
