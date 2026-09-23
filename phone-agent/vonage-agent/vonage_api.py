"""Thin Vonage REST client: Application API (webhook sync) and Voice API (calls).

Only the endpoints this agent needs are implemented, over plain HTTPS with
``httpx`` so every request is easy to inspect and reproduce with curl.

- Application API v2 (``/v2/applications``): basic auth with API key/secret.
- Voice API v1 (``/v1/calls``): JWT (RS256) signed with the application key.
"""

from __future__ import annotations

import copy
import hashlib
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from config import Settings, load_private_key_pem

WEBHOOK_PATHS = {
    "answer_url": ("/webhooks/answer", "GET"),
    "event_url": ("/webhooks/event", "POST"),
    "fallback_answer_url": ("/webhooks/fallback", "GET"),
}


class VonageError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def make_application_jwt(application_id: str, private_key_pem: str, *, ttl_seconds: int = 300) -> str:
    now = int(time.time())
    claims = {
        "application_id": application_id,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, private_key_pem, algorithm="RS256")


def verify_signed_webhook(authorization_header: str | None, body: bytes, signature_secret: str) -> bool:
    """Validate a Vonage signed webhook (HS256 JWT in the Authorization header)."""
    if not authorization_header or not authorization_header.lower().startswith("bearer "):
        return False
    token = authorization_header.split(" ", 1)[1].strip()
    try:
        claims = jwt.decode(token, signature_secret, algorithms=["HS256"], leeway=60)
    except jwt.PyJWTError:
        return False
    payload_hash = claims.get("payload_hash")
    if payload_hash and body:
        return hashlib.sha256(body).hexdigest() == payload_hash
    return True


def desired_voice_webhooks(base_url: str) -> dict[str, dict[str, str]]:
    base = base_url.rstrip("/")
    return {
        name: {"address": f"{base}{path}", "http_method": method}
        for name, (path, method) in WEBHOOK_PATHS.items()
    }


def build_application_update(application: dict[str, Any], base_url: str) -> tuple[dict[str, Any], bool]:
    """Return ``(request_body, changed)`` for ``PUT /v2/applications/{id}``.

    The Application API replaces the whole application on PUT, so every
    existing capability is round-tripped and only the voice webhooks are
    rewritten. Existing per-webhook timeouts are preserved.
    """
    caps = copy.deepcopy(application.get("capabilities") or {})
    voice = caps.setdefault("voice", {})
    if not isinstance(voice, dict):
        voice = {}
        caps["voice"] = voice
    webhooks = voice.setdefault("webhooks", {})
    if not isinstance(webhooks, dict):
        webhooks = {}
        voice["webhooks"] = webhooks

    changed = False
    for name, desired in desired_voice_webhooks(base_url).items():
        current = webhooks.get(name) if isinstance(webhooks.get(name), dict) else {}
        if (
            current.get("address") != desired["address"]
            or str(current.get("http_method") or "").upper() != desired["http_method"]
        ):
            changed = True
        webhooks[name] = {**current, **desired}

    body: dict[str, Any] = {"name": application.get("name") or "vonage-agent", "capabilities": caps}
    public_key = (application.get("keys") or {}).get("public_key")
    if public_key:
        # Round-trip the existing key so the PUT never rotates it.
        body["keys"] = {"public_key": public_key}
    if application.get("privacy") is not None:
        body["privacy"] = application["privacy"]
    return body, changed


def current_voice_webhooks(application: dict[str, Any]) -> dict[str, str]:
    webhooks = ((application.get("capabilities") or {}).get("voice") or {}).get("webhooks") or {}
    out: dict[str, str] = {}
    for name in WEBHOOK_PATHS:
        entry = webhooks.get(name)
        if isinstance(entry, dict) and entry.get("address"):
            out[name] = str(entry["address"])
    return out


@dataclass
class SyncResult:
    base_url: str
    changed: bool
    webhooks: dict[str, str]
    previous: dict[str, str]


