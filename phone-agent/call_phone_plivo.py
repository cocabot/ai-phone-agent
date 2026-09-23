#!/usr/bin/env python3
"""
Plivo outbound test caller (parallel to Twilio call_phone.py).

Usage:
  ./call_phone_plivo --dry-run +81XXXXXXXXXX
  ./call_phone_plivo +81XXXXXXXXXX

Do not run a live call unless the operator explicitly said 「Plivoで発信して」.
"""

from __future__ import annotations

import argparse
import sys

from number_utils import normalize_to_e164
from providers.plivo_provider import (
    TEST_MESSAGE_JA,
    build_plivo_speak_xml,
    load_plivo_config,
    place_test_call,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="call_phone_plivo",
        description="Place a Plivo test call that speaks a fixed Japanese line.",
    )
    parser.add_argument(
        "to_number",
        help="Destination in E.164, e.g. +819012345678",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config and number; do not place a call.",
    )
    args = parser.parse_args(argv)

    to_e164 = normalize_to_e164(args.to_number)

    if args.dry_run:
        # answer_url optional in dry-run so secrets can be checked stepwise
        cfg = load_plivo_config(require_answer_url=False)
        print("provider=plivo")
        print("dry-run ok")
        print(f"from={cfg.from_number}")
        print(f"to={to_e164}")
        print(f"answer_url={cfg.answer_url or '(not set — required for live call)'}")
        print("message=")
        print(TEST_MESSAGE_JA)
        print("xml preview=")
        print(build_plivo_speak_xml())
        return 0

    cfg = load_plivo_config(require_answer_url=True)
    print("provider=plivo")
    print(f"placing call from={cfg.from_number} to={to_e164}")
    call_id = place_test_call(to_e164, cfg)
    print(f"call placed provider=plivo call_id={call_id} to={to_e164}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
