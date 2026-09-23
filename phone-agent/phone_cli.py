#!/usr/bin/env python3
"""Optional unified entry: --provider twilio|plivo|vonage. Leaves ./call_phone intact.

twilio / plivo : one-shot Japanese TTS test call (delegates to the existing scripts)
vonage         : AI conversation call through vonage-agent (delegates to vonage-agent/vonage call)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from number_utils import normalize_to_e164


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="phone_cli")
    parser.add_argument("--provider", choices=("twilio", "plivo", "vonage"), required=True)
    parser.add_argument("to_number")
    parser.add_argument("objective", nargs="*", help="(vonage only) what the AI should accomplish")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--wait", action="store_true", help="(vonage only) wait for the call to end and print the transcript")
    args = parser.parse_args(argv)

    if args.provider == "vonage":
        # vonage-agent normalises numbers itself (also accepts domestic 090-... input).
        wrapper = Path(__file__).with_name("vonage-agent") / "vonage"
        if not wrapper.is_file():
            raise SystemExit(f"{wrapper} not found")
        cmd = [str(wrapper), "call", args.to_number, *args.objective]
        if args.dry_run:
            cmd.append("--dry-run")
        if args.wait:
            cmd.append("--wait")
        os.execv(str(wrapper), cmd)

    if args.objective or args.wait:
        parser.error("objective/--wait are only supported with --provider vonage")
    normalize_to_e164(args.to_number)

    # Delegate to existing scripts to avoid duplicating live-call logic.
    import runpy

    script = (
        Path(__file__).with_name("call_phone.py")
        if args.provider == "twilio"
        else Path(__file__).with_name("call_phone_plivo.py")
    )
    argv2 = []
    if args.dry_run:
        argv2.append("--dry-run")
    argv2.append(args.to_number)
    sys.argv = [str(script), *argv2]
    runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
