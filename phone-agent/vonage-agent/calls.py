"""In-memory registry of calls handled by this process.

The registry is the single source of truth the CLI/API reads: it links the
Vonage call leg (uuid), the WebSocket leg (``call_ref``), Vonage status
events, the Gemini transcript and the outcome reported by the ``end_call``
tool. It is intentionally process-local; swap it for Redis/SQLite if you ever
run more than one instance.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from phone_numbers import mask_number

MAX_CALLS_KEPT = 200


@dataclass
class TranscriptEntry:
    role: str  # "user" | "agent"
    text: str
    ts: float
    final: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "text": self.text, "ts": self.ts}


@dataclass
class CallRecord:
    call_ref: str
    direction: str  # outbound | inbound
    to_number: str
    from_number: str
    objective: str
    instructions: str = ""
    uuid: str | None = None
    conversation_uuid: str | None = None
    status: str = "created"
    created_at: float = field(default_factory=time.time)
    answered_at: float | None = None
    ended_at: float | None = None
    end_reason: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    transcript: list[TranscriptEntry] = field(default_factory=list)
    outcome: dict[str, Any] | None = None
    ws_connected: bool = False
    gemini_connected: bool = False
    error: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)

    # --- transcript helpers ----------------------------------------------

    def add_transcript(self, role: str, text: str) -> None:
        if not text:
            return
        last = self.transcript[-1] if self.transcript else None
        if last and last.role == role and not last.final:
            last.text += text
        else:
            self.transcript.append(TranscriptEntry(role=role, text=text, ts=time.time()))

    def close_transcript_turn(self, role: str | None = None) -> None:
        for entry in reversed(self.transcript):
            if role is None or entry.role == role:
                entry.final = True
                if role is not None:
                    break

    def transcript_text(self) -> str:
        labels = {"user": "相手", "agent": "AI"}
        return "\n".join(f"{labels.get(e.role, e.role)}: {e.text.strip()}" for e in self.transcript if e.text.strip())

    # --- lifecycle helpers ----------------------------------------------

    @property
    def active(self) -> bool:
        return self.ended_at is None and self.status not in {"completed", "failed", "rejected", "busy", "cancelled", "timeout", "unanswered"}

    def note_event(self, payload: dict[str, Any]) -> None:
        status = str(payload.get("status") or payload.get("detail") or "")
        self.events.append(
            {
                "ts": time.time(),
                "status": status,
                "direction": payload.get("direction"),
                "uuid": payload.get("uuid"),
                "to": mask_number(str(payload.get("to") or "")),
                "timestamp": payload.get("timestamp"),
                "reason": payload.get("reason"),
                "duration": payload.get("duration"),
            }
        )
        # Only the PSTN leg drives the call status; the websocket leg reports
        # "to": "wss://..." events of its own.
        is_ws_leg = str(payload.get("to") or "").startswith(("ws://", "wss://"))
        if is_ws_leg:
            if status in {"unanswered", "failed", "disconnected"}:
                self.error = self.error or f"websocket leg {status}"
            return
        if status:
            self.status = status
        if status == "answered" and self.answered_at is None:
            self.answered_at = time.time()
        if status in {"completed", "failed", "rejected", "busy", "cancelled", "timeout", "unanswered"}:
            self.ended_at = self.ended_at or time.time()
            self.end_reason = self.end_reason or status
            if payload.get("duration") is not None:
                self.usage["vonage_duration_seconds"] = payload.get("duration")
            if payload.get("price") is not None:
                self.usage["vonage_price"] = payload.get("price")

    def mark_ended(self, reason: str) -> None:
        if self.ended_at is None:
            self.ended_at = time.time()
        self.end_reason = self.end_reason or reason

    def to_dict(self, *, include_transcript: bool = True) -> dict[str, Any]:
        duration = None
        if self.answered_at:
            duration = round((self.ended_at or time.time()) - self.answered_at)
        data: dict[str, Any] = {
            "call_ref": self.call_ref,
            "uuid": self.uuid,
            "conversation_uuid": self.conversation_uuid,
            "direction": self.direction,
            "to": self.to_number,
            "from": self.from_number,
            "objective": self.objective,
            "instructions": self.instructions or None,
            "status": self.status,
            "active": self.active,
            "created_at": self.created_at,
            "answered_at": self.answered_at,
            "ended_at": self.ended_at,
            "end_reason": self.end_reason,
            "duration_seconds": duration,
            "ws_connected": self.ws_connected,
            "gemini_connected": self.gemini_connected,
            "outcome": self.outcome,
            "error": self.error,
            "usage": self.usage,
            "events": self.events[-20:],
        }
        if include_transcript:
            data["transcript"] = [e.to_dict() for e in self.transcript]
            data["transcript_text"] = self.transcript_text()
        return data


class CallRegistry:
    def __init__(self) -> None:
        self._by_ref: dict[str, CallRecord] = {}
        self._order: list[str] = []

    def create(
        self,
        *,
        direction: str,
        to_number: str,
        from_number: str,
        objective: str,
        instructions: str = "",
    ) -> CallRecord:
        call_ref = secrets.token_urlsafe(12)
        record = CallRecord(
            call_ref=call_ref,
            direction=direction,
            to_number=to_number,
            from_number=from_number,
            objective=objective,
            instructions=instructions,
        )
        self._by_ref[call_ref] = record
        self._order.append(call_ref)
        self._prune()
        return record

    def _prune(self) -> None:
        while len(self._order) > MAX_CALLS_KEPT:
            oldest = self._order.pop(0)
            self._by_ref.pop(oldest, None)

    def by_ref(self, call_ref: str | None) -> CallRecord | None:
        if not call_ref:
            return None
        return self._by_ref.get(call_ref)

    def by_uuid(self, value: str | None) -> CallRecord | None:
        if not value:
            return None
        for record in self._by_ref.values():
            if record.uuid == value or record.conversation_uuid == value:
                return record
        return None

    def lookup(self, key: str) -> CallRecord | None:
        return self.by_ref(key) or self.by_uuid(key)

    def all(self, *, limit: int = 50) -> list[CallRecord]:
        refs = self._order[-limit:]
        return [self._by_ref[r] for r in reversed(refs) if r in self._by_ref]

    def active(self) -> list[CallRecord]:
        return [r for r in self._by_ref.values() if r.active]
