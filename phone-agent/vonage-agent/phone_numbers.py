"""Destination number normalization and safety rules."""

from __future__ import annotations

import re

E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")

# Emergency / special service numbers that must never be dialled by the agent.
JP_BLOCKED_EXACT = {"+81110", "+81118", "+81119", "+81171", "+81177", "+81104"}
JP_BLOCKED_PREFIXES = ("+81570", "+81990", "+81180")


class NumberError(ValueError):
    pass


def normalize(raw: str, default_country: str = "81") -> str:
    """'090-1234-5678' / '0081 90…' / '+8190…' -> '+819012345678'."""
    n = re.sub(r"[\s()\-.]", "", raw.strip())
    if n.startswith("00"):
        n = "+" + n[2:]
    elif n.startswith("0") and default_country:
        n = f"+{default_country}{n[1:]}"
    elif not n.startswith("+"):
        n = "+" + n
    if not E164_RE.match(n):
        raise NumberError(f"電話番号の形式が不正です: {raw!r}（例: +819012345678 / 09012345678）")
    return n


def check_destination(
    number: str,
    allowed_country_codes: tuple[str, ...] = ("81",),
    allowlist: tuple[str, ...] = (),
) -> str:
    """Return the normalized number or raise NumberError."""
    n = normalize(number)
    if allowed_country_codes and not any(n.startswith("+" + cc) for cc in allowed_country_codes):
        raise NumberError(
            f"{n} は許可された国番号 ({', '.join(allowed_country_codes)}) 以外です（ALLOWED_COUNTRY_CODES）"
        )
    if n.startswith("+81"):
        if n in JP_BLOCKED_EXACT:
            raise NumberError("緊急通報・特番には発信できません")
        if any(n.startswith(p) for p in JP_BLOCKED_PREFIXES):
            raise NumberError("0570 / 0990 / 0180 などの特殊番号には発信できません")
    if allowlist and n.lstrip("+") not in allowlist:
        raise NumberError(f"{mask(n)} は CALL_ALLOWLIST に含まれていません")
    return n


def mask(value: str | None) -> str:
    if not value:
        return "-"
    digits = "".join(c for c in value if c.isdigit())
    if len(digits) < 4:
        return "…"
    return f"…{digits[-4:]}"
