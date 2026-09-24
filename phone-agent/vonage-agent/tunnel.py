"""Discover the current public URL and notify when it changes.

Modes (TUNNEL env):
  auto        PUBLIC_URL があれば固定URL、CLOUDFLARED_METRICS があれば metrics 監視、
              cloudflared が見つかれば quick tunnel を自前で起動、どれも無ければ none
  cloudflared cloudflared quick tunnel を子プロセスとして起動し、URL変更を検知（落ちたら再起動）
  metrics     別途起動済みの cloudflared の metrics (/quicktunnel) をポーリング
  none        PUBLIC_URL をそのまま使う（トンネル管理しない）
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from config import BASE_DIR, Settings, normalize_public_url

log = logging.getLogger("vonage-agent.tunnel")

QUICK_TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
METRICS_POLL_SECONDS = 10

OnUrl = Callable[[str], Awaitable[None]]


def find_cloudflared(configured: str = "") -> str | None:
    candidates = [configured] if configured else []
    candidates += [str(BASE_DIR / "bin" / "cloudflared"), shutil.which("cloudflared") or ""]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


def extract_quick_tunnel_url(line: str) -> str | None:
    m = QUICK_TUNNEL_RE.search(line)
    # api.trycloudflare.com appears in error messages; it is not a tunnel URL.
    if m and not m.group(0).startswith("https://api."):
        return m.group(0)
    return None


class TunnelManager:
    def __init__(self, settings: Settings, on_url: OnUrl) -> None:
        self.s = settings
        self.on_url = on_url
        self.public_url = settings.public_url
        self.mode = self._resolve_mode()
        self.connected = False
        self.last_error = ""
        self._task: asyncio.Task[None] | None = None
        self._proc: asyncio.subprocess.Process | None = None

    def _resolve_mode(self) -> str:
        mode = self.s.tunnel
        if mode != "auto":
            return mode
        if self.s.public_url:
            return "none"
        if self.s.cloudflared_metrics:
            return "metrics"
        if find_cloudflared(self.s.cloudflared_bin):
            return "cloudflared"
        return "none"

    async def start(self) -> None:
        log.info("tunnel mode=%s", self.mode)
        if self.mode == "cloudflared":
            self._task = asyncio.create_task(self._run_cloudflared())
        elif self.mode == "metrics":
            self._task = asyncio.create_task(self._poll_metrics())
        elif self.public_url:
            await self._set(self.public_url)
        else:
            log.warning(
                "公開URLが不明です。PUBLIC_URL を設定するか cloudflared を入れてください（./setup.sh で自動取得できます）"
            )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
            except asyncio.TimeoutError:
                self._proc.kill()

    async def _set(self, url: str) -> None:
        url = normalize_public_url(url)
        if url == self.public_url and self.mode != "none":
            return
        self.public_url = url
        log.info("public url -> %s", url)
        try:
            await self.on_url(url)
        except Exception:  # never let a sync error kill the tunnel loop
            log.exception("public url callback failed")

    async def _run_cloudflared(self) -> None:
        binary = find_cloudflared(self.s.cloudflared_bin)
        if not binary:
            log.error("cloudflared が見つかりません（CLOUDFLARED_BIN か ./setup.sh）")
            return
        backoff = 2
        while True:
            cmd = [binary, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.s.port}"]
            if self.s.cloudflared_protocol:
                cmd += ["--protocol", self.s.cloudflared_protocol]
            if self.s.cloudflared_metrics:
                cmd += ["--metrics", self.s.cloudflared_metrics]
            log.info("starting cloudflared quick tunnel")
            self._proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            assert self._proc.stderr is not None
            self.connected = False
            async for raw in self._proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                url = extract_quick_tunnel_url(line)
                if url:
                    backoff = 2
                    await self._set(url)
                elif "Registered tunnel connection" in line:
                    if not self.connected:
                        log.info("cloudflared connected to Cloudflare edge")
                    self.connected, self.last_error = True, ""
                elif " ERR " in line or "ERROR:" in line:
                    msg = line.split(" ERR ", 1)[-1].split("ERROR:", 1)[-1].strip(" |")
                    if msg != self.last_error:
                        log.warning("cloudflared: %s", msg[:200])
                    self.last_error = msg[:200]
            self.connected = False
            code = await self._proc.wait()
            log.warning("cloudflared exited code=%s; restarting in %ss", code, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _poll_metrics(self) -> None:
        endpoint = f"http://{self.s.cloudflared_metrics}/quicktunnel"
        async with httpx.AsyncClient(timeout=5) as client:
            while True:
                try:
                    resp = await client.get(endpoint)
                    host = (resp.json() or {}).get("hostname") if resp.is_success else None
                    if host:
                        await self._set(host)
                except (httpx.HTTPError, ValueError) as exc:
                    log.debug("metrics poll failed: %s", type(exc).__name__)
                await asyncio.sleep(METRICS_POLL_SECONDS)
