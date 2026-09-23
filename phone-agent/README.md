# phone-agent

Compare **Twilio** and **Plivo** outbound test calls, and run real AI conversations through **Vonage + Gemini Live**.

Phase 1 (Twilio / Plivo): speak one Japanese line, then hang up.  
Phase 2 (Vonage): `vonage-agent/` bridges the call audio to Gemini Live (`gemini-3.8-live`) and manages the
Cloudflare quick tunnel + Vonage webhook URLs automatically. See [vonage-agent/README.md](vonage-agent/README.md).

## Vonage + Gemini Live（AI会話）

```bash
cd vonage-agent && ./setup.sh && ./vonage doctor && ./vonage serve --detach && cd ..
./call_phone_vonage --dry-run +819012345678 "明日19時に2名で予約できるか確認"
./call_phone_vonage +819012345678 "明日19時に2名で予約できるか確認" --wait   # live only after explicit 「発信して」
./phone --provider vonage --dry-run +819012345678 "目的"                     # same thing via the unified CLI
```

## Twilio版の実行方法

```bash
cd /workspace/phone-agent
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
./phone --provider vonage --dry-run +819012345678 "目的"
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
Vonage: `vonage-agent/.env`（`vonage-agent/.env.example` 参照）

Never commit `.env`.

## ログの見分け

- Twilio live: `call placed sid=CAxxxx to=+81...`
- Plivo dry-run: `provider=plivo` then `dry-run ok`
- Plivo live: `provider=plivo` and `call placed provider=plivo call_id=...`
- Vonage dry-run: `dry-run ok` + NCCO preview; live: `call placed uuid=... status=started`
- Failures: Twilio raises API exceptions; Plivo prints `Plivo API error: ...`; Vonage prints `error <http status>: <detail>`

## Billing

- Number rental / purchase
- Each live `calls.create` (minutes + TTS)
- `--dry-run` does not place a call