class VonageClient:
    def __init__(self, settings: Settings, *, timeout: float = 15.0):
        self.s = settings
        self.timeout = timeout
        self._pem: str | None = None

    # --- auth helpers -----------------------------------------------------

    def _require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self.s, n)]
        if missing:
            env_names = {
                "application_id": "VONAGE_APPLICATION_ID",
                "api_key": "VONAGE_API_KEY",
                "api_secret": "VONAGE_API_SECRET",
                "from_number": "VONAGE_FROM_NUMBER",
            }
            raise VonageError("Missing env vars: " + ", ".join(env_names.get(m, m) for m in missing))

    def jwt(self) -> str:
        self._require("application_id")
        if self._pem is None:
            self._pem = load_private_key_pem(self.s)
        return make_application_jwt(self.s.application_id, self._pem)

    def _basic_auth(self) -> tuple[str, str]:
        self._require("api_key", "api_secret")
        return (self.s.api_key, self.s.api_secret)

    def _url(self, path: str) -> str:
        return f"{self.s.api_base}{path}"

    @staticmethod
    def _raise_for_status(response: httpx.Response, what: str) -> None:
        if response.is_success:
            return
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text
        detail = body if isinstance(body, str) else body.get("detail") or body.get("title") or body
        raise VonageError(f"Vonage {what} failed: HTTP {response.status_code}: {detail}", status=response.status_code, body=body)

    # --- Application API --------------------------------------------------

    def get_application(self) -> dict[str, Any]:
        self._require("application_id")
        with httpx.Client(timeout=self.timeout) as client:
            r = client.get(self._url(f"/v2/applications/{self.s.application_id}"), auth=self._basic_auth())
        self._raise_for_status(r, "get application")
        return r.json()

    def update_application(self, body: dict[str, Any]) -> dict[str, Any]:
        self._require("application_id")
        with httpx.Client(timeout=self.timeout) as client:
            r = client.put(
                self._url(f"/v2/applications/{self.s.application_id}"),
                auth=self._basic_auth(),
                json=body,
            )
        self._raise_for_status(r, "update application")
        return r.json()

    def sync_webhooks(self, base_url: str, *, force: bool = False) -> SyncResult:
        """Point the application's voice webhooks at ``base_url`` (idempotent)."""
        app = self.get_application()
        previous = current_voice_webhooks(app)
        body, changed = build_application_update(app, base_url)
        if changed or force:
            try:
                updated = self.update_application(body)
            except VonageError as exc:
                if exc.status not in {400, 422}:
                    raise
                # Some accounts expose read-only capability fields on GET that PUT
                # rejects; retry with a minimal body that only carries webhooks.
                minimal = {
                    "name": body["name"],
                    "capabilities": {
                        **{k: v for k, v in body["capabilities"].items() if k != "voice"},
                        "voice": {"webhooks": body["capabilities"]["voice"]["webhooks"]},
                    },
                }
                if "keys" in body:
                    minimal["keys"] = body["keys"]
                updated = self.update_application(minimal)
            webhooks = current_voice_webhooks(updated)
        else:
            webhooks = previous
        return SyncResult(base_url=base_url.rstrip("/"), changed=changed or force, webhooks=webhooks, previous=previous)

    # --- Voice API --------------------------------------------------------

    def _voice_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.jwt()}", "Content-Type": "application/json"}

    def create_call(
        self,
        *,
        to_number: str,
        from_number: str,
        ncco: list[dict[str, Any]],
        event_url: str | None = None,
        length_timer: int | None = None,
        ringing_timer: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "to": [{"type": "phone", "number": to_number.lstrip("+")}],
            "from": {"type": "phone", "number": from_number.lstrip("+")},
            "ncco": ncco,
        }
        if event_url:
            payload["event_url"] = [event_url]
            payload["event_method"] = "POST"
        if length_timer:
            payload["length_timer"] = int(length_timer)
        if ringing_timer:
            payload["ringing_timer"] = int(ringing_timer)
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(self._url("/v1/calls"), headers=self._voice_headers(), json=payload)
        self._raise_for_status(r, "create call")
        return r.json()

    def get_call(self, call_uuid: str) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            r = client.get(self._url(f"/v1/calls/{call_uuid}"), headers=self._voice_headers())
        self._raise_for_status(r, "get call")
        return r.json()

    def hangup_call(self, call_uuid: str) -> None:
        with httpx.Client(timeout=self.timeout) as client:
            r = client.put(
                self._url(f"/v1/calls/{call_uuid}"),
                headers=self._voice_headers(),
                json={"action": "hangup"},
            )
        # Vonage answers 204 on success; a 404 means the leg already ended.
        if r.status_code == 404:
            return
        self._raise_for_status(r, "hangup call")

    def send_dtmf(self, call_uuid: str, digits: str) -> None:
        with httpx.Client(timeout=self.timeout) as client:
            r = client.put(
                self._url(f"/v1/calls/{call_uuid}/dtmf"),
                headers=self._voice_headers(),
                json={"digits": digits},
            )
        self._raise_for_status(r, "send dtmf")
