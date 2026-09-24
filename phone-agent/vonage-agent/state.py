"""Small persistent state (webhook token, last synced URL) kept in DATA_DIR/state.json."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any


class State:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "state.json"
        self._data: dict[str, Any] = {}
        try:
            self._data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            self._data = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def update(self, **values: Any) -> None:
        self._data.update(values)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.path)

    def webhook_token(self, configured: str) -> str:
        """Secret path segment for webhooks. Stable across restarts."""
        if configured:
            return configured
        token = self.get("webhook_token")
        if not token:
            token = secrets.token_urlsafe(18)
            self.update(webhook_token=token)
        return token
