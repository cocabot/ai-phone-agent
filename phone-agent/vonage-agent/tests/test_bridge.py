"""Drive the Bridge with a fake Vonage socket and a fake Gemini session."""

import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from google.genai import types

import gemini_bridge
from calls import CallStore
from config import load_settings


class FakeWS:
    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent_bytes: list[bytes] = []
        self.sent_text: list[dict] = []

    async def receive(self):
        return await self.inbox.get()

    async def send_bytes(self, data):
        self.sent_bytes.append(data)

    async def send_text(self, text):
        msg = json.loads(text)
        self.sent_text.append(msg)
        if msg.get("action") == "notify":  # Vonage echoes notify after playback
            await self.inbox.put(
                {
                    "type": "websocket.receive",
                    "text": json.dumps({"event": "websocket:notify", "payload": msg["payload"]}),
                }
            )


def server_msg(**kw):
    return types.LiveServerMessage(**kw)


class FakeSession:
    def __init__(self, script):
        self.script = script
        self.sent_audio = 0
        self.texts: list[str] = []
        self.tool_responses = []
        self._turns = asyncio.Queue()
        for turn in script:
            self._turns.put_nowait(turn)

    async def send_realtime_input(self, audio=None, text=None, **_):
        if audio is not None:
            self.sent_audio += len(audio.data)
        if text:
            self.texts.append(text)

    async def send_tool_response(self, function_responses):
        self.tool_responses.extend(function_responses)

    async def receive(self):
        turn = await self._turns.get()
        for m in turn:
            yield m


class FakeConnect:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_bridge_end_to_end(monkeypatch, tmp_path):
    audio_24k = b"\x00\x10" * 2400  # 100 ms @ 24 kHz
    script = [
        [
            server_msg(
                server_content=types.LiveServerContent(
                    model_turn=types.Content(
                        parts=[types.Part(inline_data=types.Blob(data=audio_24k, mime_type="audio/pcm"))]
                    ),
                    output_transcription=types.Transcription(text="こんにちは、AIアシスタントです。"),
                )
            ),
            server_msg(server_content=types.LiveServerContent(interrupted=True)),
            server_msg(
                server_content=types.LiveServerContent(
                    input_transcription=types.Transcription(text="予約できますか")
                )
            ),
            server_msg(server_content=types.LiveServerContent(turn_complete=True)),
        ],
        [
            server_msg(
                tool_call=types.LiveServerToolCall(
                    function_calls=[
                        types.FunctionCall(
                            id="f1", name="end_call", args={"summary": "予約OK", "outcome": "success"}
                        )
                    ]
                )
            ),
        ],
    ]
    session = FakeSession(script)
    fake_client = NS(aio=NS(live=NS(connect=lambda **kw: FakeConnect(session))))
    monkeypatch.setattr(gemini_bridge.genai, "Client", lambda **kw: fake_client)
    monkeypatch.setattr(gemini_bridge, "SILENCE_BEFORE_HANGUP", 0.05)

    settings = load_settings()
    store = CallStore(tmp_path)
    call = store.new(direction="outbound", to="+819012345678", from_="1", goal="予約")
    call.call_uuid = "u-1"
    ws = FakeWS()
    for _ in range(3):
        await ws.inbox.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    hangups = []

    async def hangup(c):
        hangups.append(c.call_uuid)
        return True

    async def dtmf(c, d):
        pass

    bridge = gemini_bridge.Bridge(ws, call, settings, store, hangup, dtmf)
    await asyncio.wait_for(bridge.run(), 5)

    assert session.texts and "相手が電話に出ました" in session.texts[0]
    assert session.sent_audio >= 1280  # caller audio forwarded
    assert ws.sent_bytes and all(len(b) == 640 for b in ws.sent_bytes)
    assert {"action": "clear"} in ws.sent_text
    assert any(m.get("action") == "notify" for m in ws.sent_text)
    assert hangups == ["u-1"]
    assert call.outcome == "success" and call.summary == "予約OK"
    roles = [line["role"] for line in call.transcript]
    assert roles == ["agent", "callee"]
    assert session.tool_responses[0].scheduling == types.FunctionResponseScheduling.SILENT
