"""In-memory call sessions, persisted as JSON under DATA_DIR/calls/."""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Vonage call statuses that mean the call is over.
FINAL_STATUSES = {
    "completed",
    "busy",
    "cancelled",
    "failed",
    "rejected",
    "timeout",
    "unanswered",
    "machine",
}


@dataclass
class CallSession:
    id: str
    direction: str  # outbound | inbound
    to: str
    from_: str
    goal: str
    notes: str = ""
    token: str = field(default_factory=lambda: secrets.token_urlsafe(16), repr=False)
    call_uuid: str = ""
    status: str = "created"
    created_at: float = field(default_factory=time.time)
    answered_at: float | None = None
    ended_at: float | None = None
    outcome: str = ""
    summary: str = ""
    error: str = ""
    transcript: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        return self.status in FINAL_STATUSES or self.ended_at is not None

    def add_line(self, role: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        # Merge consecutive fragments of the same speaker.
        if self.transcript and self.transcript[-1]["role"] == role and not self.transcript[-1].get("closed"):
            self.transcript[-1]["text"] += text
        else:
            self.transcript.append({"role": role, "text": text, "t": round(time.time() - self.created_at, 1)})

    def close_turn(self) -> None:
        if self.transcript:
            self.transcript[-1]["closed"] = True

    def public(self, full: bool = True) -> dict[str, Any]:
        data = asdict(self)
        data.pop("token", None)
        data["from"] = data.pop("from_")
        data["duration"] = (
            round((self.ended_at or time.time()) - self.answered_at, 1) if self.answered_at else None
        )
        for line in data["transcript"]:
            line.pop("closed", None)
        if not full:
            data.pop("transcript")
            data.pop("events")
        return data


class CallStore:
    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir / "calls"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._calls: dict[str, CallSession] = {}

    def new(self, **kwargs: Any) -> CallSession:
        cid = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        call = CallSession(id=cid, **kwargs)
        self._calls[cid] = call
        self.save(call)
        return call

    def get(self, cid: str) -> CallSession | None:
        call = self._calls.get(cid)
        if call:
            return call
        # Also allow lookups by prefix / Vonage uuid.
        for c in self._calls.values():
            if c.call_uuid == cid or c.id.startswith(cid):
                return c
        return None

    def by_uuid(self, call_uuid: str) -> CallSession | None:
        return next((c for c in self._calls.values() if c.call_uuid == call_uuid), None)

    def active(self, max_age: float = 900) -> list[CallSession]:
        """Unfinished calls. Stale ones (no final event ever arrived) are ignored."""
        cutoff = time.time() - max_age
        return [c for c in self._calls.values() if not c.finished and c.created_at >= cutoff]

    def save(self, call: CallSession) -> None:
        path = self.dir / f"{call.id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(call.public(), ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(path)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Recent calls, including ones from previous runs (read from disk)."""
        out: dict[str, dict[str, Any]] = {}
        for path in sorted(self.dir.glob("*.json"), reverse=True)[: limit * 2]:
            try:
                data = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            out[data["id"]] = data
        for c in self._calls.values():
            out[c.id] = c.public()
        rows = sorted(out.values(), key=lambda d: d.get("created_at", 0), reverse=True)[:limit]
        for r in rows:
            r.pop("transcript", None)
            r.pop("events", None)
        return rows

    def load(self, cid: str) -> dict[str, Any] | None:
        call = self.get(cid)
        if call:
            return call.public()
        matches = sorted(self.dir.glob(f"{cid}*.json"))
        if not matches:
            return None
        return json.loads(matches[-1].read_text("utf-8"))
