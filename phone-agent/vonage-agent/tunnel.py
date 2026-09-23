"""Supervise a Cloudflare quick tunnel (``cloudflared tunnel --url ...``).

Quick tunnels get a new ``https://<random>.trycloudflare.com`` hostname every
time cloudflared starts. This module keeps cloudflared running, scrapes the
hostname from its log output, restarts it when it dies, and reports every new
URL through a callback so the server can re-sync the Vonage webhooks.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("vonage-agent.tunnel")

TRYCLOUDFLARE_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)


def extract_quick_tunnel_url(line: str) -> str | None:
    """Return the ``https://*.trycloudflare.com`` URL contained in a log line, if any."""
    match = TRYCLOUDFLARE_RE.search(line)
    return match.group(0).lower() if match else None


class QuickTunnel:
    """Run cloudflared as a child process and keep it alive.

    ``on_url`` is invoked from a background thread every time a (new) public
    URL is discovered; ``on_down`` when the process exits. Callers that need
    to touch asyncio state should hop onto the loop themselves
    (``loop.call_soon_threadsafe``).
    """

    def __init__(
        self,
        *,
        local_url: str,
        cloudflared_bin: str = "cloudflared",
        extra_args: str = "",
        on_url: Callable[[str], None],
        on_down: Callable[[int | None], None] | None = None,
        log_path: Path | None = None,
        max_backoff: float = 30.0,
    ):
        self.local_url = local_url
        self.cloudflared_bin = cloudflared_bin
        self.extra_args = shlex.split(extra_args) if extra_args else []
        self.on_url = on_url
        self.on_down = on_down
        self.log_path = log_path
        self.max_backoff = max_backoff

        self.url: str | None = None
        self.status = "stopped"  # stopped | starting | up | down
        self.restarts = 0
        self.last_error: str | None = None
        self.started_at: float | None = None

        self._proc: subprocess.Popen[str] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # --- lifecycle --------------------------------------------------------

    def command(self) -> list[str]:
        return [self.cloudflared_bin, "tunnel", "--no-autoupdate", "--url", self.local_url, *self.extra_args]

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.status = "starting"
        self._thread = threading.Thread(target=self._supervise, name="cloudflared-supervisor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._terminate(timeout)
        if self._thread:
            self._thread.join(timeout=timeout)
        self.status = "stopped"

    def _terminate(self, timeout: float) -> None:
        with self._lock:
            proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()

    # --- supervisor loop --------------------------------------------------

    def _supervise(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            run_started = time.time()
            code = self._run_once()
            if self._stop.is_set():
                break
            if time.time() - run_started > 60:
                backoff = 2.0  # it ran fine for a while; do not punish a fresh crash
            self.status = "down"
            self.url = None
            if self.on_down:
                try:
                    self.on_down(code)
                except Exception:  # noqa: BLE001 - never let a callback kill the supervisor
                    log.exception("tunnel on_down callback failed")
            self.restarts += 1
            log.warning("cloudflared exited (code=%s); restarting in %.0fs", code, backoff)
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, self.max_backoff)

    def _run_once(self) -> int | None:
        cmd = self.command()
        log_file = None
        try:
            if self.log_path:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                log_file = self.log_path.open("a", encoding="utf-8")
            proc = subprocess.Popen(  # noqa: S603 - fixed binary, argument list
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            self.last_error = f"{self.cloudflared_bin} not found on PATH"
            log.error("%s. Install cloudflared or set CLOUDFLARED_BIN.", self.last_error)
            self._stop.set()
            return None
        except OSError as exc:
            self.last_error = str(exc)
            log.error("failed to start cloudflared: %s", exc)
            return None

        with self._lock:
            self._proc = proc
        self.status = "starting"
        self.started_at = time.time()
        log.info("cloudflared started pid=%s (%s)", proc.pid, " ".join(shlex.quote(c) for c in cmd))

        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if log_file:
                    log_file.write(line)
                    log_file.flush()
                url = extract_quick_tunnel_url(line)
                if url and url != self.url:
                    self.url = url
                    self.status = "up"
                    self.last_error = None
                    log.info("quick tunnel url: %s", url)
                    try:
                        self.on_url(url)
                    except Exception:  # noqa: BLE001
                        log.exception("tunnel on_url callback failed")
                elif "ERR" in line and "error" in line.lower():
                    self.last_error = line.strip()[-300:]
        finally:
            code = proc.wait()
            if log_file:
                log_file.close()
            with self._lock:
                self._proc = None
        return code

    # --- introspection ----------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        return {
            "status": self.status,
            "url": self.url,
            "restarts": self.restarts,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "command": " ".join(shlex.quote(c) for c in self.command()),
        }
