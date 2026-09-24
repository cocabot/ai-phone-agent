"""
Vonage Voice x Gemini Live phone agent.

Public (via tunnel):
  GET  /health
  GET  /webhooks/{token}/answer     inbound call -> NCCO (websocket connect)
  POST /webhooks/{token}/event      call status events
  WS   /socket/{call_id}/{token}    media stream <-> Gemini Live

Local only (127.0.0.1 or AGENT_API_TOKEN):
  GET  /api/status                  public URL / sync state / config check
  POST /api/sync                    re-apply webhook URLs to the Vonage Application
  POST /api/link-number             link VONAGE_FROM_NUMBER to the Application (inbound)
  POST /api/calls                   place an outbound call {to, goal, notes?, dry_run?}
  GET  /api/calls                   recent calls
  GET  /api/calls/{id}?wait=SEC     call detail + transcript (optionally wait for the end)
  POST /api/calls/{id}/hangup
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from calls import FINAL_STATUSES, CallSession, CallStore
from config import Settings, load_settings, normalize_public_url
from gemini_bridge import Bridge, run_echo
from phone_numbers import NumberError, check_destination, mask
from state import State
from tunnel import TunnelManager
from vonage_api import VonageClient, VonageError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vonage-agent")

RESYNC_INTERVAL_SECONDS = 300
RESYNC_RETRY_SECONDS = 30
FALLBACK_TALK = "申し訳ありません。ただいま応答できません。時間をおいておかけ直しください。"


class _RedactAccessLog(logging.Filter):
    """Keep webhook/socket tokens and phone numbers (query strings) out of access logs."""

    _patterns = [
        (re.compile(r"/webhooks/[^/?\s]+/"), "/webhooks/***/"),
        (re.compile(r"(/socket/[^/?\s]+)/[^/?\s]+"), r"\1/***"),
        (re.compile(r"\?.*$"), "?…"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            path = str(record.args[2])
            for pat, repl in self._patterns:
                path = pat.sub(repl, path)
            record.args = (*record.args[:2], path, *record.args[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactAccessLog())


class Agent:
    """Process-wide state shared by the routes."""

    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.state = State(settings.data_dir)
        self.webhook_token = self.state.webhook_token(settings.webhook_token)
        self.store = CallStore(settings.data_dir)
        self.vonage = VonageClient(settings)
        self.tunnel = TunnelManager(settings, self.on_public_url)
        self.last_sync: dict[str, Any] = self.state.get("last_sync") or {}
        self.pending_events: dict[str, list[dict[str, Any]]] = {}

    @property
    def public_url(self) -> str:
        return self.tunnel.public_url

    def urls(self, base: str | None = None) -> dict[str, str]:
        base = base or self.public_url
        if not base:
            return {}
        return {
            "answer_url": f"{base}/webhooks/{self.webhook_token}/answer",
            "event_url": f"{base}/webhooks/{self.webhook_token}/event",
        }

    async def on_public_url(self, url: str) -> None:
        self.state.update(last_public_url=url)
        if self.s.auto_sync:
            await self.sync()

    async def sync(self) -> dict[str, Any]:
        urls = self.urls()
        if not urls:
            result = {"ok": False, "error": "public URL is not known yet"}
        elif not (self.s.api_key and self.s.api_secret and self.s.application_id):
            result = {
                "ok": False,
                "error": "VONAGE_API_KEY / VONAGE_API_SECRET / VONAGE_APPLICATION_ID が必要です",
                **urls,
            }
        else:
            try:
                res = await asyncio.to_thread(
                    self.vonage.update_voice_webhooks, urls["answer_url"], urls["event_url"]
                )
                result = {"ok": True, **res}
                log.info("vonage webhooks synced changed=%s -> %s", res["changed"], self.public_url)
            except VonageError as exc:
                result = {"ok": False, "error": str(exc), **urls}
                log.error("vonage webhook sync failed: %s", exc)
        result["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        # Never persist the secret path token in the status summary.
        self.last_sync = {k: _redact(v) if k.endswith("_url") else v for k, v in result.items()}
        self.state.update(last_sync=self.last_sync)
        return self.last_sync

    async def resync_loop(self) -> None:
        """Re-check periodically: retries failed syncs and undoes manual edits in the dashboard."""
        if not self.s.auto_sync:
            return
        while True:
            ok = self.last_sync.get("ok") if self.public_url else True
            await asyncio.sleep(RESYNC_RETRY_SECONDS if not ok else RESYNC_INTERVAL_SECONDS)
            if self.public_url:
                await self.sync()

    # -------------------------------------------------------------- calls
    def ws_uri(self, call: CallSession, base: str) -> str:
        host = base.removeprefix("https://")
        return f"wss://{host}/socket/{call.id}/{call.token}"

    def connect_ncco(self, call: CallSession, base: str) -> list[dict[str, Any]]:
        return [
            {
                "action": "connect",
                "from": self.s.from_number or "ai-phone-agent",
                "endpoint": [
                    {
                        "type": "websocket",
                        "uri": self.ws_uri(call, base),
                        "content-type": f"audio/l16;rate={self.s.audio_rate}",
                        "headers": {"call_id": call.id},
                    }
                ],
            },
            # Only reached if the websocket closes before we hang up (e.g. Gemini error).
            {"action": "talk", "text": FALLBACK_TALK, "language": "ja-JP"},
        ]

    def attach_uuid(self, call: CallSession, call_uuid: str) -> None:
        call.call_uuid = call_uuid
        for event in self.pending_events.pop(call_uuid, []):
            self.apply_event(call, event)
        self.store.save(call)

    def apply_event(self, call: CallSession, body: dict[str, Any]) -> None:
        status = str(body.get("status") or "")
        if not status:
            return
        call.events.append({"status": status, "t": round(time.time() - call.created_at, 1)})
        if call.status not in FINAL_STATUSES:
            call.status = status
        if status == "answered" and not call.answered_at:
            call.answered_at = time.time()
        if status in FINAL_STATUSES:
            call.ended_at = call.ended_at or time.time()
            if status != "completed" and not call.error:
                call.error = str(body.get("detail") or status)

    async def hangup(self, call: CallSession) -> bool:
        if not call.call_uuid:
            return False
        try:
            await asyncio.to_thread(self.vonage.hangup, call.call_uuid)
            return True
        except VonageError as exc:
            log.warning("call=%s hangup failed: %s", call.id, exc)
            return False

    async def send_dtmf(self, call: CallSession, digits: str) -> None:
        if not (call.call_uuid and digits):
            raise VonageError("DTMF cannot be sent (no call uuid or digits)")
        await asyncio.to_thread(self.vonage.send_dtmf, call.call_uuid, digits)

    def config_check(self) -> dict[str, Any]:
        s = self.s
        return {
            "vonage_application_id": bool(s.application_id),
            "vonage_private_key_file": s.private_key_present,
            "vonage_api_key_secret": bool(s.api_key and s.api_secret),
            "vonage_from_number": bool(s.from_number),
            "gemini_api_key": bool(s.gemini_api_key),
            "gemini_model": s.gemini_model,
            "audio_rate": s.audio_rate,
            "auto_sync": s.auto_sync,
            "allowed_country_codes": list(s.allowed_country_codes),
            "call_allowlist": [mask(n) for n in s.call_allowlist],
        }


def _redact(url: Any) -> Any:
    return re.sub(r"/webhooks/[^/]+/", "/webhooks/***/", url) if isinstance(url, str) else url


settings = load_settings()
agent = Agent(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    missing = [k for k, v in agent.config_check().items() if v is False and k != "auto_sync"]
    if missing:
        log.warning("未設定: %s  (./agent doctor で詳細確認)", ", ".join(missing))
    await agent.tunnel.start()
    resync = asyncio.create_task(agent.resync_loop())
    try:
        yield
    finally:
        resync.cancel()
        await agent.tunnel.stop()


app = FastAPI(title="vonage-agent", version="0.2.0", lifespan=lifespan)


# ------------------------------------------------------------------ helpers
def _check_webhook_token(token: str) -> None:
    if not hmac.compare_digest(token, agent.webhook_token):
        raise HTTPException(status_code=404)


def require_local(request: Request) -> None:
    """Allow the local CLI; reject anything that came through the tunnel."""
    if settings.api_token:
        auth = request.headers.get("authorization", "")
        if hmac.compare_digest(auth, f"Bearer {settings.api_token}"):
            return
    forwarded = any(h in request.headers for h in ("cf-connecting-ip", "x-forwarded-for", "cf-ray"))
    client = request.client.host if request.client else ""
    if not forwarded and client in {"127.0.0.1", "::1", "localhost", "testclient"}:
        return
    raise HTTPException(status_code=403, detail="local access only")


# ------------------------------------------------------------------ public
@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {"status": "ok", "service": "vonage-agent", "public_url_known": bool(agent.public_url)}
    )


@app.get("/webhooks/{token}/answer")
async def answer_webhook(token: str, request: Request) -> JSONResponse:
    _check_webhook_token(token)
    q = request.query_params
    base = (
        agent.public_url
        or f"https://{request.headers.get('x-forwarded-host') or request.headers.get('host')}"
    )
    call = agent.store.new(
        direction="inbound",
        to=q.get("to", ""),
        from_=q.get("from", ""),
        goal=settings.inbound_goal,
    )
    call.status, call.answered_at = "answered", time.time()
    agent.attach_uuid(call, q.get("uuid", ""))
    log.info("inbound call=%s from=%s", call.id, mask(call.from_))
    return JSONResponse(agent.connect_ncco(call, base))


@app.post("/webhooks/{token}/event")
async def event_webhook(token: str, request: Request) -> JSONResponse:
    _check_webhook_token(token)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    call_uuid = str(body.get("uuid") or "")
    status = body.get("status") or "-"
    call = agent.store.by_uuid(call_uuid) if call_uuid else None
    log.info("event status=%s call=%s", status, call.id if call else f"uuid:{call_uuid[:8]}")
    if call:
        agent.apply_event(call, body)
        agent.store.save(call)
    elif call_uuid:
        agent.pending_events.setdefault(call_uuid, []).append(body)
        if len(agent.pending_events) > 100:
            agent.pending_events.pop(next(iter(agent.pending_events)))
    return JSONResponse({"ok": True})


@app.websocket("/socket/{call_id}/{token}")
async def media_socket(websocket: WebSocket, call_id: str, token: str) -> None:
    call = agent.store.get(call_id)
    if not call or call.id != call_id or not hmac.compare_digest(token, call.token):
        await websocket.close(code=1008)
        log.warning("websocket rejected (unknown call or bad token)")
        return
    await websocket.accept()
    log.info("call=%s websocket connected", call.id)
    try:
        if os.environ.get("BRIDGE", "").lower() == "echo":
            await run_echo(websocket, call)
        elif not settings.gemini_api_key:
            call.error = "GEMINI_API_KEY is not set"
            log.error("call=%s GEMINI_API_KEY is not set", call.id)
        else:
            await Bridge(websocket, call, settings, agent.store, agent.hangup, agent.send_dtmf).run()
    except Exception as exc:
        call.error = call.error or f"{type(exc).__name__}: {exc}"[:300]
        log.exception("call=%s bridge error", call.id)
    finally:
        # The media stream is gone, so the call is over from the agent's point of view.
        if not call.finished:
            call.ended_at = time.time()
            call.status = "completed"
        agent.store.save(call)
        try:
            await websocket.close()
        except RuntimeError:
            pass


# ------------------------------------------------------------------ local API
class PlaceCall(BaseModel):
    to: str
    goal: str
    notes: str = ""
    dry_run: bool = False


class SyncRequest(BaseModel):
    url: str | None = None


class LinkNumber(BaseModel):
    number: str | None = None


@app.get("/api/status")
async def api_status(request: Request) -> JSONResponse:
    require_local(request)
    return JSONResponse(
        {
            "public_url": agent.public_url,
            "tunnel_mode": agent.tunnel.mode,
            "tunnel_connected": agent.tunnel.connected if agent.tunnel.mode == "cloudflared" else None,
            "tunnel_error": agent.tunnel.last_error or None,
            "webhooks": {k: _redact(v) for k, v in agent.urls().items()},
            "last_sync": agent.last_sync,
            "config": agent.config_check(),
            "active_calls": [c.public(full=False) for c in agent.store.active()],
        }
    )


@app.post("/api/sync")
async def api_sync(body: SyncRequest, request: Request) -> JSONResponse:
    require_local(request)
    if body.url:
        agent.tunnel.public_url = normalize_public_url(body.url)
        agent.state.update(last_public_url=agent.tunnel.public_url)
    return JSONResponse(await agent.sync())


@app.post("/api/link-number")
async def api_link_number(body: LinkNumber, request: Request) -> JSONResponse:
    require_local(request)
    number = body.number or settings.from_number
    if not number:
        raise HTTPException(400, "number is required (or set VONAGE_FROM_NUMBER)")
    try:
        return JSONResponse(await asyncio.to_thread(agent.vonage.link_number, number))
    except VonageError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/calls")
async def api_place_call(body: PlaceCall, request: Request) -> JSONResponse:
    require_local(request)
    try:
        to = check_destination(body.to, settings.allowed_country_codes, settings.call_allowlist)
    except NumberError as exc:
        raise HTTPException(400, str(exc)) from exc
    goal = body.goal.strip()
    if not goal:
        raise HTTPException(400, "goal（用件）は必須です")
    base = agent.public_url
    if body.dry_run:
        return JSONResponse(
            {
                "dry_run": True,
                "to": mask(to),
                "from": mask(settings.from_number),
                "public_url": base or None,
                "goal": goal,
                "ready": bool(
                    base
                    and settings.gemini_api_key
                    and settings.private_key_present
                    and settings.application_id
                    and settings.from_number
                ),
                "config": agent.config_check(),
            }
        )
    if len(agent.store.active(settings.max_call_seconds + settings.ringing_timeout + 120)) >= int(
        os.environ.get("MAX_CONCURRENT_CALLS", "1")
    ):
        raise HTTPException(409, "別の通話が進行中です（MAX_CONCURRENT_CALLS）")
    if not base:
        raise HTTPException(503, "公開URLが未確定です（トンネル起動待ち / PUBLIC_URL 未設定）")
    call = agent.store.new(
        direction="outbound", to=to, from_=settings.from_number, goal=goal, notes=body.notes
    )
    try:
        res = await asyncio.to_thread(
            agent.vonage.create_call,
            to,
            agent.connect_ncco(call, base),
            agent.urls(base)["event_url"],
            settings.max_call_seconds + 30,
            settings.ringing_timeout,
        )
    except VonageError as exc:
        call.status, call.error, call.ended_at = "failed", str(exc)[:300], time.time()
        agent.store.save(call)
        raise HTTPException(502, str(exc)) from exc
    call.status = res.get("status") or "started"
    agent.attach_uuid(call, res.get("uuid", ""))
    log.info("outbound call=%s to=%s placed", call.id, mask(to))
    return JSONResponse(call.public(full=False))


@app.get("/api/calls")
async def api_list_calls(request: Request, limit: int = 20) -> JSONResponse:
    require_local(request)
    return JSONResponse(agent.store.recent(limit))


@app.get("/api/calls/{call_id}")
async def api_get_call(call_id: str, request: Request, wait: float = 0) -> JSONResponse:
    require_local(request)
    deadline = time.monotonic() + min(max(wait, 0), 900)
    while True:
        call = agent.store.get(call_id)
        if call is None or call.finished or time.monotonic() >= deadline:
            break
        await asyncio.sleep(1)
    data = agent.store.load(call_id)
    if data is None:
        raise HTTPException(404, "call not found")
    return JSONResponse(data)


@app.post("/api/calls/{call_id}/hangup")
async def api_hangup(call_id: str, request: Request) -> JSONResponse:
    require_local(request)
    call = agent.store.get(call_id)
    if call is None:
        raise HTTPException(404, "call not found")
    ok = await agent.hangup(call)
    return JSONResponse({"ok": ok, "id": call.id})


def serve() -> None:
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    serve()
