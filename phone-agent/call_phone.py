#!/usr/bin/env python3
"""
Minimal Twilio outbound caller.

Usage:
  ./call_phone.py +81XXXXXXXXXX

Does not place a call unless credentials are set. The operator must explicitly
run this command (or ask the agent to run it after saying 発信して).
"""

from __future__ import annotations

import argparse
import sys

from twilio.rest import Client

from config import load_config
from number_utils import normalize_to_e164
from twiml_scripts import build_test_say_twiml
from providers.twilio_provider import place_trial_call, trial_create_params


def place_test_call(to_number: str) -> str:
    """Place a one-shot TTS test call. Returns Call SID."""
    cfg = load_config()
    to_e164 = normalize_to_e164(to_number)
    client = Client(cfg.account_sid, cfg.auth_token)
    twiml = build_test_say_twiml()

    call = client.calls.create(
        to=to_e164,
        from_=cfg.from_number,
        twiml=twiml,
    )
    return call.sid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="call_phone",
        description="Place a Twilio test call that speaks a fixed Japanese line.",
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
    parser.add_argument(
        "--trial",
        action="store_true",
        help="Use Trial-minimal params only (to + official voice_text_to_speech url).",
    )
    args = parser.parse_args(argv)

    to_e164 = normalize_to_e164(args.to_number)
    cfg = load_config()

    if args.dry_run:
        print("dry-run ok")
        if args.trial:
            params = trial_create_params(to_e164)
            print("mode=trial")
            print("planned_param_names=" + ",".join(params.keys()))
            print("planned_url_set=yes")
            print(f"to_tail=…{to_e164[-4:]}")
        else:
            print(f"from={cfg.from_number}")
            print(f"to={to_e164}")
            print("twiml preview:")
            print(build_test_say_twiml())
        return 0

    if args.trial:
        sid = place_trial_call(to_e164)
        print(f"call placed mode=trial sid={sid} to_tail=…{to_e164[-4:]}")
        return 0

    sid = place_test_call(to_e164)
    print(f"call placed sid={sid} to={to_e164}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
