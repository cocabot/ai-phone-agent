# vonage-agent — Vonage Voice × Gemini Live 電話AIエージェント

Vonage の電話回線と Google AI Studio の **Gemini Live**（既定 `gemini-3.8-live`）をリアルタイムに接続し、
AIが日本語で電話をかける／受けるためのサーバーとCLIです。Vonage 無料トライアル + AI Studio の APIキーだけで試せます。

```text
相手の電話 ─PSTN─ Vonage ─(WebSocket, L16 PCM)─ cloudflared ─ vonage-agent ─(Live API)─ Gemini 3.8 Live
                     │                                              │
                     └── Answer / Event Webhook ────────────────────┘
                         ↑ トンネルURLが変わるたびに Vonage Application API で自動更新
```

## 主な機能

- **トンネルURLの自動反映**: `cloudflared` quick tunnel を自動起動し、URLが変わる（再起動・切断）たびに
  Vonage Application の Answer URL / Event URL を API で書き換え。手動設定は不要
- **Gemini Live 音声ブリッジ**: 双方向ストリーミング、割り込み（barge-in）対応、文字起こし保存
- **AIが自分で通話を終了**: 目的達成・相手の終了希望で `end_call`（話し終わるのを待ってから切断）、結果要約つき
- **IVR対応**: 自動音声案内でプッシュボタン（DTMF）を押せる。相手のDTMFもAIに伝達
- **発信・着信の両対応**: `./agent call` で発信、Vonage番号への着信にも応答
- **安全装置**: 110/119等・0570/0990/0180への発信拒否、国番号制限、`CALL_ALLOWLIST`、同時通話数・最大通話時間の上限、
  Webhook URLに秘密トークン、WebSocketは通話ごとのトークン必須、ローカルAPIはトンネル経由のアクセスを拒否

## クイックスタート

### 1. セットアップ

```bash
cd phone-agent/vonage-agent
./setup.sh          # venv作成・依存インストール・.env作成・cloudflared取得（未インストール時）
```

### 2. Vonage（無料トライアル）

