#!/usr/bin/env python3
"""Command line for the Vonage x Gemini Live phone agent.

./agent start                           サーバー + cloudflared を起動し、URLをVonageへ自動設定
./agent doctor                          設定・資格情報・Gemini接続をまとめてチェック
./agent test-gemini                     電話を使わずに Gemini Live の接続と音声生成を確認
./agent status                          公開URL / 同期状態 / 進行中の通話
./agent sync [--url URL]                VonageアプリのAnswer/Event URLを今すぐ再設定
./agent link-number [NUMBER]            Vonage番号をこのアプリに紐付け（着信用）
./agent call NUMBER "用件" [--wait]      AIが発信（--dry-run で発信せず確認）
./agent calls                           最近の通話一覧
./agent show ID                         通話の詳細と文字起こし
./agent hangup ID                       通話を切る
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave
from typing import Any

import httpx

from config import load_settings

settings = load_settings()


# ------------------------------------------------------------------ helpers
def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_token}"} if settings.api_token else {}


def api(method: str, path: str, timeout: float = 20, **kwargs: Any) -> Any:
    url = settings.local_api_base + path
    try:
        resp = httpx.request(method, url, headers=_headers(), timeout=timeout, **kwargs)
    except httpx.ConnectError:
        sys.exit(
            f"サーバーに接続できません ({settings.local_api_base})。先に `./agent start` を実行してください。"
        )
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        sys.exit(f"エラー (HTTP {resp.status_code}): {detail}")
    return resp.json()


def server_running() -> bool:
    try:
        return httpx.get(settings.local_api_base + "/health", timeout=2).is_success
    except httpx.HTTPError:
        return False


def dump(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def fmt_time(ts: float | None) -> str:
    return time.strftime("%m/%d %H:%M:%S", time.localtime(ts)) if ts else "-"


def print_call(c: dict[str, Any]) -> None:
    print(f"ID:       {c['id']}  ({c['direction']})")
    print(f"状態:     {c['status']}" + (f"  エラー: {c['error']}" if c.get("error") else ""))
    print(f"開始:     {fmt_time(c.get('created_at'))}   通話時間: {c.get('duration') or '-'} 秒")
    if c.get("goal"):
        print(f"用件:     {c['goal']}")
    if c.get("outcome") or c.get("summary"):
        print(f"結果:     [{c.get('outcome') or '-'}] {c.get('summary') or ''}")
    lines = c.get("transcript") or []
    if lines:
        print("--- 文字起こし ---")
        for line in lines:
            who = {"agent": "AI", "callee": "相手", "caller": "相手"}.get(line["role"], line["role"])
            print(f"[{line.get('t', 0):>6}s] {who}: {line['text']}")


# ------------------------------------------------------------------ commands
def cmd_start(args: argparse.Namespace) -> None:
    from main import serve

    serve()


def cmd_status(args: argparse.Namespace) -> None:
    data = api("GET", "/api/status")
    if args.json:
        return dump(data)
    print(f"公開URL:     {data['public_url'] or '(未確定)'}  [tunnel={data['tunnel_mode']}]")
    if data.get("tunnel_connected") is False:
        print(f"  ! トンネル未接続: {data.get('tunnel_error') or '接続待ち'}")
    sync = data.get("last_sync") or {}
    state = "OK" if sync.get("ok") else f"NG: {sync.get('error', '未実行')}"
    print(f"Vonage同期:  {state}  ({sync.get('at', '-')})")
    for k, v in (data.get("webhooks") or {}).items():
        print(f"  {k}: {v}")
    missing = [k for k, v in data["config"].items() if v is False and k != "auto_sync"]
    print("設定:        " + ("OK" if not missing else "未設定 → " + ", ".join(missing)))
    active = data.get("active_calls") or []
    print(f"進行中通話:  {len(active)}")
    for c in active:
        print(f"  {c['id']} {c['direction']} {c['status']}")


def cmd_sync(args: argparse.Namespace) -> None:
    if server_running():
        dump(api("POST", "/api/sync", json={"url": args.url}))
        return
    # Server is down: apply directly (needs --url).
    if not args.url:
        sys.exit("サーバー停止中は --url https://xxxx.trycloudflare.com を指定してください")
    from config import normalize_public_url
    from state import State
    from vonage_api import VonageClient, VonageError

    base = normalize_public_url(args.url)
    token = State(settings.data_dir).webhook_token(settings.webhook_token)
    try:
        res = VonageClient(settings).update_voice_webhooks(
            f"{base}/webhooks/{token}/answer", f"{base}/webhooks/{token}/event"
        )
    except VonageError as exc:
        sys.exit(str(exc))
    print(f"Vonage webhooks updated (changed={res['changed']}) -> {base}")


def cmd_link_number(args: argparse.Namespace) -> None:
    dump(api("POST", "/api/link-number", json={"number": args.number}))


def cmd_call(args: argparse.Namespace) -> None:
    body = {"to": args.number, "goal": args.goal, "notes": args.notes or "", "dry_run": args.dry_run}
    data = api("POST", "/api/calls", json=body)
    if args.dry_run:
        if args.json:
            return dump(data)
        print(f"dry-run ok: to={data['to']} from={data['from']} ready={data['ready']}")
        print(f"公開URL: {data['public_url'] or '(未確定)'}")
        missing = [k for k, v in data["config"].items() if v is False and k != "auto_sync"]
        if missing:
            print("未設定: " + ", ".join(missing))
        return
    print(f"call placed id={data['id']} status={data['status']}")
    if not args.wait:
        print(f"結果確認: ./agent show {data['id']}   (待つ場合: ./agent show {data['id']} --wait)")
        return
    wait_and_show(data["id"], args.json)


def wait_and_show(call_id: str, as_json: bool) -> None:
    print("通話終了を待っています…（Ctrl+C で待機だけ中断）", file=sys.stderr)
    deadline = time.monotonic() + settings.max_call_seconds + settings.ringing_timeout + 120
    data: dict[str, Any] = {}
    while time.monotonic() < deadline:
        data = api("GET", f"/api/calls/{call_id}?wait=60", timeout=90)
        if data.get("ended_at") or data.get("status") in {
            "completed",
            "busy",
            "cancelled",
            "failed",
            "rejected",
            "timeout",
            "unanswered",
            "machine",
        }:
            break
    dump(data) if as_json else print_call(data)


def cmd_calls(args: argparse.Namespace) -> None:
    rows = api("GET", f"/api/calls?limit={args.limit}")
    if args.json:
        return dump(rows)
    if not rows:
        print("通話履歴はありません")
    for c in rows:
        summary = (c.get("summary") or c.get("error") or "")[:40]
        print(f"{c['id']}  {c['direction']:<8} {c['status']:<11} {c.get('outcome') or '-':<8} {summary}")


def cmd_show(args: argparse.Namespace) -> None:
    if args.wait:
        return wait_and_show(args.id, args.json)
    data = api("GET", f"/api/calls/{args.id}")
    dump(data) if args.json else print_call(data)


def cmd_hangup(args: argparse.Namespace) -> None:
    dump(api("POST", f"/api/calls/{args.id}/hangup"))


def cmd_doctor(args: argparse.Namespace) -> None:
    from tunnel import find_cloudflared
    from vonage_api import VonageClient, VonageError, voice_webhooks_of

    s = settings
    ok = True

    def row(label: str, good: bool, note: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"[{'OK' if good else 'NG'}] {label}" + (f"  {note}" if note else ""))

    row("VONAGE_APPLICATION_ID", bool(s.application_id))
    row("秘密鍵ファイル", s.private_key_present, str(s.private_key_path))
    row("VONAGE_API_KEY / SECRET", bool(s.api_key and s.api_secret), "Webhook自動設定に必要")
    row("VONAGE_FROM_NUMBER", bool(s.from_number), "発信元（Vonageの番号）")
    row("GEMINI_API_KEY", bool(s.gemini_api_key), f"model={s.gemini_model}")
    cf = find_cloudflared(s.cloudflared_bin)
    row(
        "cloudflared",
        bool(cf) or bool(s.public_url),
        cf or ("PUBLIC_URL を使用" if s.public_url else "./setup.sh で取得可"),
    )

    if s.application_id and s.api_key and s.api_secret:
        try:
            app = VonageClient(s).get_application()
            answer, event = voice_webhooks_of(app)
            row("Vonage Application API", True, f"name={app.get('name')}")
            print(f"     answer_url: {answer or '(未設定)'}")
            print(f"     event_url:  {event or '(未設定)'}")
        except (VonageError, httpx.HTTPError) as exc:
            row("Vonage Application API", False, str(exc)[:200])

    if s.gemini_api_key:
        try:
            resp = httpx.get(
                f"https://generativelanguage.googleapis.com/v1beta/models/{s.gemini_model}",
                params={"key": s.gemini_api_key},
                timeout=15,
            )
            note = ""
            if not resp.is_success:
                try:
                    note = resp.json()["error"]["message"]
                except (ValueError, KeyError, TypeError):
                    note = resp.text[:160]
                note = f"HTTP {resp.status_code}: {note}"
            row(f"Gemini モデル確認 ({s.gemini_model})", resp.is_success, note)
        except httpx.HTTPError as exc:
            row("Gemini モデル確認", False, str(exc))

    row(
        "ローカルサーバー",
        server_running(),
        settings.local_api_base + ("" if server_running() else " (停止中)"),
    )
    sys.exit(0 if ok else 1)


def cmd_test_gemini(args: argparse.Namespace) -> None:
    """Talk to Gemini Live with text and save its spoken reply as a WAV file."""
    from google import genai

    from gemini_bridge import build_live_config
    from prompts import build_instructions

    if not settings.gemini_api_key:
        sys.exit("GEMINI_API_KEY が未設定です")

    async def run() -> None:
        client = genai.Client(api_key=settings.gemini_api_key)
        instructions = build_instructions(
            direction="outbound", goal="接続テスト。短く挨拶する。", agent_name=settings.agent_name
        )
        config = build_live_config(settings, instructions)
        pcm = bytearray()
        text: list[str] = []
        started = time.monotonic()
        first_audio = None
        async with client.aio.live.connect(model=settings.gemini_model, config=config) as session:
            await session.send_realtime_input(text=args.text)
            async for msg in session.receive():
                sc = msg.server_content
                if not sc:
                    continue
                if sc.model_turn:
                    for part in sc.model_turn.parts or []:
                        if part.inline_data and part.inline_data.data:
                            first_audio = first_audio or time.monotonic()
                            pcm.extend(part.inline_data.data)
                if sc.output_transcription and sc.output_transcription.text:
                    text.append(sc.output_transcription.text)
                if sc.turn_complete:
                    break
        out = settings.data_dir / "gemini_test.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(bytes(pcm))
        latency = f"{first_audio - started:.2f}s" if first_audio else "-"
        print(f"model={settings.gemini_model} audio={len(pcm) / 48000:.1f}s first_audio={latency}")
        print(f"transcript: {''.join(text) or '(なし)'}")
        print(f"saved: {out}")
        if not pcm:
            sys.exit("音声が返ってきませんでした")

    try:
        asyncio.run(run())
    except genai.errors.APIError as exc:
        sys.exit(
            f"Gemini エラー: {exc}\n（APIキー / GEMINI_MODEL={settings.gemini_model} / 無料枠の上限を確認）"
        )


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="JSONで出力")
    sub = p.add_subparsers(dest="cmd", required=True)
    _add = sub.add_parser
    sub.add_parser = lambda *a, **k: _add(*a, parents=[common], **k)  # type: ignore[method-assign]

    sub.add_parser("start", help="サーバー起動（トンネル・自動同期込み）").set_defaults(func=cmd_start)
    sub.add_parser("doctor", help="設定と接続をチェック").set_defaults(func=cmd_doctor)
    t = sub.add_parser("test-gemini", help="Gemini Liveだけをテスト（電話なし）")
    t.add_argument("text", nargs="?", default="こんにちは。接続テストです。一言で挨拶してください。")
    t.set_defaults(func=cmd_test_gemini)
    sub.add_parser("status", help="状態表示").set_defaults(func=cmd_status)
    s = sub.add_parser("sync", help="Vonage Webhook URLを再設定")
    s.add_argument("--url", help="公開URLを手動指定（例: https://xxxx.trycloudflare.com）")
    s.set_defaults(func=cmd_sync)
    ln = sub.add_parser("link-number", help="Vonage番号をアプリに紐付け（着信用）")
    ln.add_argument("number", nargs="?")
    ln.set_defaults(func=cmd_link_number)
    c = sub.add_parser("call", help="AIが発信")
    c.add_argument("number")
    c.add_argument("goal", help="用件（AIへの指示）")
    c.add_argument("--notes", help="補足情報（名前・予約内容など）")
    c.add_argument("--dry-run", action="store_true", help="発信せず確認だけ")
    c.add_argument("--wait", action="store_true", help="通話終了まで待って結果を表示")
    c.set_defaults(func=cmd_call)
    ls = sub.add_parser("calls", help="通話一覧")
    ls.add_argument("--limit", type=int, default=20)
    ls.set_defaults(func=cmd_calls)
    sh = sub.add_parser("show", help="通話の詳細")
    sh.add_argument("id")
    sh.add_argument("--wait", action="store_true")
    sh.set_defaults(func=cmd_show)
    h = sub.add_parser("hangup", help="通話を切る")
    h.add_argument("id")
    h.set_defaults(func=cmd_hangup)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
