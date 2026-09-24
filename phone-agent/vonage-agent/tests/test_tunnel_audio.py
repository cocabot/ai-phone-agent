from audio import Framer, Resampler
from config import normalize_public_url
from tunnel import extract_quick_tunnel_url


def test_extract_quick_tunnel_url():
    line = "2026-09-24T00:00:00Z INF |  https://funny-cats-jump-high.trycloudflare.com  |"
    assert extract_quick_tunnel_url(line) == "https://funny-cats-jump-high.trycloudflare.com"
    assert extract_quick_tunnel_url("failed to request https://api.trycloudflare.com/tunnel") is None
    assert extract_quick_tunnel_url("INF Starting tunnel") is None


def test_normalize_public_url():
    assert normalize_public_url("abc.trycloudflare.com/") == "https://abc.trycloudflare.com"
    assert normalize_public_url("wss://abc.trycloudflare.com") == "https://abc.trycloudflare.com"
    assert normalize_public_url("") == ""


def test_framer_splits_and_pads():
    f = Framer(16000)
    assert f.frame_bytes == 640
    frames = f.push(b"\x01" * 1500)
    assert [len(x) for x in frames] == [640, 640]
    tail = f.flush()
    assert len(tail) == 1 and len(tail[0]) == 640 and tail[0].endswith(b"\x00" * 420)
    assert f.flush() == []


def test_resampler_24k_to_16k_keeps_duration():
    r = Resampler(24000, 16000)
    total = sum(len(r(b"\x00\x10" * 2400)) for _ in range(10))  # 10 x 100 ms
    assert abs(total - 32000) <= 8  # ~1 s @ 16 kHz, 16-bit
    assert Resampler(24000, 24000)(b"abcd") == b"abcd"
