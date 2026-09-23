"""Load Vonage settings from env. Never log secret values."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    application_id: str
    private_key_path: str
    public_domain: str
    api_key: str
    api_secret: str
    port: int


def load_settings() -> Settings:
    domain = (os.environ.get("VONAGE_PUBLIC_DOMAIN") or "").strip()
    domain = domain.removeprefix("https://").removeprefix("http://").removeprefix("wss://").removeprefix("ws://")
    domain = domain.rstrip("/")
    return Settings(
        application_id=(os.environ.get("VONAGE_APPLICATION_ID") or "").strip(),
        private_key_path=(os.environ.get("VONAGE_PRIVATE_KEY_PATH") or "./private.key").strip(),
        public_domain=domain,
        api_key=(os.environ.get("VONAGE_API_KEY") or "").strip(),
        api_secret=(os.environ.get("VONAGE_API_SECRET") or "").strip(),
        port=int(os.environ.get("PORT") or "8090"),
    )


def private_key_file_ok(path: str) -> bool:
    p = Path(path)
    return p.is_file() and p.stat().st_size > 0
