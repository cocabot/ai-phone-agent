"""Bridge one Vonage WebSocket call leg to a Gemini Live session.

Audio flow
----------
Vonage -> Gemini : 16-bit PCM frames at ``VONAGE_AUDIO_RATE`` are forwarded as
                   ``audio/pcm;rate=<rate>`` blobs (Gemini resamples input).
Gemini -> Vonage : 24 kHz PCM is resampled to the Vonage rate, cut into 20 ms
                   frames and sent as binary WebSocket messages.

Barge-in: when Gemini reports ``interrupted`` we send Vonage's ``clear``
command so the queued audio stops immediately.

Ending the call: the model calls the ``end_call`` tool after saying goodbye;
we wait for Vonage to finish playing the queued audio (``notify`` command)
and then hang up the PSTN leg through the Voice API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from audio import FRAME_MS, GEMINI_OUTPUT_RATE, FrameChunker, LinearResampler, frame_bytes
from calls import CallRecord
from config import Settings

log = logging.getLogger("vonage-agent.bridge")

MAX_PLAYBACK_BACKLOG_S = 40.0  # Vonage buffers ~60 s; stay well below it
INACTIVITY_TIMEOUT_S = 45.0  # no frames from Vonage for this long -> give up
HANGUP_FALLBACK_S = 12.0  # hang up even if Vonage never acknowledges playback end

HangupCallback = Callable[[CallRecord, str], Awaitable[None]]
DtmfCallback = Callable[[CallRecord, str], Awaitable[bool]]


def build_tools() -> list[types.Tool]:
    end_call = types.FunctionDeclaration(
        name="end_call",
        description=(
            "通話を終了します。用件が済んでお別れの挨拶を言い終えた直後、"
            "または相手が通話終了を希望したときに必ず呼び出してください。"
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "summary": types.Schema(
                    type="STRING",
                    description="通話結果の要約（日本語で2〜3文）。相手の回答、決まったこと、次のアクションを含める。",
                ),
                "outcome": types.Schema(
                    type="STRING",
                    enum=["success", "partial", "failed", "declined", "voicemail", "wrong_number"],
                    description="通話目的が達成できたか。",
                ),
            },
            required=["summary", "outcome"],
        ),
    )
    press_keys = types.FunctionDeclaration(
        name="press_keys",
        description=(
            "自動音声ガイダンス（IVR）でプッシュボタン操作を求められたときに、DTMF信号を送ります。"
            "0-9、*、# を組み合わせた文字列を指定します。"
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "digits": types.Schema(type="STRING", description="送信するボタン。例: '1' や '2#'"),
            },
            required=["digits"],
        ),
    )
    return [types.Tool(function_declarations=[end_call, press_keys])]


def build_system_instruction(record: CallRecord, settings: Settings) -> str:
    outbound = record.direction == "outbound"
    lines = [
        f"あなたは電話で会話するAIアシスタント「{settings.agent_name}」です。",
        "今、電話がつながり相手が応答しました。" if outbound else "今、電話がかかってきてあなたが応答しました。",
        "必ず日本語で、電話にふさわしい自然で簡潔な話し言葉で話してください。一度に長く話しすぎず、相手の返答を待ってください。",
        "最初の発話で、自分がAIアシスタントであることを明確に名乗ってください。人間になりすましたり、権限・身分・事実を捏造してはいけません。",
        "相手が聞き取れなかった様子なら、ゆっくり言い直してください。数字・日時・固有名詞は復唱して確認してください。",
        "通話目的に必要な確認だけを行い、不必要な個人情報を聞かないでください。",
        "料金・契約・購入など新たな金銭的義務を勝手に確定しないでください。判断が必要な場合は「確認して折り返します」と伝えてください。",
        "留守番電話や自動応答につながったと判断したら、短い伝言を残して end_call を呼び出してください。",
        "自動音声ガイダンスでボタン操作を求められたら press_keys ツールで操作してください。",
        "用件が済んだら、お礼とお別れの挨拶を述べ、その直後に必ず end_call ツールを呼び出してください。相手が終了を希望した場合も同様です。",
        f"今回の通話目的: {record.objective}",
    ]
    if record.instructions:
        lines.append(f"追加指示: {record.instructions}")
    if settings.agent_extra_instructions:
        lines.append(settings.agent_extra_instructions)
    return "\n".join(lines)


def kickoff_text(record: CallRecord) -> str:
    if record.direction == "outbound":
        return (
            "（システム通知）電話がつながり、相手が応答しました。あなたから先に日本語で話し始めてください。"
            "AIアシスタントであることを名乗り、通話目的を一文で伝えてから会話を進めてください。"
        )
    return "（システム通知）着信に応答しました。あなたから先に日本語で名乗り、ご用件を伺ってください。"


class GeminiVoiceBridge:
    def __init__(
        self,
        websocket: WebSocket,
        record: CallRecord,
        settings: Settings,
        *,
        on_hangup: HangupCallback,
        on_dtmf: DtmfCallback | None = None,
    ):
        self.ws = websocket
        self.record = record
        self.s = settings
        self.on_hangup = on_hangup
        self.on_dtmf = on_dtmf

        self.rate = settings.audio_rate
        self.frame_size = frame_bytes(self.rate)
        self.resampler = LinearResampler(GEMINI_OUTPUT_RATE, self.rate)
        self.chunker = FrameChunker(self.frame_size)

        self.bytes_in = 0
        self.bytes_out = 0
        self._playhead = 0.0
        self._last_vonage_frame = time.monotonic()
        self._hangup_pending = False
        self._hangup_started = False
        self._notify_waiter: asyncio.Future[None] | None = None
        self._end_reason: str | None = None

    # ------------------------------------------------------------------ setup

    def _live_config(self) -> types.LiveConnectConfig:
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=build_system_instruction(self.record, self.s),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.s.gemini_voice)
                )
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            tools=build_tools(),
            # Lifts the 15-minute audio session cap; the Vonage length_timer stays the hard limit.
            context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
        )

    # -------------------------------------------------------------------- run

    async def run(self) -> str:
        """Run the bridge until either side ends. Returns the end reason."""
        client = genai.Client(api_key=self.s.gemini_api_key)
        try:
            async with client.aio.live.connect(model=self.s.gemini_model, config=self._live_config()) as session:
                self.record.gemini_connected = True
                log.info("gemini session open call_ref=%s model=%s", self.record.call_ref, self.s.gemini_model)
                await session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text=kickoff_text(self.record))]),
                    turn_complete=True,
                )
                tasks = {
                    asyncio.create_task(self._pump_vonage_to_gemini(session), name="vonage->gemini"),
                    asyncio.create_task(self._pump_gemini_to_vonage(session), name="gemini->vonage"),
                    asyncio.create_task(self._watchdog(), name="watchdog"),
                }
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                results: list[str] = []
                for task in done:
                    exc = task.exception()
                    if exc is not None:
                        raise exc
                    result = task.result()
                    if isinstance(result, str):
                        results.append(result)
                if results and not self._end_reason:
                    self._end_reason = "agent_end_call" if "agent_end_call" in results else results[0]
        except genai_errors.APIError as exc:
            self.record.error = f"Gemini API error {exc.code}: {exc.message}"
            log.error("gemini api error call_ref=%s code=%s message=%s", self.record.call_ref, exc.code, exc.message)
            self._end_reason = self._end_reason or "gemini_error"
        except WebSocketDisconnect:
            self._end_reason = self._end_reason or "vonage_disconnected"
        except Exception as exc:  # noqa: BLE001 - report anything else on the call record
            self.record.error = f"{type(exc).__name__}: {exc}"
            log.exception("bridge failed call_ref=%s", self.record.call_ref)
            self._end_reason = self._end_reason or "bridge_error"
        finally:
            self.record.gemini_connected = False
            self.record.usage["audio_bytes_in"] = self.bytes_in
            self.record.usage["audio_bytes_out"] = self.bytes_out
            await self._close_ws()
        return self._end_reason or "ended"

    # ------------------------------------------------------------ vonage side

    async def _pump_vonage_to_gemini(self, session: Any) -> str:
        mime = f"audio/pcm;rate={self.rate}"
        while True:
            message = await self.ws.receive()
            kind = message.get("type")
            if kind == "websocket.disconnect":
                return "vonage_disconnected"
            data = message.get("bytes")
            if data:
                self.bytes_in += len(data)
                self._last_vonage_frame = time.monotonic()
                await session.send_realtime_input(audio=types.Blob(data=data, mime_type=mime))
                continue
            text = message.get("text")
            if text:
                await self._handle_vonage_text(session, text)

    async def _handle_vonage_text(self, session: Any, text: str) -> None:
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            log.info("vonage text frame (non-json) len=%s", len(text))
            return
        if not isinstance(event, dict):
            return
        name = str(event.get("event") or "")
        if name == "websocket:connected":
            log.info("vonage websocket connected content-type=%s", event.get("content-type"))
        elif name == "websocket:cleared":
            log.debug("vonage playback buffer cleared")
        elif name == "websocket:notify":
            payload = event.get("payload") or {}
            if payload.get("kind") == "end_call" and self._notify_waiter and not self._notify_waiter.done():
                self._notify_waiter.set_result(None)
        elif name == "websocket:dtmf":
            digit = str(event.get("digit") or "")
            log.info("dtmf received digit=%s", digit)
            if digit:
                await session.send_realtime_input(text=f"（相手がプッシュボタン {digit} を押しました）")
        else:
            log.info("vonage event %s", name or "(unknown)")

    async def _send_frame(self, frame: bytes) -> None:
        now = time.monotonic()
        if self._playhead < now:
            self._playhead = now
        backlog = self._playhead - now
        if backlog > MAX_PLAYBACK_BACKLOG_S:
            await asyncio.sleep(backlog - MAX_PLAYBACK_BACKLOG_S)
        await self.ws.send_bytes(frame)
        self.bytes_out += len(frame)
        self._playhead += FRAME_MS / 1000

    async def _clear_playback(self) -> None:
        self.chunker.clear()
        self.resampler.reset()
        self._playhead = time.monotonic()
        if self.ws.client_state == WebSocketState.CONNECTED:
            await self.ws.send_text(json.dumps({"action": "clear"}))

    async def _close_ws(self) -> None:
        try:
            if self.ws.client_state == WebSocketState.CONNECTED and self.ws.application_state == WebSocketState.CONNECTED:
                await self.ws.close(code=1000)
        except Exception:  # noqa: BLE001 - socket may already be gone
            pass

    # ------------------------------------------------------------ gemini side

    async def _pump_gemini_to_vonage(self, session: Any) -> str:
        while True:
            # receive() ends at every turn boundary; keep pulling until the session closes.
            async for message in session.receive():
                result = await self._handle_gemini_message(session, message)
                if result:
                    return result
            if self._hangup_pending and not self._hangup_started:
                return await self._finish_call()

    async def _handle_gemini_message(self, session: Any, message: types.LiveServerMessage) -> str | None:
        if message.data:
            pcm = self.resampler.process(message.data)
            for frame in self.chunker.push(pcm):
                await self._send_frame(frame)

        content = message.server_content
        if content is not None:
            if content.interrupted:
                log.info("barge-in: clearing vonage playback call_ref=%s", self.record.call_ref)
                await self._clear_playback()
                self.record.close_transcript_turn("agent")
            if content.input_transcription and content.input_transcription.text:
                self.record.add_transcript("user", content.input_transcription.text)
                if self.s.transcript_log:
                    log.info("transcript user: %s", content.input_transcription.text)
            if content.output_transcription and content.output_transcription.text:
                self.record.add_transcript("agent", content.output_transcription.text)
                if self.s.transcript_log:
                    log.info("transcript agent: %s", content.output_transcription.text)
            if content.turn_complete:
                tail = self.chunker.flush()
                if tail:
                    await self._send_frame(tail)
                self.record.close_transcript_turn()

        if message.tool_call and message.tool_call.function_calls:
            await self._handle_tool_calls(session, message.tool_call.function_calls)

        if message.usage_metadata and message.usage_metadata.total_token_count is not None:
            self.record.usage["gemini_total_tokens"] = message.usage_metadata.total_token_count

        if message.go_away is not None:
            log.warning("gemini go_away time_left=%s call_ref=%s", message.go_away.time_left, self.record.call_ref)
        return None

    async def _handle_tool_calls(self, session: Any, calls: list[types.FunctionCall]) -> None:
        responses: list[types.FunctionResponse] = []
        for call in calls:
            args = dict(call.args or {})
            if call.name == "end_call":
                self.record.outcome = {
                    "summary": str(args.get("summary") or ""),
                    "outcome": str(args.get("outcome") or "unknown"),
                    "reported_at": time.time(),
                }
                self._hangup_pending = True
                log.info("end_call requested outcome=%s call_ref=%s", self.record.outcome["outcome"], self.record.call_ref)
                responses.append(
                    types.FunctionResponse(
                        id=call.id,
                        name=call.name,
                        response={"result": "ok", "note": "通話は挨拶の再生後に終了します。これ以上話す必要はありません。"},
                        scheduling=types.FunctionResponseScheduling.SILENT,
                    )
                )
            elif call.name == "press_keys":
                digits = "".join(ch for ch in str(args.get("digits") or "") if ch in "0123456789*#")
                ok = False
                if digits and self.on_dtmf:
                    try:
                        ok = await self.on_dtmf(self.record, digits)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("dtmf send failed: %s", exc)
                responses.append(
                    types.FunctionResponse(
                        id=call.id,
                        name=call.name,
                        response={"result": "ok" if ok else "failed", "digits": digits},
                        scheduling=types.FunctionResponseScheduling.SILENT,
                    )
                )
            else:
                responses.append(
                    types.FunctionResponse(id=call.id, name=call.name, response={"error": f"unknown tool {call.name}"})
                )
        if responses:
            await session.send_tool_response(function_responses=responses)

    # ------------------------------------------------------------- hangup

    async def _finish_call(self) -> str:
        """Wait for queued audio to finish playing, then hang up."""
        self._hangup_started = True
        loop = asyncio.get_running_loop()
        self._notify_waiter = loop.create_future()
        backlog = max(self._playhead - time.monotonic(), 0.0)
        try:
            if self.ws.client_state == WebSocketState.CONNECTED:
                await self.ws.send_text(json.dumps({"action": "notify", "payload": {"kind": "end_call"}}))
            await asyncio.wait_for(self._notify_waiter, timeout=backlog + HANGUP_FALLBACK_S)
        except asyncio.TimeoutError:
            log.info("playback-end notify not received; hanging up anyway")
        except Exception:  # noqa: BLE001
            pass
        # Vonage may buffer a little beyond the notify; give the goodbye a moment to land.
        await asyncio.sleep(0.5)
        await self.on_hangup(self.record, "agent_end_call")
        return "agent_end_call"

    async def _watchdog(self) -> str:
        while True:
            await asyncio.sleep(5)
            idle = time.monotonic() - self._last_vonage_frame
            if idle > INACTIVITY_TIMEOUT_S:
                log.warning("no audio from vonage for %.0fs; ending bridge call_ref=%s", idle, self.record.call_ref)
                return "vonage_inactive"
