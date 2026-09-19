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
- Twilio→OpenAI SIPに渡す通話目的をHMAC署名
- 緊急番号（110/118/119）と一部の特殊・高額課金系番号をブロック
- 通話時間はMVPでは最大15分

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

Twilioの日本番号取得・発信にはアカウント種別や規制要件が適用される場合があります。Twilio Consoleで利用する番号の要件を確認してください。

## セットアップ

```bash
npm install
cp .env.example .env
```

`.env` を設定します。

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

ランダムなシークレットは例えば以下で生成できます。

```bash
openssl rand -hex 32
```

## OpenAI Webhook

OpenAI Dashboardで、このサーバーの次のURLをWebhookに設定します。

```text
https://YOUR-DOMAIN/webhooks/openai
```

購読イベントに **`realtime.call.incoming`** を含め、発行された署名シークレットを `OPENAI_WEBHOOK_SECRET` に設定してください。

SIP送信先はコード内で次の形式を使います。

```text
sip:PROJECT_ID@sip.api.openai.com;transport=tls;edge=tokyo
```

## MCP

MCPエンドポイント:

```text
https://YOUR-DOMAIN/mcp
```

このMVPは副作用のある電話発信ツールなので、`MCP_BEARER_TOKEN` を必須にしています。公開配布する場合は固定Bearer tokenではなくOAuth認証へ置き換えてください。

`mcp.json` の `YOUR-DOMAIN` を実際のデプロイ先に変更します。

## ローカル起動

```bash
npm run dev
```

ヘルスチェック:

```bash
curl http://localhost:3000/health
```

## 日本向けの安全制約

このMVPは以下の前提です。

- 日本国内番号（+81）のみ
- 110 / 118 / 119 は発信不可
- 0570、0990系はMVPでは発信不可
- 一括・自動大量発信を想定しない
- 相手にはAIアシスタントであることを明示
- 相手が終了を希望したら会話を終了
- AIが勝手に新規契約・購入などの金銭的義務を確定しない

実運用では、利用目的に応じて電気通信、録音、広告・勧誘、個人情報、業界固有の規制・ガイドラインを確認してください。

## 次にやること

- Vercel / Cloud Run / Fly.io などHTTPS環境へデプロイ
- OpenAI Webhook URLを登録
- `mcp.json` のURLを更新
- テスト番号への発信
- 必要なら通話終了後の要約・文字起こし保存を追加

## License

MIT
