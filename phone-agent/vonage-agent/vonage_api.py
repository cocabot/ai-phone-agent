"""Thin Vonage REST client: Application webhooks, Numbers linking, Voice calls.

Auth:
  - Application / Numbers API: API key + secret (Basic auth / params)
  - Voice API (create call, hangup): JWT signed with the Application private key
"""

from __future__ import annotations

import copy
import time
import uuid
from typing import Any

import httpx
import jwt

from config import Settings

API_BASE = "https://api.nexmo.com"
REST_BASE = "https://rest.nexmo.com"
TIMEOUT = httpx.Timeout(15.0)


class VonageError(RuntimeError):
    pass


def _raise_for(resp: httpx.Response, what: str) -> None:
    if resp.is_success:
        return
    detail = resp.text[:300].replace("\n", " ")
    hint = ""
    if resp.status_code == 401:
        hint = " (認証失敗: VONAGE_API_KEY / VONAGE_API_SECRET / 秘密鍵とApplication IDの組み合わせを確認)"
    raise VonageError(f"{what} failed: HTTP {resp.status_code}{hint}: {detail}")


def _req(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """httpx request that turns network errors into VonageError."""
    try:
        return httpx.request(method, url, timeout=TIMEOUT, **kwargs)
    except httpx.HTTPError as exc:
        raise VonageError(f"{method} {url.split('?')[0]} network error: {type(exc).__name__}: {exc}") from exc


def make_jwt(application_id: str, private_key_pem: str, ttl: int = 300) -> str:
    now = int(time.time())
    claims = {
        "application_id": application_id,
        "iat": now,
        "exp": now + ttl,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, private_key_pem, algorithm="RS256")


def merge_voice_webhooks(app: dict[str, Any], answer_url: str, event_url: str) -> dict[str, Any]:
    """Build a PUT body that keeps everything from GET and only swaps the voice URLs.

    PUT replaces the whole application, so name / keys / other capabilities
    must be sent back unchanged.
    """
    capabilities = copy.deepcopy(app.get("capabilities") or {})
    voice = capabilities.setdefault("voice", {})
    webhooks = voice.setdefault("webhooks", {})

    answer = dict(webhooks.get("answer_url") or {})
    answer.update(address=answer_url, http_method="GET")
    event = dict(webhooks.get("event_url") or {})
    event.update(address=event_url, http_method="POST")
    webhooks["answer_url"] = answer
    webhooks["event_url"] = event

    body: dict[str, Any] = {"name": app.get("name") or "ai-phone-agent", "capabilities": capabilities}
    public_key = (app.get("keys") or {}).get("public_key")
    if public_key:
        body["keys"] = {"public_key": public_key}
    if "privacy" in app:
        body["privacy"] = app["privacy"]
    return body


def voice_webhooks_of(app: dict[str, Any]) -> tuple[str, str]:
    hooks = ((app.get("capabilities") or {}).get("voice") or {}).get("webhooks") or {}
    return (
        (hooks.get("answer_url") or {}).get("address", ""),
        (hooks.get("event_url") or {}).get("address", ""),
    )


class VonageClient:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    # ---------- credentials ----------
    def _basic(self) -> tuple[str, str]:
        if not (self.s.api_key and self.s.api_secret):
            raise VonageError("VONAGE_API_KEY / VONAGE_API_SECRET が未設定です（Webhook自動設定に必要）")
        return (self.s.api_key, self.s.api_secret)

    def _bearer(self) -> dict[str, str]:
        if not self.s.application_id:
            raise VonageError("VONAGE_APPLICATION_ID が未設定です")
        if not self.s.private_key_present:
            raise VonageError(f"秘密鍵ファイルが見つかりません: {self.s.private_key_path}")
        pem = self.s.private_key_path.read_text("utf-8")
        return {"Authorization": f"Bearer {make_jwt(self.s.application_id, pem)}"}

    # ---------- Application API ----------
    def get_application(self) -> dict[str, Any]:
        if not self.s.application_id:
            raise VonageError("VONAGE_APPLICATION_ID が未設定です")
        resp = _req("GET", f"{API_BASE}/v2/applications/{self.s.application_id}", auth=self._basic())
        _raise_for(resp, "GET application")
        return resp.json()

    def update_voice_webhooks(self, answer_url: str, event_url: str) -> dict[str, Any]:
        app = self.get_application()
        if voice_webhooks_of(app) == (answer_url, event_url):
            return {"changed": False, "answer_url": answer_url, "event_url": event_url}
        body = merge_voice_webhooks(app, answer_url, event_url)
        resp = _req(
            "PUT", f"{API_BASE}/v2/applications/{self.s.application_id}", auth=self._basic(), json=body
        )
        _raise_for(resp, "PUT application")
        now_answer, now_event = voice_webhooks_of(resp.json())
        return {"changed": True, "answer_url": now_answer, "event_url": now_event}

    # ---------- Numbers API ----------
    def list_numbers(self) -> list[dict[str, Any]]:
        key, secret = self._basic()
        resp = _req(
            "GET", f"{REST_BASE}/account/numbers", params={"api_key": key, "api_secret": secret, "size": 100}
        )
        _raise_for(resp, "list numbers")
        return resp.json().get("numbers") or []

    def link_number(self, msisdn: str) -> dict[str, Any]:
        """Point an owned Vonage number's voice to this Application (for inbound calls)."""
        msisdn = msisdn.lstrip("+")
        numbers = self.list_numbers()
        match = next((n for n in numbers if n.get("msisdn") == msisdn), None)
        if not match:
            raise VonageError(f"番号 {msisdn} はこのアカウントの所有番号ではありません")
        if match.get("app_id") == self.s.application_id:
            return {"changed": False, "msisdn": msisdn}
        key, secret = self._basic()
        resp = _req(
            "POST",
            f"{REST_BASE}/number/update",
            params={"api_key": key, "api_secret": secret},
            data={"country": match.get("country", ""), "msisdn": msisdn, "app_id": self.s.application_id},
        )
        _raise_for(resp, "number update")
        return {"changed": True, "msisdn": msisdn}

    # ---------- Voice API ----------
    def create_call(
        self,
        to_number: str,
        ncco: list[dict[str, Any]],
        event_url: str,
        length_timer: int,
        ringing_timer: int,
    ) -> dict[str, Any]:
        if not self.s.from_number:
            raise VonageError("VONAGE_FROM_NUMBER が未設定です（Vonageで取得した番号）")
        body = {
            "to": [{"type": "phone", "number": to_number.lstrip("+")}],
            "from": {"type": "phone", "number": self.s.from_number},
            "ncco": ncco,
            "event_url": [event_url],
            "event_method": "POST",
            "length_timer": length_timer,
            "ringing_timer": ringing_timer,
        }
        resp = _req("POST", f"{API_BASE}/v1/calls", headers=self._bearer(), json=body)
        _raise_for(resp, "create call")
        return resp.json()

    def hangup(self, call_uuid: str) -> None:
        resp = _req(
            "PUT", f"{API_BASE}/v1/calls/{call_uuid}", headers=self._bearer(), json={"action": "hangup"}
        )
        if resp.status_code in (400, 404):  # already finished
            return
        _raise_for(resp, "hangup")

    def send_dtmf(self, call_uuid: str, digits: str) -> None:
        resp = _req(
            "PUT", f"{API_BASE}/v1/calls/{call_uuid}/dtmf", headers=self._bearer(), json={"digits": digits}
        )
        _raise_for(resp, "send dtmf")
