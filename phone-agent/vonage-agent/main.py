"""
Vonage Voice → WebSocket connectivity test (no AI yet).

Endpoints:
  GET  /health
  GET  /webhooks/answer  → NCCO JSON (websocket connect)
  POST /webhooks/event   → minimal call event logging
  WS   /socket           → receive binary audio; count bytes only
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from config import load_settings, private_key_file_ok

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vonage-agent")

app = FastAPI(title="vonage-agent", version="0.1.0")
settings = load_settings()


def _mask_phone(value: str | None) -> str:
    if not value:
        return "-"
    digits = "".join(c for c in value if c.isdigit())
    if len(digits) < 4:
        return "…"
    return f"…{digits[-4:]}"


def build_answer_ncco(public_domain: str) -> list[dict[str, Any]]:
    host = public_domain.strip().rstrip("/")
    ws_uri = f"wss://{host}/socket"
    return [
        {
            "action": "talk",
            "text": "Connecting audio websocket.",
            "language": "en-US",
        },
        {
            "action": "connect",
            "from": "vonage-agent",
            "endpoint": [
                {
                    "type": "websocket",
                    "uri": ws_uri,
                    "content-type": "audio/l16;rate=16000",
                    "headers": {
                        "X-Agent": "vonage-agent",
                    },
                }
            ],
        },
    ]


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "vonage-agent",
            "public_domain_set": bool(settings.public_domain),
            "application_id_set": bool(settings.application_id),
            "private_key_path_set": bool(settings.private_key_path),
            "private_key_file_present": private_key_file_ok(settings.private_key_path)
            if settings.private_key_path
            else False,
        }
    )


@app.get("/webhooks/answer")
async def answer_webhook() -> JSONResponse:
    # Reload domain each request so tunnel hostname can be set after start
    current = load_settings()
    domain = current.public_domain
    if not domain:
        # Fallback for local smoke test before tunnel hostname is known
        domain = "localhost"
    ncco = build_answer_ncco(domain)
    return JSONResponse(ncco)


@app.post("/webhooks/event")
async def event_webhook(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    # Minimal log — never dump full payload with numbers/secrets
    status = body.get("status") or body.get("detail") or "-"
    direction = body.get("direction") or "-"
    uuid = body.get("uuid") or body.get("conversation_uuid") or "-"
    from_tail = _mask_phone(str(body.get("from") or ""))
    to_tail = _mask_phone(str(body.get("to") or ""))
    log.info(
        "event status=%s direction=%s uuid=%s from=%s to=%s",
        status,
        direction,
        uuid,
        from_tail,
        to_tail,
    )
    return JSONResponse({"ok": True})


@app.websocket("/socket")
async def media_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    log.info("websocket connected")
    total = 0
    text_msgs = 0
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if "bytes" in message and message["bytes"] is not None:
                chunk = message["bytes"]
                total += len(chunk)
                if total == len(chunk) or total % 32000 < len(chunk):
                    log.info("audio bytes received total=%s last_chunk=%s", total, len(chunk))
                # Optional echo (off by default for safety; set VONAGE_ECHO=1 to enable)
                if os.environ.get("VONAGE_ECHO", "").strip() == "1":
                    await websocket.send_bytes(chunk)
            elif "text" in message and message["text"] is not None:
                text_msgs += 1
                # Do not log raw text (may contain metadata with numbers)
                log.info("text frame received count=%s len=%s", text_msgs, len(message["text"]))
    except WebSocketDisconnect:
        log.info("websocket disconnected total_audio_bytes=%s text_frames=%s", total, text_msgs)
    except Exception as exc:
        log.info("websocket error type=%s total_audio_bytes=%s", type(exc).__name__, total)
    finally:
        log.info("websocket closed total_audio_bytes=%s", total)


if __name__ == "__main__":
    import uvicorn

    s = load_settings()
    uvicorn.run("main:app", host="0.0.0.0", port=s.port, reload=False)
