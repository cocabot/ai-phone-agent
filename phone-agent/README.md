# phone-agent

音声プロバイダの発信テスト用Pythonツール群です。

- **AIと会話する電話エージェント（推奨）: [`vonage-agent/`](vonage-agent/README.md)** — Vonage Voice × Gemini Live。
  トンネルURLの自動反映、発信/着信、文字起こし・結果要約つき。ボット向け手順は [`vonage-agent/BOT_GUIDE.md`](vonage-agent/BOT_GUIDE.md)
- 以下の Twilio / Plivo スクリプトは、固定の日本語を1回話して切るだけの回線テストです。

## Twilio版の実行方法

```bash
cd phone-agent
./call_phone --dry-run +819012345678
# live only after explicit 「発信して」:
./call_phone +819012345678
```

Logs include `call placed sid=...` on success.

## Plivo版の実行方法

```bash
./call_phone_plivo --dry-run +819012345678
# live only after explicit 「Plivoで発信して」:
./call_phone_plivo +819012345678
```

Optional unified CLI (does not replace `./call_phone`):

```bash
./phone --provider twilio --dry-run +819012345678
./phone --provider plivo --dry-run +819012345678
```

Plivo needs a **public** `PLIVO_ANSWER_URL` that returns `static/plivo_answer.xml`.
Local helper:

```bash
./.venv/bin/python serve_plivo_answer.py   # :8787
# then expose with your tunnel and set PLIVO_ANSWER_URL
```

## 必要な環境変数

Twilio: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`  
Plivo: `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN`, `PLIVO_FROM_NUMBER`, `PLIVO_ANSWER_URL`

Never commit `.env`.

## ログの見分け

- Twilio live: `call placed sid=CAxxxx to=+81...`
- Plivo dry-run: `provider=plivo` then `dry-run ok`
- Plivo live: `provider=plivo` and `call placed provider=plivo call_id=...`
- Failures: Twilio raises API exceptions; Plivo prints `Plivo API error: ...`

## Billing

- Number rental / purchase
- Each live `calls.create` (minutes + TTS)
- `--dry-run` does not place a call
