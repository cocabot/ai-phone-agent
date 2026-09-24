import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import main


@pytest.fixture()
def client():
    main.agent.store._calls.clear()
    with TestClient(main.app) as c:
        yield c


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_webhooks_require_token(client):
    assert client.get("/webhooks/wrong/answer").status_code == 404
    assert client.post("/webhooks/wrong/event", json={}).status_code == 404


def test_answer_returns_websocket_ncco(client):
    r = client.get(
        "/webhooks/hooktoken/answer", params={"uuid": "u-in-1", "from": "819011112222", "to": "12015550123"}
    )
    ncco = r.json()
    connect = ncco[0]
    assert connect["action"] == "connect"
    ep = connect["endpoint"][0]
    assert ep["type"] == "websocket"
    assert ep["content-type"] == "audio/l16;rate=16000"
    assert ep["uri"].startswith("wss://unit-test.trycloudflare.com/socket/")
    call_id = ep["headers"]["call_id"]
    call = main.agent.store.get(call_id)
    assert call.direction == "inbound" and call.call_uuid == "u-in-1"
    assert ncco[-1]["action"] == "talk"


def test_event_updates_call(client):
    ncco = client.get("/webhooks/hooktoken/answer", params={"uuid": "u-ev-1"}).json()
    call_id = ncco[0]["endpoint"][0]["headers"]["call_id"]
    client.post("/webhooks/hooktoken/event", json={"uuid": "u-ev-1", "status": "completed"})
    data = client.get(f"/api/calls/{call_id}").json()
    assert data["status"] == "completed" and data["ended_at"]


def test_early_events_are_applied_later(client):
    client.post("/webhooks/hooktoken/event", json={"uuid": "u-early", "status": "ringing"})
    call = main.agent.store.new(direction="outbound", to="+819012345678", from_="1", goal="g")
    main.agent.attach_uuid(call, "u-early")
    assert call.status == "ringing"


def test_local_api_rejects_tunnel_traffic(client):
    assert client.get("/api/status").status_code == 200
    r = client.get("/api/status", headers={"cf-connecting-ip": "1.2.3.4"})
    assert r.status_code == 403


def test_status_hides_webhook_token(client):
    data = client.get("/api/status").json()
    assert "hooktoken" not in str(data)
    assert data["public_url"] == "https://unit-test.trycloudflare.com"


def test_dry_run_call(client):
    r = client.post("/api/calls", json={"to": "090-1234-5678", "goal": "予約確認", "dry_run": True})
    data = r.json()
    assert data["dry_run"] is True and data["to"] == "…5678"
    assert data["ready"] is False  # no private key / Gemini key in tests


def test_call_validation(client):
    assert client.post("/api/calls", json={"to": "110", "goal": "x"}).status_code == 400
    assert client.post("/api/calls", json={"to": "09012345678", "goal": " "}).status_code == 400


def test_place_call_uses_inline_ncco(client, monkeypatch):
    captured = {}

    def fake_create(to, ncco, event_url, length_timer, ringing_timer):
        captured.update(to=to, ncco=ncco, event_url=event_url)
        return {"uuid": "u-out-1", "status": "started"}

    monkeypatch.setattr(main.agent.vonage, "create_call", fake_create)
    r = client.post("/api/calls", json={"to": "09012345678", "goal": "テスト"})
    assert r.status_code == 200, r.text
    assert captured["to"] == "+819012345678"
    assert captured["event_url"] == "https://unit-test.trycloudflare.com/webhooks/hooktoken/event"
    assert captured["ncco"][0]["endpoint"][0]["uri"].startswith("wss://unit-test.trycloudflare.com/socket/")
    # concurrent call limit (default 1)
    assert client.post("/api/calls", json={"to": "09012345678", "goal": "2本目"}).status_code == 409
    client.post("/webhooks/hooktoken/event", json={"uuid": "u-out-1", "status": "completed"})


def test_websocket_rejects_bad_token(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/socket/nope/bad") as ws:
            ws.receive_text()


def test_websocket_echo_mode(client, monkeypatch):
    monkeypatch.setenv("BRIDGE", "echo")
    ncco = client.get("/webhooks/hooktoken/answer", params={"uuid": "u-echo"}).json()
    path = "/" + ncco[0]["endpoint"][0]["uri"].split("/", 3)[3]
    with client.websocket_connect(path) as ws:
        ws.send_text('{"event":"websocket:connected"}')
        ws.send_bytes(b"\x01\x02" * 320)
        assert ws.receive_bytes() == b"\x01\x02" * 320
