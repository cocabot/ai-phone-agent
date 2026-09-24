"""Load settings from env / .env. Never log secret values."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Always read the .env next to this file, regardless of the caller's cwd.
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def normalize_public_url(raw: str) -> str:
    """Accept 'host', 'https://host/', 'wss://host' and return 'https://host'."""
    value = raw.strip().rstrip("/")
    if not value:
        return ""
    for prefix in ("https://", "http://", "wss://", "ws://"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    return f"https://{value}"


@dataclass(frozen=True)
class Settings:
    # Vonage
    application_id: str
    private_key_path: Path
    api_key: str
    api_secret: str
    from_number: str
    # Public URL handling
    public_url: str
    tunnel: str  # auto | cloudflared | metrics | none
    cloudflared_bin: str
    cloudflared_metrics: str
    cloudflared_protocol: str
    auto_sync: bool
    webhook_token: str
    # Gemini
    gemini_api_key: str
    gemini_model: str
    gemini_voice: str
    # Call behaviour
    audio_rate: int
    max_call_seconds: int
    ringing_timeout: int
    allowed_country_codes: tuple[str, ...]
    call_allowlist: tuple[str, ...]
    agent_name: str
    inbound_goal: str
    extra_instructions: str
    # Server
    host: str
    port: int
    data_dir: Path
    api_token: str = field(repr=False, default="")

    @property
    def private_key_present(self) -> bool:
        p = self.private_key_path
        return p.is_file() and p.stat().st_size > 0

    @property
    def local_api_base(self) -> str:
        host = "127.0.0.1" if self.host in {"0.0.0.0", "::", ""} else self.host
        return f"http://{host}:{self.port}"


def _split(raw: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in raw.replace(";", ",").split(",") if p.strip())


def load_settings() -> Settings:
    key_path = Path(_env("VONAGE_PRIVATE_KEY_PATH", "./private.key")).expanduser()
    if not key_path.is_absolute():
        key_path = BASE_DIR / key_path
    data_dir = Path(_env("DATA_DIR", "./data")).expanduser()
    if not data_dir.is_absolute():
        data_dir = BASE_DIR / data_dir

    # VONAGE_PUBLIC_DOMAIN is the legacy name (hostname only).
    public_url = normalize_public_url(_env("PUBLIC_URL") or _env("VONAGE_PUBLIC_DOMAIN"))

    audio_rate = _env_int("AUDIO_RATE", 16000)
    if audio_rate not in (8000, 16000, 24000):
        audio_rate = 16000

    return Settings(
        application_id=_env("VONAGE_APPLICATION_ID"),
        private_key_path=key_path,
        api_key=_env("VONAGE_API_KEY"),
        api_secret=_env("VONAGE_API_SECRET"),
        from_number=_env("VONAGE_FROM_NUMBER").lstrip("+"),
        public_url=public_url,
        tunnel=_env("TUNNEL", "auto").lower(),
        cloudflared_bin=_env("CLOUDFLARED_BIN"),
        cloudflared_metrics=_env("CLOUDFLARED_METRICS"),
        cloudflared_protocol=_env("CLOUDFLARED_PROTOCOL"),
        auto_sync=_env_bool("VONAGE_AUTO_SYNC", True),
        webhook_token=_env("WEBHOOK_TOKEN"),
        gemini_api_key=_env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY"),
        gemini_model=_env("GEMINI_MODEL", "gemini-3.8-live"),
        gemini_voice=_env("GEMINI_VOICE", "Kore"),
        audio_rate=audio_rate,
        max_call_seconds=_env_int("MAX_CALL_SECONDS", 600),
        ringing_timeout=_env_int("RINGING_TIMEOUT", 45),
        allowed_country_codes=_split(_env("ALLOWED_COUNTRY_CODES", "81")),
        call_allowlist=tuple(n.lstrip("+") for n in _split(_env("CALL_ALLOWLIST"))),
        agent_name=_env("AGENT_NAME", "AIアシスタント"),
        inbound_goal=_env("INBOUND_GOAL"),
        extra_instructions=_env("EXTRA_INSTRUCTIONS"),
        host=_env("HOST", "0.0.0.0"),
        port=_env_int("PORT", 8090),
        data_dir=data_dir,
        api_token=_env("AGENT_API_TOKEN"),
    )
