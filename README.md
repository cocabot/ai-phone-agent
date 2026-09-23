# AI Phone Agent JP

日本向けのAI電話エージェント用MCPプラグインです。

**Twilio Programmable Voice → Twilio SIP-out (Tokyo edge) → OpenAI Realtime SIP** で、AIエージェントから日本の電話番号へ発信して会話します。

## できること

- `make_phone_call`: 日本の電話番号へAIが1件発信
- `get_phone_call_status`: 発信状況・終了状況を確認
- `hangup_phone_call`: 進行中の通話を終了
- 日本語で会話
- 受電者へAI音声であることを冒頭で通知
- OpenAI Webhook署名を検証
- Twilio→OpenAI SIPの通話コンテキストを短い署名付きIDで保護
- 110 / 118 / 119 と、MVPで扱わない一部特殊番号をブロック
- 通話時間は最大15分

## 構成

```text
ChatGPT / AI Agent
        |
        | MCP make_phone_call
        v
ai-phone-agent-jp
        |
        | Twilio Calls API
        v
日本の電話番号
        |
        | 応答後に <Dial><Sip>
        v
Twilio SIP-out (edge=tokyo)
        |
        v
OpenAI Realtime SIP
        |
        | realtime.call.incoming webhook
        v
ai-phone-agent-jp が署名を検証して accept
        |
        v
AIが日本語で会話
```

## 前提

1. Twilioアカウント
2. 発信に使えるTwilio電話番号
3. OpenAI APIプロジェクト
4. HTTPSで公開できるNode.js実行環境

Twilioの日本番号取得・発信には、番号種別やアカウントに応じた規制要件が適用される場合があります。利用する番号の要件はTwilio Consoleで確認してください。

## セットアップ

```bash
npm install
cp .env.example .env
```

`.env`:

```env
MCP_BEARER_TOKEN=...
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=+81...
OPENAI_API_KEY=sk-...
OPENAI_PROJECT_ID=proj_...
OPENAI_WEBHOOK_SECRET=whsec_...
OPENAI_REALTIME_MODEL=gpt-realtime-2.1
CALL_CONTEXT_SECRET=...
```

ランダムなシークレット:

```bash
openssl rand -hex 32
```

## OpenAI Webhook

OpenAI Dashboardで次をWebhook URLに設定します。

```text
https://YOUR-DOMAIN/webhooks/openai
```

購読イベントに **`realtime.call.incoming`** を含め、発行された署名シークレットを `OPENAI_WEBHOOK_SECRET` に設定してください。

SIP送信先は次の形式です。

```text
sip:PROJECT_ID@sip.api.openai.com;transport=tls;edge=tokyo
```

TwilioのSIP URIには長さ制限があるため、通話目的の全文はSIPヘッダーに入れません。短い署名付きIDだけを送り、実際の目的・追加指示はサーバー側で一時保持します。

## MCP

MCPエンドポイント:

```text
https://YOUR-DOMAIN/mcp
```

このMVPは実際に電話を発信する副作用のあるツールなので、`MCP_BEARER_TOKEN` を必須にしています。公開配布する場合はOAuth認証へ置き換えてください。

`mcp.json` の `YOUR-DOMAIN` を実際のデプロイ先に変更します。

## ローカル起動

```bash
npm run dev
```

```bash
curl http://localhost:3000/health
```

## デプロイ時の重要事項

現在のMVPでは、発信開始からOpenAIのSIP Webhookが届くまでの短時間だけ、通話目的をNode.jsプロセス内のメモリに保持します。そのため、最初のテストは**単一インスタンスの常駐Node.jsサーバー**で行ってください。

水平スケールやサーバーレス運用をする場合は、この一時ストアをRedis / KVなどに置き換えてください。現状のままVercel Functionsのような別インスタンスにWebhookが飛ぶ構成にすると、通話目的を復元できないことがあります。

## 日本向けの安全制約

- 日本国内番号（+81）のみ
- 110 / 118 / 119 は発信不可
- 0570、0990系はMVPでは発信不可
- 一括・自動大量発信を想定しない
- 相手にはAIアシスタントであることを明示
- 相手が終了を希望したら会話を終了
- AIが勝手に新規契約・購入などの金銭的義務を確定しない

実運用では、用途に応じて電気通信、録音、広告・勧誘、個人情報、業界固有の規制・ガイドラインを確認してください。

## 次にやること

- 単一インスタンスのHTTPS環境へデプロイ
- OpenAI Webhook URLを登録
- `mcp.json` のURLを更新
- 自分のテスト番号へ発信
- 必要ならRedis化、文字起こし、通話結果要約を追加

## Pythonテストベッド（`phone-agent/`）

`phone-agent/` には、このMCPプラグイン（TypeScript）とは独立した、音声プロバイダの接続確認用Pythonテストベッドが入っています（Grok Botと組み合わせて使っていたもの）。`skills/phone-agent/` はMCP用のスキル定義で別物です。

- **Twilio Trial**: `call_phone` / `call_phone.py` — 固定の日本語TTSを1回話して切る発信テスト。`--dry-run` は発信せず設定と番号だけ検証、`--trial` はTrialアカウント向けの最小パラメータで発信
- **Plivo**: `call_phone_plivo` / `serve_plivo_answer.py` — 公開 `PLIVO_ANSWER_URL` が返す `static/plivo_answer.xml` を使う発信テスト
- **Vonage + Gemini Live（AI会話）**: `vonage-agent/` — Vonage Voice APIのWebSocket音声をGemini Live（`gemini-3.8-live`）へ双方向に中継し、AIが日本語で会話するFastAPIアプリ。Cloudflare quick tunnelを自動起動し、URLが変わるたびにVonage ApplicationのwebhookをApplication APIで自動更新します。`./vonage serve --detach` → `./call_phone_vonage --dry-run +81... "目的"` の順で使います。Vonage無料トライアル + Google AI Studioの無料枠で試せます

セットアップと環境変数は `phone-agent/README.md` と `phone-agent/vonage-agent/README.md` を参照してください。資格情報は各ディレクトリの `.env.example` をコピーした `.env` に入れ、`.env` / `*.key` / `private.key` / `.venv` / `.runtime` はコミットしないでください（`.gitignore` で除外済み）。ライブ発信は課金され、相手に実際に電話がかかります。明示的に指示された場合のみ実行し、確認には `--dry-run` を使ってください。

## License

MIT
