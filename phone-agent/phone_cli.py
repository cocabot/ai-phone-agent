#!/usr/bin/env python3
"""Optional unified entry: --provider twilio|plivo. Leaves ./call_phone intact."""

from __future__ import annotations

import argparse
import sys

from number_utils import normalize_to_e164


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="phone_cli")
    parser.add_argument("--provider", choices=("twilio", "plivo"), required=True)
    parser.add_argument("to_number")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    # Delegate to existing scripts to avoid duplicating live-call logic.
    import runpy
    from pathlib import Path

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
    try:
        raise SystemExit(main())
    except SystemExit as exc:
        raise
