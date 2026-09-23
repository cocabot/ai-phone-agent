#!/usr/bin/env python3
"""
vonage-agent control CLI (wrapped by ./vonage).

  ./vonage doctor                         check config, keys, cloudflared, Vonage app, Gemini
  ./vonage serve [--detach]               start server (+ quick tunnel + webhook auto-sync)
  ./vonage stop | logs [-f] | status      manage the detached server
  ./vonage call +81... "目的" [--dry-run] [--wait]
  ./vonage status <uuid|call_ref>         call status, outcome and transcript
  ./vonage hangup <uuid|call_ref>
  ./vonage sync-webhooks [--url URL]      point the Vonage application at a URL now

Live calls cost money and ring a real phone. Use --dry-run first.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / ".runtime"
STATE_FILE = RUNTIME_DIR / "state.json"
PID_FILE = RUNTIME_DIR / "server.pid"
LOG_FILE = RUNTIME_DIR / "server.log"


# --------------------------------------------------------------------------- helpers


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def read_state() -> dict[str, Any] | None:
    if not STATE_FILE.is_file():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _configured_local_base_url() -> str:
    try:
        from config import settings

        return settings.local_base_url
    except SystemExit:
        return "http://127.0.0.1:8090"


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Api:
    """Client for the local admin API of a running server."""

    def __init__(self) -> None:
        state = read_state() or {}
        self.base_url = os.environ.get("AGENT_BASE_URL") or state.get("local_base_url") or _configured_local_base_url()
        self.token = os.environ.get("ADMIN_TOKEN") or state.get("admin_token") or ""
        self.pid = state.get("pid")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def request(self, method: str, path: str, *, json_body: Any = None, params: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
        try:
            r = httpx.request(method, f"{self.base_url}{path}", headers=self._headers(), json=json_body, params=params, timeout=timeout)
        except httpx.HTTPError as exc:
            raise SystemExit(
                f"cannot reach vonage-agent at {self.base_url} ({type(exc).__name__}). "
                "Start it with: ./vonage serve --detach"
            ) from exc
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text}
        if r.status_code >= 400:
            detail = data.get("detail") if isinstance(data, dict) else data
            raise SystemExit(f"error {r.status_code}: {detail}")
        return data

    def health(self, timeout: float = 5.0) -> dict[str, Any] | None:
        try:
            r = httpx.get(f"{self.base_url}/health", timeout=timeout)
            return r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None


def fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def print_call(call: dict[str, Any], *, transcript: bool = True) -> None:
    print(f"uuid:       {call.get('uuid') or '-'}")
    print(f"call_ref:   {call.get('call_ref')}")
    print(f"direction:  {call.get('direction')}  to={call.get('to')}  from={call.get('from')}")
    print(f"status:     {call.get('status')}  active={call.get('active')}  end_reason={call.get('end_reason') or '-'}")
    print(f"created:    {fmt_ts(call.get('created_at'))}  answered: {fmt_ts(call.get('answered_at'))}  duration: {call.get('duration_seconds') or '-'}s")
    print(f"objective:  {call.get('objective')}")
    if call.get("instructions"):
        print(f"instructions: {call['instructions']}")
    if call.get("error"):
        print(f"error:      {call['error']}")
    outcome = call.get("outcome")
    if outcome:
        print(f"outcome:    {outcome.get('outcome')}")
        print(f"summary:    {outcome.get('summary')}")
    usage = call.get("usage") or {}
    if usage:
        print("usage:      " + ", ".join(f"{k}={v}" for k, v in usage.items()))
    if call.get("vonage"):
        v = call["vonage"]
        print(f"vonage:     status={v.get('status')} duration={v.get('duration') or '-'}s price={v.get('price') or '-'} start={v.get('start_time') or '-'} end={v.get('end_time') or '-'}")
    if call.get("vonage_error"):
        print(f"vonage:     lookup failed: {call['vonage_error']}")
    if transcript and call.get("transcript_text"):
        print("--- transcript ---")
        print(call["transcript_text"])


def print_health(h: dict[str, Any]) -> None:
    tunnel = h.get("tunnel") or {}
    webhooks = h.get("webhooks") or {}
    vonage = h.get("vonage") or {}
    gemini = h.get("gemini") or {}
    probe = (h.get("public_url_probe") or {}).get("status")
    print(f"server:     ok  v{h.get('version')}  uptime={h.get('uptime_seconds')}s  active_calls={h.get('calls_active')}")
    print(f"public url: {h.get('public_url') or '(none yet)'}  source={h.get('public_url_source')}" + (f"  reachability={probe}" if probe and probe != "unknown" else ""))
    print(f"tunnel:     mode={tunnel.get('mode')}  status={tunnel.get('status', '-')}  restarts={tunnel.get('restarts', 0)}" + (f"  last_error={tunnel.get('last_error')}" if tunnel.get("last_error") else ""))
    print(f"webhooks:   {webhooks.get('status')}  base={webhooks.get('base_url') or '-'}" + (f"  error={webhooks.get('error')}" if webhooks.get("error") else ""))
    print(f"vonage:     app_id={'set' if vonage.get('application_id_set') else 'MISSING'}  api_key={'set' if vonage.get('api_key_set') else 'MISSING'}  private_key={'ok' if vonage.get('private_key_ok') else 'MISSING'}  from={vonage.get('from_number') or 'MISSING'}{' (trial caller id)' if vonage.get('trial_caller_id') else ''}")
    print(f"gemini:     model={gemini.get('model')}  voice={gemini.get('voice')}  api_key={'set' if gemini.get('api_key_set') else 'MISSING'}")


# --------------------------------------------------------------------------- commands


def cmd_serve(args: argparse.Namespace) -> int:
    if args.public_url:
        os.environ["PUBLIC_URL"] = args.public_url
    if args.tunnel:
        os.environ["TUNNEL"] = args.tunnel
    if args.port:
        os.environ["PORT"] = str(args.port)
    if args.host:
        os.environ["HOST"] = args.host

    if args.detach:
        return _serve_detached(args)

    import uvicorn

    from config import settings

    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False, log_level="info")
    return 0


def _serve_detached(args: argparse.Namespace) -> int:
    existing = read_state()
    if existing and pid_alive(existing.get("pid")):
        print(f"already running (pid {existing['pid']}) at {existing.get('local_base_url')}")
        return _wait_and_report(Api(), args.wait)

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(BASE_DIR / "agentctl.py"), "serve"]
    if args.public_url:
        cmd += ["--public-url", args.public_url]
    if args.tunnel:
        cmd += ["--tunnel", args.tunnel]
    if args.port:
        cmd += ["--port", str(args.port)]
    if args.host:
        cmd += ["--host", args.host]

    with LOG_FILE.open("ab") as log_fh:
        log_fh.write(f"\n===== vonage-agent start {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n".encode())
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            cwd=BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    print(f"started pid {proc.pid}; logs: {LOG_FILE}")

    api = Api()
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            print("server exited immediately; last log lines:")
            print(_tail(LOG_FILE, 30))
            return 1
        api = Api()
        if api.health(timeout=2):
            break
        time.sleep(0.5)
    else:
        print("server did not answer /health within 20s; see logs")
        return 1
    return _wait_and_report(api, args.wait)


def _wait_and_report(api: Api, wait_seconds: int) -> int:
    """Wait for the tunnel URL and webhook sync (bounded), then print the status block."""
    deadline = time.time() + max(wait_seconds, 0)
    health: dict[str, Any] | None = None
    while True:
        health = api.health()
        if health:
            tunnel_mode = (health.get("tunnel") or {}).get("mode")
            webhook_status = (health.get("webhooks") or {}).get("status")
            settled = bool(health.get("public_url")) and webhook_status in {"ok", "skipped", "error"}
            if tunnel_mode == "none" and not health.get("public_url"):
                settled = True
            if settled or time.time() >= deadline:
                break
        elif time.time() >= deadline:
            break
        time.sleep(1.0)
    if not health:
        print("server is not answering /health")
        return 1
    print_health(health)
    if not health.get("public_url"):
        print("note: public URL not available yet; run ./vonage status again in a few seconds")
    return 0


def _tail(path: Path, lines: int) -> str:
    if not path.is_file():
        return "(no log file)"
    data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(data[-lines:])


def cmd_stop(_: argparse.Namespace) -> int:
    state = read_state() or {}
    pid = state.get("pid")
    if not pid and PID_FILE.is_file():
        try:
            pid = int(PID_FILE.read_text().strip())
        except ValueError:
            pid = None
    if not pid or not pid_alive(pid):
        print("server is not running")
        PID_FILE.unlink(missing_ok=True)
        STATE_FILE.unlink(missing_ok=True)
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not pid_alive(pid):
            break
        time.sleep(0.2)
    else:
        os.kill(pid, signal.SIGKILL)
    PID_FILE.unlink(missing_ok=True)
    STATE_FILE.unlink(missing_ok=True)
    print(f"stopped pid {pid}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    if args.follow:
        if not LOG_FILE.is_file():
            print("(no log file yet)")
            return 1
        os.execvp("tail", ["tail", "-n", str(args.lines), "-f", str(LOG_FILE)])
    print(_tail(LOG_FILE, args.lines))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    api = Api()
    if args.key:
        data = api.request("GET", f"/calls/{args.key}", params={"refresh": "true"} if args.refresh else None)
        if args.json:
            _print_json(data)
        else:
            print_call(data)
        return 0
    health = api.health()
    if not health:
        print(f"server not running (expected at {api.base_url}). Start with: ./vonage serve --detach")
        return 1
    if args.json:
        _print_json(api.request("GET", "/admin/status") if api.token else health)
        return 0
    print_health(health)
    if api.token:
        calls = api.request("GET", "/calls", params={"limit": 5}).get("calls", [])
        if calls:
            print("--- recent calls ---")
            for c in calls:
                outcome = (c.get("outcome") or {}).get("outcome")
                print(f"{fmt_ts(c.get('created_at'))}  {c.get('uuid') or c.get('call_ref')}  {c.get('direction')} {c.get('to')}  {c.get('status')}" + (f"  outcome={outcome}" if outcome else ""))
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    api = Api()
    objective = " ".join(args.objective).strip() if args.objective else ""
    if not objective:
        raise SystemExit("objective is required, e.g. ./vonage call +819012345678 \"明日19時に2名で予約できるか確認\"")
    body = {"to": args.to, "objective": objective, "instructions": args.instructions, "dry_run": args.dry_run}
    data = api.request("POST", "/calls", json_body=body, timeout=60)
    if args.dry_run:
        if args.json:
            _print_json(data)
            return 0 if data.get("ok") else 1
        print("dry-run " + ("ok" if data.get("ok") else "NOT READY"))
        for p in data.get("problems") or []:
            print(f"  problem: {p}")
        print(f"to={data.get('to')} from={data.get('from')} public_url={data.get('public_url')} model={data.get('gemini_model')}")
        print(f"webhooks: {(data.get('webhooks') or {}).get('status')} base={(data.get('webhooks') or {}).get('base_url')}")
        print("ncco preview:")
        _print_json(data.get("ncco"))
        return 0 if data.get("ok") else 1

    if args.json and not args.wait:
        _print_json(data)
        return 0
    print(f"call placed uuid={data.get('uuid')} status={data.get('status')} to={data.get('to')}")
    if not args.wait:
        print(f"track it with: ./vonage status {data.get('uuid')}")
        return 0
    return _wait_for_call(api, data.get("uuid") or data.get("call_ref"), args.json, args.timeout)


def _wait_for_call(api: Api, key: str, as_json: bool, timeout: int) -> int:
    deadline = time.time() + timeout
    last_status = None
    while True:
        call = api.request("GET", f"/calls/{key}")
        status = call.get("status")
        if status != last_status and not as_json:
            print(f"  [{time.strftime('%H:%M:%S')}] {status}")
            last_status = status
        if not call.get("active") or time.time() >= deadline:
            break
        time.sleep(2)
    if as_json:
        _print_json(call)
    else:
        print()
        print_call(call)
    return 0


def cmd_hangup(args: argparse.Namespace) -> int:
    data = Api().request("POST", f"/calls/{args.key}/hangup")
    print(f"hangup requested uuid={data.get('uuid')} status={data.get('status')}")
    return 0


def cmd_sync_webhooks(args: argparse.Namespace) -> int:
    api = Api()
    if api.health() and api.token:
        result = api.request("POST", "/admin/sync-webhooks", json_body={"url": args.url, "force": args.force}, timeout=60)
    else:
        from config import normalize_public_url, settings
        from vonage_api import VonageClient, VonageError

        base_url = normalize_public_url(args.url or settings.public_url)
        if not base_url:
            raise SystemExit("server is not running: pass --url https://your-host (or set PUBLIC_URL)")
        try:
            sync = VonageClient(settings).sync_webhooks(base_url, force=args.force)
        except VonageError as exc:
            raise SystemExit(str(exc)) from exc
        result = {"status": "ok", "base_url": sync.base_url, "changed": sync.changed, "webhooks": sync.webhooks, "previous": sync.previous}
    if args.json:
        _print_json(result)
    else:
        print(f"webhooks {result.get('status')} base={result.get('base_url')} changed={result.get('changed')}")
        for name, url in (result.get("webhooks") or {}).items():
            print(f"  {name}: {url}")
        if result.get("error"):
            print(f"  error: {result['error']}")
    return 0 if result.get("status") == "ok" else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    from config import (
        VONAGE_TRIAL_CALLER_ID,
        cloudflared_available,
        effective_tunnel_mode,
        private_key_file_ok,
        settings,
    )

    failures = 0
    warnings = 0

    def ok(msg: str) -> None:
        print(f"PASS  {msg}")

    def warn(msg: str) -> None:
        nonlocal warnings
        warnings += 1
        print(f"WARN  {msg}")

    def fail(msg: str) -> None:
        nonlocal failures
        failures += 1
        print(f"FAIL  {msg}")

    env_file = BASE_DIR / ".env"
    (ok if env_file.is_file() else warn)(f".env {'found' if env_file.is_file() else 'missing (cp .env.example .env)'}")

    # Vonage application / voice auth
    if settings.application_id:
        ok("VONAGE_APPLICATION_ID set")
    else:
        fail("VONAGE_APPLICATION_ID missing")
    key_ok = bool(settings.private_key_inline) or private_key_file_ok(settings.private_key_path)
    if key_ok:
        ok(f"private key available ({'inline' if settings.private_key_inline else settings.private_key_path})")
        if settings.application_id:
            try:
                from vonage_api import VonageClient

                VonageClient(settings).jwt()
                ok("JWT generation with private key works")
            except Exception as exc:  # noqa: BLE001
                fail(f"JWT generation failed: {exc}")
    else:
        fail(f"private key not found at {settings.private_key_path} (download from the Vonage application page)")

    if settings.from_number:
        if settings.from_number == VONAGE_TRIAL_CALLER_ID:
            ok(f"VONAGE_FROM_NUMBER={VONAGE_TRIAL_CALLER_ID} (trial caller id: only your registered number can be called)")
        else:
            ok(f"VONAGE_FROM_NUMBER set ({settings.from_number})")
    else:
        fail("VONAGE_FROM_NUMBER missing (trial accounts: 123456789)")

    # Application API (webhook auto-sync)
    if settings.api_key and settings.api_secret:
        ok("VONAGE_API_KEY / VONAGE_API_SECRET set (webhook auto-sync enabled)")
        if settings.application_id and not args.offline:
            try:
                from vonage_api import VonageClient, current_voice_webhooks

                app_data = VonageClient(settings).get_application()
                ok(f"Vonage application reachable: {app_data.get('name')}")
                for name, url in current_voice_webhooks(app_data).items():
                    print(f"      {name}: {url}")
            except Exception as exc:  # noqa: BLE001
                fail(f"Vonage Application API: {exc}")
    else:
        warn("VONAGE_API_KEY / VONAGE_API_SECRET missing: webhooks must be updated manually in the dashboard")

    # Tunnel
    mode = effective_tunnel_mode(settings)
    cf = cloudflared_available(settings)
    if settings.public_url:
        ok(f"PUBLIC_URL fixed: {settings.public_url} (tunnel mode={mode})")
    elif cf:
        version = "unknown"
        try:
            out = subprocess.run([cf, "--version"], capture_output=True, text=True, timeout=10, check=False)  # noqa: S603
            version = (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr).strip() else version
        except (OSError, subprocess.SubprocessError):
            pass
        ok(f"cloudflared found: {cf} ({version}); quick tunnel mode={mode}")
    else:
        fail("no PUBLIC_URL and cloudflared not found: install cloudflared or set PUBLIC_URL")

    # Gemini
    if settings.gemini_api_key:
        ok(f"GEMINI_API_KEY set; model={settings.gemini_model} voice={settings.gemini_voice}")
        if not args.offline:
            try:
                from google import genai

                client = genai.Client(api_key=settings.gemini_api_key)
                try:
                    client.models.get(model=settings.gemini_model)
                    ok(f"Gemini model {settings.gemini_model} is visible to this key")
                except Exception as exc:  # noqa: BLE001
                    warn(f"could not fetch model {settings.gemini_model}: {exc}")
                    live_models = [m.name for m in client.models.list() if "live" in (m.name or "").lower() or "audio" in (m.name or "").lower()]
                    if live_models:
                        print("      live-capable models visible to this key:")
                        for name in live_models[:15]:
                            print(f"        {name.removeprefix('models/')}")
            except Exception as exc:  # noqa: BLE001
                fail(f"Gemini API: {exc}")
    else:
        fail("GEMINI_API_KEY missing (create one in Google AI Studio)")

    if settings.allowed_to_numbers:
        ok(f"ALLOWED_TO_NUMBERS allowlist active ({len(settings.allowed_to_numbers)} numbers)")
    else:
        warn("ALLOWED_TO_NUMBERS not set: any non-blocked number can be dialled (set it to your own number while testing)")

    state = read_state()
    if state and pid_alive(state.get("pid")):
        ok(f"server running (pid {state['pid']}) public_url={state.get('public_url') or '(pending)'}")
    else:
        print("INFO  server not running (./vonage serve --detach)")

    print()
    print(f"{failures} failure(s), {warnings} warning(s)")
    return 1 if failures else 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vonage", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the server (foreground unless --detach)")
    p.add_argument("--detach", "-d", action="store_true", help="run in the background (logs in .runtime/server.log)")
    p.add_argument("--tunnel", choices=["auto", "quick", "none"], help="override TUNNEL")
    p.add_argument("--public-url", help="fixed public URL (disables the quick tunnel)")
    p.add_argument("--port", type=int)
    p.add_argument("--host")
    p.add_argument("--wait", type=int, default=90, help="seconds to wait for tunnel + webhook sync when detaching")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("stop", help="stop the detached server")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("logs", help="show server logs")
    p.add_argument("-n", "--lines", type=int, default=80)
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("status", help="server status, or one call's status/transcript")
    p.add_argument("key", nargs="?", help="call uuid or call_ref")
    p.add_argument("--refresh", action="store_true", help="also fetch the live call record from Vonage")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("call", help="place an outbound AI call")
    p.add_argument("to", help="destination, e.g. +819012345678")
    p.add_argument("objective", nargs="*", help="what the AI should accomplish")
    p.add_argument("--instructions", "-i", help="extra facts/constraints for the AI")
    p.add_argument("--dry-run", action="store_true", help="validate and show the NCCO without calling")
    p.add_argument("--wait", "-w", action="store_true", help="wait for the call to end and print the transcript")
    p.add_argument("--timeout", type=int, default=1200, help="max seconds to wait with --wait")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_call)

    p = sub.add_parser("hangup", help="end a call")
    p.add_argument("key", help="call uuid or call_ref")
    p.set_defaults(func=cmd_hangup)

    p = sub.add_parser("sync-webhooks", help="update the Vonage application webhooks")
    p.add_argument("--url", help="public base URL (default: the running server's URL / PUBLIC_URL)")
    p.add_argument("--force", action="store_true", help="PUT even if the URLs already match")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sync_webhooks)

    p = sub.add_parser("doctor", help="check the local setup")
    p.add_argument("--offline", action="store_true", help="skip network checks")
    p.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    os.chdir(BASE_DIR)
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
