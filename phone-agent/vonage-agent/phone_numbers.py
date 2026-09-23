"""Phone number normalisation and destination safety checks."""

from __future__ import annotations

import re

E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")

# Japan: emergency and special-rate ranges that this agent must never dial.
JP_BLOCKED_EXACT = {"+81110", "+81118", "+81119"}
JP_BLOCKED_PREFIXES = ("+81570", "+81990")


class NumberError(ValueError):
    pass


def normalize_to_e164(raw: str, *, default_country: str = "81") -> str:
    """Return an E.164 number (with ``+``).

    Accepts ``+819012345678``, ``819012345678``, ``0081...``, and Japanese
    domestic ``090-1234-5678`` style input (leading ``0`` is replaced by the
    default country code).
    """
    n = re.sub(r"[\s()\-.]", "", raw or "")
    if not n:
        raise NumberError("Phone number is empty")
    if n.lstrip("+") in {"110", "118", "119"}:
        raise NumberError("Emergency-service numbers are blocked")
    if n.startswith("00"):
        n = "+" + n[2:]
    elif n.startswith("0"):
        n = f"+{default_country}" + n[1:]
    elif not n.startswith("+"):
        n = "+" + n
    if not E164_RE.match(n):
        raise NumberError(f"Invalid number {raw!r}. Use E.164 like +819012345678")
    return n


def check_destination_allowed(e164: str, allowed: tuple[str, ...] | list[str] = ()) -> None:
    """Raise NumberError when the destination must not be dialled."""
    if e164 in JP_BLOCKED_EXACT:
        raise NumberError("Emergency-service numbers are blocked")
    if e164.startswith(JP_BLOCKED_PREFIXES):
        raise NumberError("0570/0990 special-rate numbers are blocked")
    if allowed:
        normalized_allow = {normalize_to_e164(a) for a in allowed}
        if e164 not in normalized_allow:
            raise NumberError(
                f"{mask_number(e164)} is not in ALLOWED_TO_NUMBERS. "
                "Add it to .env or clear ALLOWED_TO_NUMBERS to disable the allowlist."
            )


def to_vonage_number(e164: str) -> str:
    """Vonage expects E.164 digits without the leading ``+``."""
    return e164.lstrip("+")


def mask_number(value: str | None) -> str:
    if not value:
        return "-"
    digits = "".join(c for c in value if c.isdigit())
    if len(digits) < 4:
        return "…"
    return f"…{digits[-4:]}"
