"""Twilio outbound provider (mirrors existing call_phone.py behavior)."""

from __future__ import annotations

from twilio.rest import Client

from config import TwilioConfig, load_config
from twiml_scripts import build_test_say_twiml

TRIAL_VOICE_TTS_URL = (
    "https://webhooks.twilio.com/v1/Voice/Template/voice_text_to_speech"
)


def load_twilio_config() -> TwilioConfig:
    return load_config()


def place_test_call(to_e164: str, cfg: TwilioConfig | None = None) -> str:
    """Full/test path (may use params Trial disallows). Unchanged for non-Trial use."""
    cfg = cfg or load_config()
    client = Client(cfg.account_sid, cfg.auth_token)
    call = client.calls.create(
        to=to_e164,
        from_=cfg.from_number,
        twiml=build_test_say_twiml(),
    )
    return call.sid


def trial_create_params(to_e164: str) -> dict[str, str]:
    """Parameter NAMES/VALUES for Trial-minimal create. Does not place a call."""
    return {
        "to": to_e164,
        "url": TRIAL_VOICE_TTS_URL,
    }


def place_trial_call(to_e164: str, cfg: TwilioConfig | None = None) -> str:
    """
    Trial-only minimal Call create.
    Sends only: to, url (official voice_text_to_speech template).
    Does NOT send: from_/from, twiml, method, timeout, machine_detection, etc.
    """
    cfg = cfg or load_config()  # still needs SID/TOKEN for auth header
    client = Client(cfg.account_sid, cfg.auth_token)
    params = trial_create_params(to_e164)
    call = client.calls.create(**params)
    return call.sid
