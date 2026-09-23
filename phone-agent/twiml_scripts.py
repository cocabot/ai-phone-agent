"""TwiML payloads. Keep Say-only test separate from future realtime voice hooks."""

from __future__ import annotations

from twilio.twiml.voice_response import VoiceResponse

# Phase 1: one-shot TTS, then hang up.
TEST_MESSAGE_JA = (
    "これはAI電話システムのテストです。正常に電話を発信できました。"
)


def build_test_say_twiml(message: str = TEST_MESSAGE_JA) -> str:
    """Minimal outbound test: speak Japanese, then end."""
    response = VoiceResponse()
    response.say(message, language="ja-JP", voice="Polly.Mizuki")
    response.hangup()
    return str(response)


def build_realtime_stub_twiml() -> str:
    """
    Placeholder for phase 2: stream / ConversationRelay toward Grok Voice.

    Do not use yet. Replace with Media Streams or ConversationRelay once
    a public webhook URL and Grok Voice bridge exist.
    """
    response = VoiceResponse()
    response.say(
        "リアルタイム会話はまだ未接続です。",
        language="ja-JP",
        voice="Polly.Mizuki",
    )
    response.hangup()
    return str(response)
