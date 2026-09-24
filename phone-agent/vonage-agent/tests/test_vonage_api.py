import httpx

import vonage_api
from config import load_settings
from vonage_api import VonageClient, merge_voice_webhooks, voice_webhooks_of

APP = {
    "id": "app-123",
    "name": "my-app",
    "keys": {"public_key": "-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----"},
    "privacy": {"improve_ai": False},
    "capabilities": {
        "voice": {
            "webhooks": {
                "answer_url": {
                    "address": "https://old.trycloudflare.com/webhooks/answer",
                    "http_method": "GET",
                    "connect_timeout": 500,
                },
                "event_url": {
                    "address": "https://old.trycloudflare.com/webhooks/event",
                    "http_method": "POST",
                },
            },
            "signed_callbacks": True,
        },
        "messages": {"webhooks": {"inbound_url": {"address": "https://x/inbound"}}},
    },
    "_links": {"self": {"href": "/v2/applications/app-123"}},
}


def test_merge_keeps_everything_else():
    body = merge_voice_webhooks(APP, "https://new/a", "https://new/e")
    assert body["name"] == "my-app"
    assert body["keys"] == {"public_key": APP["keys"]["public_key"]}
    assert body["privacy"] == {"improve_ai": False}
    assert body["capabilities"]["messages"] == APP["capabilities"]["messages"]
    assert body["capabilities"]["voice"]["signed_callbacks"] is True
    hooks = body["capabilities"]["voice"]["webhooks"]
    assert hooks["answer_url"] == {"address": "https://new/a", "http_method": "GET", "connect_timeout": 500}
    assert hooks["event_url"] == {"address": "https://new/e", "http_method": "POST"}
    assert "_links" not in body
    # input untouched
    assert voice_webhooks_of(APP)[0].startswith("https://old")


def test_merge_adds_voice_capability_when_missing():
    body = merge_voice_webhooks({"name": "n", "capabilities": {}}, "https://a", "https://e")
    assert voice_webhooks_of(body) == ("https://a", "https://e")


def test_update_voice_webhooks_puts_only_when_changed(monkeypatch):
    calls = []

    def fake_request(method, url, **kw):
        if method == "GET":
            calls.append(("GET", url, kw.get("auth")))
            return httpx.Response(200, json=APP, request=httpx.Request("GET", url))
        calls.append((method, url, kw["json"]))
        return httpx.Response(200, json=kw["json"], request=httpx.Request(method, url))

    monkeypatch.setattr(vonage_api.httpx, "request", fake_request)
    client = VonageClient(load_settings())

    res = client.update_voice_webhooks("https://new/a", "https://new/e")
    assert res["changed"] is True
    assert [c[0] for c in calls] == ["GET", "PUT"]
    assert calls[0][2] == ("key", "secret")
    assert calls[1][1].endswith("/v2/applications/app-123")

    calls.clear()
    old = voice_webhooks_of(APP)
    res = client.update_voice_webhooks(*old)
    assert res["changed"] is False
    assert [c[0] for c in calls] == ["GET"]


def test_jwt_roundtrip():
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    token = vonage_api.make_jwt("app-123", pem)
    claims = jwt.decode(token, key.public_key(), algorithms=["RS256"])
    assert claims["application_id"] == "app-123"
    assert claims["exp"] > claims["iat"]


def test_network_errors_become_vonage_errors(monkeypatch):
    import pytest

    def boom(method, url, **kw):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(vonage_api.httpx, "request", boom)
    with pytest.raises(vonage_api.VonageError, match="network error"):
        VonageClient(load_settings()).get_application()
