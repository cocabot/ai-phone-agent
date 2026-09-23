"""Plivo outbound provider.

Unlike Twilio (inline TwiML), Plivo requires a public answer_url that returns XML.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv
from plivo import RestClient
from plivo.exceptions import PlivoRestError

load_dotenv()

TEST_MESSAGE_JA = (
    "これはAI電話システムのPlivoテストです。正常に電話を発信できました。"
)


@dataclass(frozen=True)
class PlivoConfig:
    auth_id: str
    auth_token: str
    from_number: str
    answer_url: str


def load_plivo_config(*, require_answer_url: bool = True) -> PlivoConfig:
    auth_id = os.environ.get("PLIVO_AUTH_ID", "").strip()
    auth_token = os.environ.get("PLIVO_AUTH_TOKEN", "").strip()
    from_number = os.environ.get("PLIVO_FROM_NUMBER", "").strip()
    answer_url = os.environ.get("PLIVO_ANSWER_URL", "").strip()

    missing = [
        name
        for name, value in (
            ("PLIVO_AUTH_ID", auth_id),
            ("PLIVO_AUTH_TOKEN", auth_token),
            ("PLIVO_FROM_NUMBER", from_number),
        )
        if not value
    ]
    if require_answer_url and not answer_url:
        missing.append("PLIVO_ANSWER_URL")
    if missing:
        raise SystemExit(
            "Missing env vars: "
            + ", ".join(missing)
            + ". See .env.example (Plivo needs a public answer_url)."
        )

    return PlivoConfig(
        auth_id=auth_id,
        auth_token=auth_token,
        from_number=from_number,
        answer_url=answer_url,
    )


def build_plivo_speak_xml(message: str = TEST_MESSAGE_JA) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        '  <Speak language="ja-JP" voice="WOMAN">\n'
        f'    {message}\n'
        '  </Speak>\n'
        "</Response>\n"
    )


def place_test_call(to_e164: str, cfg: PlivoConfig | None = None) -> str:
    cfg = cfg or load_plivo_config(require_answer_url=True)
    client = RestClient(cfg.auth_id, cfg.auth_token)
    try:
        response = client.calls.create(
            from_=cfg.from_number,
            to_=to_e164,
            answer_url=cfg.answer_url,
            answer_method="GET",
        )
    except PlivoRestError as exc:
        raise SystemExit(f"Plivo API error: {exc}") from exc

    # SDK returns a response object with request_uuid / call_uuid fields
    call_id = (
        getattr(response, "request_uuid", None)
        or getattr(response, "call_uuid", None)
        or str(response)
    )
    return str(call_id)