1. [Vonage API Dashboard](https://dashboard.nexmo.com/) でアカウント作成（トライアルクレジット付与）
2. ダッシュボードの **API key / API secret** を `.env` の `VONAGE_API_KEY` / `VONAGE_API_SECRET` に設定
3. **Applications → Create a new application**
   - 「Generate public and private key」で秘密鍵をダウンロード → `vonage-agent/private.key` に保存
   - **Voice** を有効化。Answer URL / Event URL は仮の値（`https://example.com/answer` 等）でOK — 起動時に自動で書き換わります
   - 保存後の **Application ID** を `VONAGE_APPLICATION_ID` に設定
4. **Numbers** で番号を取得し `VONAGE_FROM_NUMBER` に設定（着信も試すなら `./agent link-number` でアプリに紐付け）

> トライアル中は発信先が登録済み（認証済み）の番号に限られるなど制限があります。最初は自分の携帯番号でテストし、
> `CALL_ALLOWLIST` にその番号を入れておくと誤発信を防げます。

### 3. Gemini（Google AI Studio）

[Google AI Studio](https://aistudio.google.com/apikey) で APIキーを作成し `GEMINI_API_KEY` に設定。
モデルは `GEMINI_MODEL`（既定 `gemini-3.8-live`）で変更できます。

### 4. 確認 → 起動 → 発信

```bash
./agent doctor                 # 設定・Vonage API・Geminiモデルをまとめてチェック
./agent test-gemini            # 電話なしで Gemini Live の音声生成を確認（data/gemini_test.wav に保存）
./agent start                  # サーバー + cloudflared 起動、URLをVonageへ自動設定（別ターミナル / バックグラウンドで）
./agent status                 # 公開URL・同期状態・進行中の通話
./agent call +819012345678 "明日19時に2名で予約できるか確認する" --dry-run
./agent call +819012345678 "明日19時に2名で予約できるか確認する" --notes "予約名: 山田" --wait
```

`--wait` を付けると通話終了まで待ち、結果（success / partial / failed）、要約、文字起こしを表示します。

## コマンド一覧

| コマンド | 内容 |
| --- | --- |
| `./agent start` | サーバー起動（トンネル管理・Vonage自動同期込み） |
| `./agent doctor` | 設定と接続のチェック（NGがあれば終了コード1） |
| `./agent test-gemini [テキスト]` | Gemini Live 単体テスト |
| `./agent status` | 公開URL / トンネル接続 / 同期結果 / 進行中の通話 |
| `./agent sync [--url URL]` | Webhook URLを今すぐ再設定（サーバー停止中でも `--url` 指定で可） |
| `./agent link-number [番号]` | Vonage番号をアプリに紐付け（着信用） |
| `./agent call 番号 "用件" [--notes ..] [--dry-run] [--wait]` | AIが発信 |
| `./agent calls` / `./agent show ID [--wait]` | 通話一覧 / 詳細・文字起こし |
| `./agent hangup ID` | 通話を切る |

すべてのコマンドに `--json` を付けると機械可読な出力になります（ボット向け）。
通話記録は `data/calls/*.json` に保存されます。

## 公開URL（トンネル）のモード

`TUNNEL` で切り替えます。

| 値 | 動作 |
| --- | --- |
| `auto`（既定） | `PUBLIC_URL` があれば固定URL、`CLOUDFLARED_METRICS` があれば監視、`cloudflared` があれば自動起動 |
| `cloudflared` | quick tunnel を子プロセスで起動。落ちたら再起動し、新URLを自動でVonageへ反映 |
| `metrics` | 別に起動済みの `cloudflared --metrics 127.0.0.1:20241` の `/quicktunnel` を10秒ごとに監視し、変化したら反映 |
| `none` | `PUBLIC_URL` をそのまま使う（named tunnel / ngrok / 自前ドメインなど） |

- 自動同期は Vonage Application API（`GET` → Voice webhookだけ差し替え → `PUT`）で行い、アプリ名・公開鍵・他のcapabilityは保持します。
  URLが既に一致していれば何もしません。無効化は `VONAGE_AUTO_SYNC=0`。
- Webhook URL には推測されにくいトークン（`data/state.json` に保存、`WEBHOOK_TOKEN` で固定可）が入ります。
- `status` に「トンネル未接続: Allow outbound QUIC traffic on port 7844 …」と出る場合は、ネットワークが
  Cloudflare への 7844 番ポートを塞いでいます。`CLOUDFLARED_PROTOCOL=http2` を試すか、許可してください。
- `~/.cloudflared/config.yml` があると quick tunnel が起動しないことがあります。

## 主な環境変数

`.env.example` に全項目とコメントがあります。よく触るもの:

| 変数 | 既定 | 説明 |
| --- | --- | --- |
| `GEMINI_MODEL` | `gemini-3.8-live` | Live API のモデルID |
| `GEMINI_VOICE` | `Kore` | 音声（Puck / Charon / Aoede など） |
| `AGENT_NAME` | `AIアシスタント` | AIが名乗る名前 |
| `INBOUND_GOAL` | 受付係 | 着信時の役割 |
| `EXTRA_INSTRUCTIONS` | - | 全通話に追加する指示 |
| `CALL_ALLOWLIST` | - | 発信を許可する番号のリスト（カンマ区切り） |
| `ALLOWED_COUNTRY_CODES` | `81` | 発信を許可する国番号 |
| `MAX_CALL_SECONDS` | `600` | 最大通話時間（30秒前にAIへまとめを指示） |
| `AUDIO_RATE` | `16000` | Vonage⇔サーバーのPCMサンプルレート（8000/16000/24000） |
| `BRIDGE` | - | `echo` にするとGeminiを使わず音声をそのまま返す回線テスト |

## HTTPエンドポイント

公開（トンネル経由）: `GET /health`, `GET /webhooks/{token}/answer`, `POST /webhooks/{token}/event`, `WS /socket/{call_id}/{token}`

ローカル専用（`127.0.0.1` から、またはトンネル経由でない場合のみ。`AGENT_API_TOKEN` 設定時は Bearer でも可）:
`GET /api/status`, `POST /api/sync`, `POST /api/link-number`, `POST /api/calls`, `GET /api/calls`, `GET /api/calls/{id}?wait=秒`, `POST /api/calls/{id}/hangup`

## 開発

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

テストは Vonage / Gemini / WebSocket をすべてフェイクで置き換えるので、資格情報なしで動きます。

## 注意

- ライブ発信は課金され、相手に実際に電話がかかります。明示的に指示された場合のみ実行し、確認には `--dry-run` を使ってください。
- AIは冒頭でAIであることを名乗り、相手が終了を望めば切るように指示されています。用途に応じて関係法令・ガイドラインを確認してください。
- `.env` / `private.key` / `data/` はコミットしないでください（`.gitignore` 済み）。
