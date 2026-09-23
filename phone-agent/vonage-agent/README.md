# vonage-agent

Vonage Voice API → Answer webhook → WebSocket media path (connectivity test).
No AI bridge yet. Do not place calls without explicit Owner permission.

## Setup

```bash
cd /workspace/phone-agent/vonage-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Put Application private key file on disk, set VONAGE_PRIVATE_KEY_PATH
# Set VONAGE_PUBLIC_DOMAIN to tunnel hostname (no scheme)
```

## Run locally

```bash
PORT=8090 .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8090
```

## Endpoints

- `GET /health`
- `GET /webhooks/answer` → NCCO with `wss://$VONAGE_PUBLIC_DOMAIN/socket`
- `POST /webhooks/event`
- `WS /socket` → counts inbound audio bytes

## Vonage Console (next steps, manual)

- Answer URL: `https://<PUBLIC_DOMAIN>/webhooks/answer`
- Event URL: `https://<PUBLIC_DOMAIN>/webhooks/event`
- Application private key file path via `VONAGE_PRIVATE_KEY_PATH`
- Do not paste private key body into chat

## Echo (optional)

`VONAGE_ECHO=1` echoes binary frames back (off by default).
