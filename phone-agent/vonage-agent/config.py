"""Load vonage-agent settings from the environment. Never log secret values.

All settings are read once at import time (``settings``). Values that must be
stable for the lifetime of the process (admin token, WebSocket auth token) are
generated here when they are not configured, so every module sees the same
value.
"""

from __future__ import annotations

import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / ".runtime"

# Explicit path so the CLI works from any working directory.
load_dotenv(BASE_DIR / ".env")

DEFAULT_GEMINI_MODEL = "gemini-3.8-live"
DEFAULT_GEMINI_VOICE = "Kore"
# Vonage's documented trial caller ID: works only while the account is on the free trial.
VONAGE_TRIAL_CALLER_ID = "123456789"

_GENERATED_ADMIN_TOKEN = secrets.token_urlsafe(32)
_GENERATED_WS_TOKEN = secrets.token_urlsafe(32)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise SystemExit(f"{name} must be a boolean (1/0, true/false), got {raw!r}")


def normalize_public_url(value: str) -> str:
    """Accept ``host``, ``https://host`` or ``wss://host/`` and return ``https://host``.

    Returns an empty string when nothing usable was provided.
    """
    v = (value or "").strip()
    if not v:
        return ""
    for prefix in ("https://", "http://", "wss://", "ws://"):
        if v.lower().startswith(prefix):
            v = v[len(prefix):]
            break
    v = v.split("/", 1)[0].strip()
    if not v:
        return ""
    return f"https://{v}"


@dataclass(frozen=True)
class Settings:
    # Vonage application (Voice API auth uses JWTs signed with the private key)
    application_id: str
    private_key_path: str
    private_key_inline: str
    # Vonage account (Application API uses basic auth; required for webhook auto-sync)
    api_key: str
    api_secret: str
    from_number: str
    signature_secret: str
    api_base: str

    # Public URL and tunnel
    public_url: str
    tunnel_mode: str  # auto | quick | none
    cloudflared_bin: str
    cloudflared_extra_args: str
    sync_webhooks: bool
    public_url_wait_seconds: int

    # HTTP server
    host: str
    port: int
    admin_token: str

    # Call behaviour
    audio_rate: int
    call_max_seconds: int
    ringing_timeout: int
    max_concurrent_calls: int
    announce_ai: bool
    announce_text: str
    allowed_to_numbers: tuple[str, ...]
    ws_auth: bool
    ws_auth_token: str
    transcript_log: bool

    # Gemini Live
    gemini_api_key: str
    gemini_model: str
    gemini_voice: str
    agent_name: str
    agent_extra_instructions: str
    inbound_objective: str

    @property
    def local_base_url(self) -> str:
        host = "127.0.0.1" if self.host in {"0.0.0.0", "", "::"} else self.host
        return f"http://{host}:{self.port}"

    @property
    def is_trial_caller_id(self) -> bool:
        return self.from_number == VONAGE_TRIAL_CALLER_ID


def load_settings() -> Settings:
    public_url = normalize_public_url(_env("PUBLIC_URL") or _env("VONAGE_PUBLIC_DOMAIN"))

    tunnel_mode = (_env("TUNNEL", "auto") or "auto").lower()
    if tunnel_mode not in {"auto", "quick", "none"}:
        raise SystemExit("TUNNEL must be one of: auto, quick, none")

    audio_rate = _env_int("VONAGE_AUDIO_RATE", 16000)
    if audio_rate not in {8000, 16000, 24000}:
        raise SystemExit("VONAGE_AUDIO_RATE must be 8000, 16000 or 24000")

    allowed = tuple(
        n.strip() for n in _env("ALLOWED_TO_NUMBERS").replace(";", ",").split(",") if n.strip()
    )

    private_key_path = _env("VONAGE_PRIVATE_KEY_PATH") or str(BASE_DIR / "private.key")
    if not os.path.isabs(private_key_path):
        private_key_path = str((BASE_DIR / private_key_path).resolve())

    return Settings(
        application_id=_env("VONAGE_APPLICATION_ID"),
        private_key_path=private_key_path,
        private_key_inline=_env("VONAGE_PRIVATE_KEY"),
        api_key=_env("VONAGE_API_KEY"),
        api_secret=_env("VONAGE_API_SECRET"),
        from_number=_env("VONAGE_FROM_NUMBER").lstrip("+"),
        signature_secret=_env("VONAGE_SIGNATURE_SECRET"),
        api_base=_env("VONAGE_API_BASE", "https://api.nexmo.com").rstrip("/"),
        public_url=public_url,
        tunnel_mode=tunnel_mode,
        cloudflared_bin=_env("CLOUDFLARED_BIN", "cloudflared"),
        cloudflared_extra_args=_env("CLOUDFLARED_ARGS"),
        sync_webhooks=_env_bool("SYNC_WEBHOOKS", True),
        public_url_wait_seconds=_env_int("PUBLIC_URL_WAIT_SECONDS", 45),
        host=_env("HOST", "0.0.0.0"),
        port=_env_int("PORT", 8090),
        admin_token=_env("ADMIN_TOKEN") or _GENERATED_ADMIN_TOKEN,
        audio_rate=audio_rate,
        call_max_seconds=_env_int("CALL_MAX_SECONDS", 900),
        ringing_timeout=_env_int("RINGING_TIMEOUT", 45),
        max_concurrent_calls=_env_int("MAX_CONCURRENT_CALLS", 1),
        announce_ai=_env_bool("ANNOUNCE_AI", True),
        announce_text=_env("ANNOUNCE_TEXT", "AIアシスタントからのお電話です。"),
        allowed_to_numbers=allowed,
        ws_auth=_env_bool("WS_AUTH", True),
        ws_auth_token=_env("WS_AUTH_TOKEN") or _GENERATED_WS_TOKEN,
        transcript_log=_env_bool("TRANSCRIPT_LOG", False),
        gemini_api_key=_env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY"),
        gemini_model=_env("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
        gemini_voice=_env("GEMINI_VOICE", DEFAULT_GEMINI_VOICE),
        agent_name=_env("AGENT_NAME", "AIアシスタント"),
        agent_extra_instructions=_env("AGENT_EXTRA_INSTRUCTIONS"),
        inbound_objective=_env(
            "INBOUND_OBJECTIVE",
            "着信対応: 丁寧に用件を伺い、要点を確認してください。折り返しが必要なら連絡先を聞いてください。",
        ),
    )


def private_key_file_ok(path: str) -> bool:
    p = Path(path)
    return p.is_file() and p.stat().st_size > 0


def load_private_key_pem(s: Settings) -> str:
    """Return the PEM private key, from VONAGE_PRIVATE_KEY or the key file."""
    if s.private_key_inline:
        return s.private_key_inline.replace("\\n", "\n")
    if not private_key_file_ok(s.private_key_path):
        raise SystemExit(
            f"Vonage private key not found at {s.private_key_path}. "
            "Download it from the Vonage application page and set VONAGE_PRIVATE_KEY_PATH."
        )
    return Path(s.private_key_path).read_text(encoding="utf-8")


def cloudflared_available(s: Settings) -> str | None:
    """Return the resolved cloudflared binary path, or None when not installed."""
    return shutil.which(s.cloudflared_bin)


def effective_tunnel_mode(s: Settings) -> str:
    """Resolve TUNNEL=auto: quick tunnel unless PUBLIC_URL is fixed or cloudflared is missing."""
    if s.tunnel_mode != "auto":
        return s.tunnel_mode
    if s.public_url:
        return "none"
    return "quick" if cloudflared_available(s) else "none"


settings = load_settings()
