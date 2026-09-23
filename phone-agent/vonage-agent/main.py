"""
vonage-agent: Vonage Voice API <-> Gemini Live phone agent.

Endpoints
  GET  /health                     status (public URL, tunnel, webhook sync, config checks)
  GET  /webhooks/answer            NCCO for inbound calls (websocket connect)
  POST /webhooks/event             Vonage call events -> call registry
  GET  /webhooks/fallback          NCCO used when the answer webhook fails
  WS   /socket                     Vonage audio <-> Gemini Live bridge

Admin API (Authorization: Bearer <ADMIN_TOKEN>, see .runtime/state.json)
  POST /calls                      place an outbound call
  GET  /calls                      recent calls
  GET  /calls/{uuid|call_ref}      call detail incl. transcript and outcome
  POST /calls/{uuid|call_ref}/hangup
  POST /admin/sync-webhooks        re-point the Vonage application webhooks

On startup the server (optionally) launches a Cloudflare quick tunnel, learns
its public URL and updates the Vonage application's answer/event/fallback
webhooks through the Application API. When cloudflared restarts and the URL
changes, the webhooks are updated again automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from calls import CallRecord, CallRegistry
from config import (
    RUNTIME_DIR,
    Settings,
    effective_tunnel_mode,
    normalize_public_url,
    private_key_file_ok,
    settings,
)
from gemini_bridge import GeminiVoiceBridge
from phone_numbers import NumberError, check_destination_allowed, mask_number, normalize_to_e164
from tunnel import QuickTunnel
from vonage_api import VonageClient, VonageError, verify_signed_webhook

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vonage-agent")

VERSION = "0.2.0"
STATE_FILE = RUNTIME_DIR / "state.json"
GOODBYE_TEXT = "通話を終了します。失礼いたします。"


# --------------------------------------------------------------------------- state


class AppState:
    def __init__(self, s: Settings):
        self.settings = s
        CallRecord.stale_after_seconds = s.call_max_seconds + 180
        self.registry = CallRegistry()
        self.vonage = VonageClient(s)
        self.started_at = time.time()
        self.public_url: str = s.public_url
        self.public_url_source: str = "env" if s.public_url else "none"
        self.tunnel: QuickTunnel | None = None
        self.tunnel_mode = effective_tunnel_mode(s)
        self.webhook_sync: dict[str, Any] = {"status": "never", "base_url": None, "at": None, "error": None, "webhooks": {}}
        self.public_url_probe: dict[str, Any] = {"status": "unknown", "url": None, "at": None}
        self.loop: asyncio.AbstractEventLoop | None = None
        self._sync_lock = asyncio.Lock()
        self._background: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Any) -> None:
        """Run a coroutine in the background while keeping a strong reference to the task."""
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # --- public url --------------------------------------------------------

    def set_public_url(self, url: str, source: str) -> None:
        self.public_url = normalize_public_url(url)
        self.public_url_source = source
        write_state_file(self)

    # --- webhook sync ------------------------------------------------------

    async def sync_webhooks(self, base_url: str, *, force: bool = False) -> dict[str, Any]:
        s = self.settings
        if not (s.api_key and s.api_secret and s.application_id):
            self.webhook_sync.update(
                status="skipped",
                base_url=base_url,
                at=time.time(),
                error="VONAGE_API_KEY / VONAGE_API_SECRET / VONAGE_APPLICATION_ID required for automatic webhook sync",
            )
            log.warning("webhook sync skipped: %s", self.webhook_sync["error"])
            return self.webhook_sync

        async with self._sync_lock:
            if self.public_url and base_url != self.public_url:
                # The tunnel moved again while we were queued; the newer sync task wins.
                return self.webhook_sync
            self.webhook_sync.update(status="syncing", base_url=base_url, error=None)
            last_error: str | None = None
            for attempt in range(1, 4):
                if self.public_url and base_url != self.public_url:
                    return self.webhook_sync
                try:
                    result = await asyncio.to_thread(self.vonage.sync_webhooks, base_url, force=force)
                    self.webhook_sync.update(
                        status="ok",
                        base_url=result.base_url,
                        at=time.time(),
                        error=None,
                        changed=result.changed,
                        webhooks=result.webhooks,
                    )
                    log.info(
                        "vonage webhooks %s -> %s",
                        "updated" if result.changed else "already current",
                        result.webhooks.get("answer_url"),
                    )
                    return self.webhook_sync
                except (VonageError, httpx.HTTPError, SystemExit) as exc:
                    last_error = str(exc)
                    log.warning("webhook sync attempt %s failed: %s", attempt, last_error)
                    await asyncio.sleep(2 * attempt)
            self.webhook_sync.update(status="error", at=time.time(), error=last_error)
            return self.webhook_sync

    async def handle_new_public_url(self, url: str, source: str) -> None:
        self.set_public_url(url, source)
        log.info("public url is now %s (%s)", self.public_url, source)
        if self.settings.public_url_wait_seconds > 0:
            self.spawn(self.probe_public_url(self.public_url))
        if self.settings.sync_webhooks:
            await self.sync_webhooks(self.public_url)

    async def probe_public_url(self, url: str) -> None:
        """Informational: confirm the public URL answers from the internet (DNS can lag a few seconds)."""
        self.public_url_probe = {"status": "checking", "url": url, "at": time.time()}
        reachable = await wait_until_reachable(f"{url}/health", timeout=self.settings.public_url_wait_seconds)
        if self.public_url != url:
            return  # superseded by a newer URL
        self.public_url_probe = {"status": "reachable" if reachable else "unreachable", "url": url, "at": time.time()}

    # --- call helpers ------------------------------------------------------

    async def hangup(self, record: CallRecord, reason: str) -> None:
        if not record.uuid:
            record.mark_ended(reason)
            return
        try:
            await asyncio.to_thread(self.vonage.hangup_call, record.uuid)
            record.mark_ended(reason)
            log.info("hung up uuid=%s reason=%s", record.uuid, reason)
        except (VonageError, httpx.HTTPError, SystemExit) as exc:
            record.error = f"hangup failed: {exc}"
            log.warning("hangup failed uuid=%s: %s", record.uuid, exc)

    async def send_dtmf(self, record: CallRecord, digits: str) -> bool:
        if not record.uuid:
            return False
        try:
            await asyncio.to_thread(self.vonage.send_dtmf, record.uuid, digits)
            log.info("dtmf sent uuid=%s digits=%s", record.uuid, digits)
            return True
        except (VonageError, httpx.HTTPError, SystemExit) as exc:
            log.warning("dtmf failed uuid=%s: %s", record.uuid, exc)
            return False

    def snapshot(self, *, public: bool = False) -> dict[str, Any]:
        """Status document. ``public=True`` (unauthenticated /health) masks numbers."""
        s = self.settings
        return {
            "status": "ok",
            "service": "vonage-agent",
            "version": VERSION,
            "uptime_seconds": round(time.time() - self.started_at),
            "public_url": self.public_url or None,
            "public_url_source": self.public_url_source,
            "public_url_probe": self.public_url_probe,
            "tunnel": {"mode": self.tunnel_mode, **(self.tunnel.snapshot() if self.tunnel else {})},
            "webhooks": self.webhook_sync,
            "vonage": {
                "application_id_set": bool(s.application_id),
                "api_key_set": bool(s.api_key and s.api_secret),
                "private_key_ok": bool(s.private_key_inline) or private_key_file_ok(s.private_key_path),
                "from_number": (mask_number(s.from_number) if public and not s.is_trial_caller_id else s.from_number) or None,
                "trial_caller_id": s.is_trial_caller_id,
                "audio_rate": s.audio_rate,
            },
            "gemini": {"api_key_set": bool(s.gemini_api_key), "model": s.gemini_model, "voice": s.gemini_voice},
            "security": {
                "ws_auth": s.ws_auth,
                "signed_webhooks": bool(s.signature_secret),
                "allowlist": len(s.allowed_to_numbers) if public else list(s.allowed_to_numbers),
            },
            "limits": {"call_max_seconds": s.call_max_seconds, "max_concurrent_calls": s.max_concurrent_calls},
            "calls_active": len(self.registry.active()),
        }


def write_state_file(state: AppState) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "local_base_url": state.settings.local_base_url,
        "admin_token": state.settings.admin_token,
        "public_url": state.public_url or None,
        "started_at": state.started_at,
    }
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


async def wait_until_reachable(url: str, *, timeout: float = 45.0) -> bool:
    """Poll ``url`` until it answers (new trycloudflare hostnames take a few seconds to resolve)."""
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
        while time.monotonic() < deadline:
            try:
                r = await client.get(url)
                if r.status_code < 500:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2.0)
    log.warning("%s not reachable after %.0fs; continuing anyway", url, timeout)
    return False


# --------------------------------------------------------------------------- ncco


def build_ncco(public_url: str, call_ref: str, s: Settings) -> list[dict[str, Any]]:
    host = normalize_public_url(public_url).removeprefix("https://")
    endpoint: dict[str, Any] = {
        "type": "websocket",
        "uri": f"wss://{host}/socket",
        "content-type": f"audio/l16;rate={s.audio_rate}",
        "headers": {"call_ref": call_ref},
    }
    if s.ws_auth:
        endpoint["authorization"] = {"type": "custom", "value": f"Bearer {s.ws_auth_token}"}
    ncco: list[dict[str, Any]] = []
    if s.announce_ai and s.announce_text:
        ncco.append({"action": "talk", "text": s.announce_text, "language": "ja-JP"})
    ncco.append(
        {
            "action": "connect",
            "eventType": "synchronous",
            "eventUrl": [f"https://{host}/webhooks/event"],
            "limit": s.call_max_seconds,
            "endpoint": [endpoint],
        }
    )
    ncco.append({"action": "talk", "text": GOODBYE_TEXT, "language": "ja-JP"})
    return ncco


def talk_ncco(text: str) -> list[dict[str, Any]]:
    return [{"action": "talk", "text": text, "language": "ja-JP"}]


# --------------------------------------------------------------------------- app


@asynccontextmanager
async def lifespan(app: FastAPI):
    state: AppState = app.state.agent
    state.loop = asyncio.get_running_loop()
    write_state_file(state)
    log.info("vonage-agent %s listening on %s:%s", VERSION, settings.host, settings.port)

    if state.public_url:
        state.spawn(state.handle_new_public_url(state.public_url, "env"))

    if state.tunnel_mode == "quick":
        loop = state.loop

        def on_url(url: str) -> None:
            loop.call_soon_threadsafe(lambda: state.spawn(state.handle_new_public_url(url, "tunnel")))

        def on_down(code: int | None) -> None:
            def _apply() -> None:
                state.public_url_source = "tunnel(down)"
                state.webhook_sync.update(status="stale", error=f"cloudflared exited (code={code}); waiting for a new URL")

            loop.call_soon_threadsafe(_apply)

        state.tunnel = QuickTunnel(
            local_url=f"http://127.0.0.1:{settings.port}",
            cloudflared_bin=settings.cloudflared_bin,
            extra_args=settings.cloudflared_extra_args,
            on_url=on_url,
            on_down=on_down,
            log_path=RUNTIME_DIR / "cloudflared.log",
        )
        state.tunnel.start()
    elif state.tunnel_mode == "none" and not state.public_url:
        log.warning(
            "No PUBLIC_URL and no tunnel: outbound calls are disabled until a public URL is known. "
            "Install cloudflared (TUNNEL=quick) or set PUBLIC_URL."
        )
    if not settings.gemini_api_key:
        log.warning("GEMINI_API_KEY is not set; calls will connect but the AI cannot talk.")

    try:
        yield
    finally:
        if state.tunnel:
            state.tunnel.stop()
        try:
            STATE_FILE.unlink(missing_ok=True)
        except OSError:
            pass


# The tunnel exposes this app publicly: keep the auto-generated docs off.
app = FastAPI(title="vonage-agent", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.state.agent = AppState(settings)


def get_state() -> AppState:
    return app.state.agent


def require_admin(request: Request) -> None:
    auth = request.headers.get("authorization") or ""
    expected = f"Bearer {settings.admin_token}"
    if not secrets.compare_digest(auth.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="admin token required (see .runtime/state.json)")


async def verify_vonage_webhook(request: Request) -> bytes:
    body = await request.body()
    if settings.signature_secret and not verify_signed_webhook(request.headers.get("authorization"), body, settings.signature_secret):
        raise HTTPException(status_code=401, detail="invalid Vonage webhook signature")
    return body


def public_base_from_request(state: AppState, request: Request) -> str:
    if state.public_url:
        return state.public_url
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return normalize_public_url(host)


# --------------------------------------------------------------------------- public routes


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse({"service": "vonage-agent", "version": VERSION, "health": "/health"})


@app.get("/health")
async def health(state: AppState = Depends(get_state)) -> JSONResponse:
    return JSONResponse(state.snapshot(public=True))


@app.api_route("/webhooks/answer", methods=["GET", "POST"])
async def answer_webhook(request: Request, state: AppState = Depends(get_state)) -> JSONResponse:
    body = await verify_vonage_webhook(request)
    params: dict[str, Any] = dict(request.query_params)
    if body:
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                params.update(parsed)
        except json.JSONDecodeError:
            pass

    base_url = public_base_from_request(state, request)
    if not base_url:
        return JSONResponse(talk_ncco("現在準備中です。しばらくしてからおかけ直しください。"))
    if not settings.gemini_api_key:
        return JSONResponse(talk_ncco("現在AIアシスタントを利用できません。失礼いたします。"))

    caller = str(params.get("from") or "")
    record = state.registry.create(
        direction="inbound",
        to_number=str(params.get("to") or settings.from_number),
        from_number=caller,
        objective=settings.inbound_objective,
    )
    record.uuid = params.get("uuid") or None
    record.conversation_uuid = params.get("conversation_uuid") or None
    record.status = "answered"
    record.answered_at = time.time()
    log.info("inbound call from=%s call_ref=%s uuid=%s", mask_number(caller), record.call_ref, record.uuid)
    return JSONResponse(build_ncco(base_url, record.call_ref, settings))


@app.post("/webhooks/event")
async def event_webhook(request: Request, state: AppState = Depends(get_state)) -> Response:
    body = await verify_vonage_webhook(request)
    payload: dict[str, Any] = {}
    if body:
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            payload = {}
    if not payload:
        payload = dict(request.query_params)

    status = payload.get("status") or payload.get("detail") or "-"
    log.info(
        "event status=%s direction=%s uuid=%s from=%s to=%s",
        status,
        payload.get("direction") or "-",
        payload.get("uuid") or payload.get("conversation_uuid") or "-",
        mask_number(str(payload.get("from") or "")),
        mask_number(str(payload.get("to") or "")),
    )
    record = state.registry.by_uuid(payload.get("uuid")) or state.registry.by_uuid(payload.get("conversation_uuid"))
    if record is not None:
        record.note_event(payload)
    # An empty 200 tells Vonage to carry on with the current NCCO.
    return Response(status_code=200)


@app.api_route("/webhooks/fallback", methods=["GET", "POST"])
async def fallback_webhook(request: Request) -> JSONResponse:
    await verify_vonage_webhook(request)
    log.warning("fallback webhook hit reason=%s", request.query_params.get("reason"))
    return JSONResponse(talk_ncco("申し訳ありません。現在接続できません。失礼いたします。"))


# --------------------------------------------------------------------------- websocket


@app.websocket("/socket")
async def media_socket(websocket: WebSocket) -> None:
    state = get_state()
    if settings.ws_auth:
        auth = websocket.headers.get("authorization") or ""
        if not secrets.compare_digest(auth.encode(), f"Bearer {settings.ws_auth_token}".encode()):
            log.warning("websocket rejected: bad authorization header")
            await websocket.close(code=1008)
            return

    await websocket.accept()

    # Vonage's first frame is JSON metadata carrying the NCCO headers (call_ref).
    try:
        first = await asyncio.wait_for(websocket.receive(), timeout=10)
    except asyncio.TimeoutError:
        log.warning("websocket: no metadata frame within 10s")
        await websocket.close(code=1008)
        return
    if first.get("type") == "websocket.disconnect":
        return

    meta = parse_ws_metadata(first.get("text"))
    call_ref = extract_call_ref(meta)
    record = state.registry.by_ref(call_ref)
    if record is None or record.ended_at is not None or record.ws_connected:
        log.warning(
            "websocket rejected: %s call_ref=%s",
            "unknown" if record is None else ("call already ended" if record.ended_at else "duplicate connection"),
            call_ref,
        )
        await websocket.close(code=1008)
        return

    record.ws_connected = True
    log.info("websocket connected call_ref=%s content-type=%s", record.call_ref, meta.get("content-type") or "?")
    bridge = GeminiVoiceBridge(websocket, record, settings, on_hangup=state.hangup, on_dtmf=state.send_dtmf)
    reason = "error"
    try:
        reason = await bridge.run()
    finally:
        record.ws_connected = False
        log.info(
            "websocket closed call_ref=%s reason=%s audio_in=%s audio_out=%s",
            record.call_ref,
            reason,
            bridge.bytes_in,
            bridge.bytes_out,
        )
    if reason not in {"agent_end_call", "vonage_disconnected"} and record.active and record.direction == "outbound":
        # The AI side ended (error/inactivity): make sure the PSTN leg is released too.
        await state.hangup(record, reason)


def parse_ws_metadata(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def extract_call_ref(meta: dict[str, Any]) -> str | None:
    """Vonage merges NCCO ``headers`` into the metadata frame; tolerate a nested form too."""
    value = meta.get("call_ref")
    if not value and isinstance(meta.get("headers"), dict):
        value = meta["headers"].get("call_ref")
    return str(value) if value else None


# --------------------------------------------------------------------------- admin API


class CallRequest(BaseModel):
    to: str = Field(..., description="Destination number, E.164 preferred (+81...)")
    objective: str = Field(..., min_length=1, max_length=1000, description="What the AI should accomplish")
    instructions: str | None = Field(None, max_length=2000, description="Extra facts/constraints for the AI")
    dry_run: bool = False


class SyncRequest(BaseModel):
    url: str | None = None
    force: bool = False


@app.get("/admin/status", dependencies=[Depends(require_admin)])
async def admin_status(state: AppState = Depends(get_state)) -> JSONResponse:
    data = state.snapshot()
    data["calls"] = [r.to_dict(include_transcript=False) for r in state.registry.all(limit=10)]
    return JSONResponse(data)


@app.post("/admin/sync-webhooks", dependencies=[Depends(require_admin)])
async def admin_sync_webhooks(req: SyncRequest, state: AppState = Depends(get_state)) -> JSONResponse:
    base_url = normalize_public_url(req.url) if req.url else state.public_url
    if not base_url:
        raise HTTPException(status_code=409, detail="public URL unknown: pass {\"url\": ...} or wait for the tunnel")
    if req.url:
        state.set_public_url(base_url, "manual")
    result = await state.sync_webhooks(base_url, force=req.force)
    status_code = 200 if result.get("status") == "ok" else 502
    return JSONResponse(result, status_code=status_code)


@app.post("/calls", dependencies=[Depends(require_admin)])
async def create_call(req: CallRequest, state: AppState = Depends(get_state)) -> JSONResponse:
    try:
        to_number = normalize_to_e164(req.to)
        check_destination_allowed(to_number, settings.allowed_to_numbers)
    except NumberError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    problems: list[str] = []
    if not settings.from_number:
        problems.append("VONAGE_FROM_NUMBER is not set (trial accounts: 123456789)")
    if not settings.application_id:
        problems.append("VONAGE_APPLICATION_ID is not set")
    if not (settings.private_key_inline or private_key_file_ok(settings.private_key_path)):
        problems.append(f"Vonage private key not found at {settings.private_key_path}")
    if not settings.gemini_api_key:
        problems.append("GEMINI_API_KEY is not set")
    if not state.public_url:
        problems.append("public URL unknown yet (tunnel still starting, or set PUBLIC_URL)")
    if problems and not req.dry_run:
        raise HTTPException(status_code=409, detail="; ".join(problems))

    active = state.registry.active(direction="outbound")
    if len(active) >= settings.max_concurrent_calls and not req.dry_run:
        raise HTTPException(
            status_code=429,
            detail=(
                f"{len(active)} outbound call(s) already active (MAX_CONCURRENT_CALLS={settings.max_concurrent_calls}); "
                f"hang up first: {', '.join(r.uuid or r.call_ref for r in active)}"
            ),
        )

    record = state.registry.create(
        direction="outbound",
        to_number=to_number,
        from_number=settings.from_number,
        objective=req.objective.strip(),
        instructions=(req.instructions or "").strip(),
    )
    base_url = state.public_url or "https://PUBLIC-URL-PENDING"
    ncco = build_ncco(base_url, record.call_ref, settings)

    if req.dry_run:
        record.status = "dry_run"
        record.mark_ended("dry_run")
        return JSONResponse(
            {
                "dry_run": True,
                "ok": not problems,
                "problems": problems,
                "to": to_number,
                "from": settings.from_number or None,
                "public_url": state.public_url or None,
                "webhooks": state.webhook_sync,
                "ncco": ncco,
                "call_ref": record.call_ref,
                "gemini_model": settings.gemini_model,
            }
        )

    try:
        result = await asyncio.to_thread(
            state.vonage.create_call,
            to_number=to_number,
            from_number=settings.from_number,
            ncco=ncco,
            event_url=f"{state.public_url}/webhooks/event",
            length_timer=settings.call_max_seconds,
            ringing_timer=settings.ringing_timeout,
        )
    except (VonageError, httpx.HTTPError, SystemExit) as exc:
        record.status = "failed"
        record.error = str(exc)
        record.mark_ended("create_failed")
        status = getattr(exc, "status", None) or 502
        raise HTTPException(status_code=502, detail=f"{exc} (vonage http {status})") from exc

    record.uuid = result.get("uuid")
    record.conversation_uuid = result.get("conversation_uuid")
    record.status = result.get("status") or "started"
    log.info("call placed uuid=%s to=%s call_ref=%s", record.uuid, mask_number(to_number), record.call_ref)
    return JSONResponse(record.to_dict(include_transcript=False), status_code=201)


@app.get("/calls", dependencies=[Depends(require_admin)])
async def list_calls(limit: int = 20, state: AppState = Depends(get_state)) -> JSONResponse:
    return JSONResponse({"calls": [r.to_dict(include_transcript=False) for r in state.registry.all(limit=limit)]})


@app.get("/calls/{key}", dependencies=[Depends(require_admin)])
async def get_call(key: str, refresh: bool = False, state: AppState = Depends(get_state)) -> JSONResponse:
    record = state.registry.lookup(key)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown call")
    data = record.to_dict()
    if refresh and record.uuid:
        try:
            data["vonage"] = await asyncio.to_thread(state.vonage.get_call, record.uuid)
        except (VonageError, httpx.HTTPError, SystemExit) as exc:
            data["vonage_error"] = str(exc)
    return JSONResponse(data)


@app.post("/calls/{key}/hangup", dependencies=[Depends(require_admin)])
async def hangup_call(key: str, state: AppState = Depends(get_state)) -> JSONResponse:
    record = state.registry.lookup(key)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown call")
    await state.hangup(record, "operator_hangup")
    return JSONResponse(record.to_dict(include_transcript=False))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False)
