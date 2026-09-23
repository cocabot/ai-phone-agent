"""Shared E.164 helpers for Twilio and Plivo CLIs."""

from __future__ import annotations

import re

E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize_to_e164(raw: str) -> str:
    number = raw.strip().replace(" ", "").replace("-", "")
    if number.startswith("00"):
        number = "+" + number[2:]
    if not E164_RE.match(number):
        raise SystemExit(
            f"Invalid number {raw!r}. Use E.164 like +819012345678"
        )
    return number
