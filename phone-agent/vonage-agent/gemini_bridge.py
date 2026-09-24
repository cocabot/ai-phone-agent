"""Bridge a Vonage media WebSocket <-> Gemini Live API session.

Vonage  --(L16 PCM, 20 ms binary frames)-->  bridge  --(audio/pcm;rate=R)-->  Gemini Live
Vonage  <--(L16 PCM @ R, 20 ms frames)----  bridge  <--(PCM 24 kHz)---------  Gemini Live

Barge-in: when Gemini reports `interrupted`, the bridge sends {"action":"clear"} so
Vonage drops the audio it has buffered. Hang-up waits for buffered speech to finish
by sending {"action":"notify"} and hanging up when Vonage echoes it back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types

from audio import GEMINI_OUTPUT_RATE, Framer, Resampler
from calls import CallSession, CallStore
from config import Settings
from prompts import INBOUND_KICKOFF, OUTBOUND_KICKOFF, build_instructions

log = logging.getLogger("vonage-agent.bridge")

END_CALL_NOTIFY = {"action": "notify", "payload": {"agent": "end_call"}}
SILENCE_BEFORE_HANGUP = 1.2  # seconds without new model audio before we flush + notify
NOTIFY_TIMEOUT = 30.0
WRAP_UP_WARNING = 30  # seconds before MAX_CALL_SECONDS

TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="end_call",
                description="最後の挨拶を言い終えた後に呼び出し、電話を切る。",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "summary": types.Schema(type=types.Type.STRING, description="通話結果の日本語の要約"),
                        "outcome": types.Schema(
                            type=types.Type.STRING,
                            enum=["success", "partial", "failed"],
                            description="用件の達成度",
                        ),
                    },
                    required=["summary", "outcome"],
                ),
            ),
            types.FunctionDeclaration(
                name="send_dtmf",
                description="自動音声案内（IVR）でプッシュボタンを押す。例: '1', '#', '123#'",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={"digits": types.Schema(type=types.Type.STRING, description="0-9, *, #")},
                    required=["digits"],
                ),
            ),
        ]
    )
]


def build_live_config(settings: Settings, instructions: str) -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=types.Content(parts=[types.Part(text=instructions)]),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=settings.gemini_voice)
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        tools=TOOLS,
    )


class Bridge:
    def __init__(
        self,
        ws: WebSocket,
        call: CallSession,
        settings: Settings,
        store: CallStore,
        hangup: Callable[[CallSession], Awaitable[bool]],
        dtmf: Callable[[CallSession, str], Awaitable[None]],
    ) -> None:
        self.ws = ws
        self.call = call
        self.s = settings
        self.store = store
        self.hangup_cb = hangup
        self.dtmf_cb = dtmf
        self.rate = settings.audio_rate
        self.resample = Resampler(GEMINI_OUTPUT_RATE, self.rate)
        self.framer = Framer(self.rate)
        self.session: Any = None
        self.last_model_audio = 0.0
        self.ending = asyncio.Event()
        self.notify_ack = asyncio.Event()
        self.done = asyncio.Event()
        self.bytes_in = 0
        self.bytes_out = 0

    # ------------------------------------------------------------------ run
    async def run(self) -> None:
        call = self.call
        instructions = build_instructions(
            direction=call.direction,
            goal=call.goal or self.s.inbound_goal,
            agent_name=self.s.agent_name,
            extra=self.s.extra_instructions,
            notes=call.notes,
        )
        client = genai.Client(api_key=self.s.gemini_api_key)
        config = build_live_config(self.s, instructions)
        log.info("call=%s connecting gemini model=%s", call.id, self.s.gemini_model)
        async with client.aio.live.connect(model=self.s.gemini_model, config=config) as session:
            self.session = session
            call.status = "in-progress"
            call.answered_at = call.answered_at or time.time()
            self.store.save(call)
            kickoff = OUTBOUND_KICKOFF if call.direction == "outbound" else INBOUND_KICKOFF
            await session.send_realtime_input(text=kickoff)

            tasks = [
                asyncio.create_task(self._vonage_to_gemini(), name="vonage->gemini"),
                asyncio.create_task(self._gemini_to_vonage(), name="gemini->vonage"),
                asyncio.create_task(self._watchdog(), name="watchdog"),
                asyncio.create_task(self._end_call_flow(), name="end-call"),
            ]
            try:
                await self.done.wait()
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        log.info("call=%s bridge closed audio_in=%sB audio_out=%sB", call.id, self.bytes_in, self.bytes_out)

    def _finish(self, reason: str) -> None:
        if not self.done.is_set():
            log.info("call=%s bridge finishing: %s", self.call.id, reason)
            self.done.set()

    # ------------------------------------------------------- vonage -> gemini
    async def _vonage_to_gemini(self) -> None:
        mime = f"audio/pcm;rate={self.rate}"
        pending = bytearray()
        batch = self.framer.frame_bytes * 2  # ~40 ms per send
        try:
            while True:
                msg = await self.ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                data = msg.get("bytes")
                if data:
                    self.bytes_in += len(data)
                    pending.extend(data)
                    if len(pending) >= batch:
                        await self.session.send_realtime_input(
                            audio=types.Blob(data=bytes(pending), mime_type=mime)
                        )
                        pending.clear()
                elif msg.get("text"):
                    await self._on_vonage_text(msg["text"])
        except WebSocketDisconnect:
            pass
        finally:
            self._finish("vonage websocket closed")

    async def _on_vonage_text(self, text: str) -> None:
        try:
            event = json.loads(text)
        except ValueError:
            return
        name = str(event.get("event", ""))
        if name == "websocket:dtmf":
            digit = event.get("digit") or event.get("dtmf", {}).get("digits", "")
            await self.session.send_realtime_input(
                text=f"（システム: 相手がプッシュボタン「{digit}」を押しました）"
            )
        elif name == "websocket:notify" or (event.get("payload") or {}).get("agent") == "end_call":
            self.notify_ack.set()

    # ------------------------------------------------------- gemini -> vonage
    async def _send_frames(self, frames: list[bytes]) -> None:
        for frame in frames:
            await self.ws.send_bytes(frame)
            self.bytes_out += len(frame)

    async def _gemini_to_vonage(self) -> None:
        try:
            while not self.done.is_set():
                got_any = False
                async for msg in self.session.receive():
                    got_any = True
                    await self._on_gemini_message(msg)
                if not got_any:
                    break  # connection closed
        except Exception as exc:
            log.warning("call=%s gemini receive error: %s", self.call.id, exc)
            self.call.error = self.call.error or f"gemini: {exc}"[:300]
        finally:
            self._finish("gemini session closed")

    async def _on_gemini_message(self, msg: types.LiveServerMessage) -> None:
        sc = msg.server_content
        if sc:
            if sc.interrupted:
                self.framer.clear()
                self.resample.reset()
                await self.ws.send_text(json.dumps({"action": "clear"}))
            if sc.model_turn and sc.model_turn.parts:
                for part in sc.model_turn.parts:
                    blob = part.inline_data
                    if blob and blob.data:
                        self.last_model_audio = time.monotonic()
                        await self._send_frames(self.framer.push(self.resample(blob.data)))
            if sc.input_transcription and sc.input_transcription.text:
                self.call.add_line(
                    "callee" if self.call.direction == "outbound" else "caller", sc.input_transcription.text
                )
            if sc.output_transcription and sc.output_transcription.text:
                self.call.add_line("agent", sc.output_transcription.text)
            if sc.turn_complete or sc.generation_complete:
                await self._send_frames(self.framer.flush())
            if sc.turn_complete:
                self.call.close_turn()
                self.store.save(self.call)
        if msg.tool_call and msg.tool_call.function_calls:
            await self._on_tool_calls(msg.tool_call.function_calls)
        if msg.go_away:
            log.info("call=%s gemini go_away time_left=%s", self.call.id, msg.go_away.time_left)

    async def _on_tool_calls(self, calls: list[types.FunctionCall]) -> None:
        responses = []
        for fc in calls:
            args = fc.args or {}
            result: dict[str, Any] = {"result": "ok"}
            scheduling = None
            if fc.name == "end_call":
                self.call.summary = str(args.get("summary", ""))[:2000]
                self.call.outcome = str(args.get("outcome", ""))
                self.store.save(self.call)
                self.ending.set()
                scheduling = types.FunctionResponseScheduling.SILENT
            elif fc.name == "send_dtmf":
                digits = "".join(c for c in str(args.get("digits", "")) if c in "0123456789*#")
                try:
                    await self.dtmf_cb(self.call, digits)
                    self.call.add_line("agent", f"[DTMF {digits}]")
                except Exception as exc:
                    result = {"error": str(exc)[:200]}
            else:
                result = {"error": f"unknown tool {fc.name}"}
            responses.append(
                types.FunctionResponse(id=fc.id, name=fc.name, response=result, scheduling=scheduling)
            )
        await self.session.send_tool_response(function_responses=responses)

    # ----------------------------------------------------------- ending
    async def _end_call_flow(self) -> None:
        await self.ending.wait()
        # Let the model finish its goodbye (audio may still be streaming in).
        while time.monotonic() - self.last_model_audio < SILENCE_BEFORE_HANGUP:
            await asyncio.sleep(0.2)
        await self._send_frames(self.framer.flush())
        # Vonage echoes the notify back once everything queued before it has played.
        await self.ws.send_text(json.dumps(END_CALL_NOTIFY))
        try:
            await asyncio.wait_for(self.notify_ack.wait(), NOTIFY_TIMEOUT)
        except asyncio.TimeoutError:
            log.info("call=%s notify ack timeout; hanging up anyway", self.call.id)
        await self.hangup_cb(self.call)
        self._finish("end_call")

    async def _watchdog(self) -> None:
        limit = self.s.max_call_seconds
        if limit <= 0:
            return
        if limit > WRAP_UP_WARNING * 2:
            await asyncio.sleep(limit - WRAP_UP_WARNING)
            if not self.ending.is_set():
                await self.session.send_realtime_input(
                    text="（システム: 通話時間の上限が近づいています。要点をまとめ、挨拶して end_call を呼んでください）"
                )
            await asyncio.sleep(WRAP_UP_WARNING)
        else:
            await asyncio.sleep(limit)
        log.info("call=%s max duration reached", self.call.id)
        if not self.call.summary:
            self.call.summary = "通話時間の上限に達したため終了しました。"
        await self.hangup_cb(self.call)
        self._finish("max duration")


async def run_echo(ws: WebSocket, call: CallSession) -> None:
    """Connectivity test without Gemini: echo caller audio back."""
    total = 0
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes"):
                total += len(msg["bytes"])
                await ws.send_bytes(msg["bytes"])
    except WebSocketDisconnect:
        pass
    log.info("call=%s echo closed audio_bytes=%s", call.id, total)
