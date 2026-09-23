# vonage-agent

Vonage Voice API と Gemini Live をつないで、AIが日本語で電話をかけて会話するエージェントです。
Vonage 無料トライアル + Google AI Studio の API キー（`gemini-3.8-live`）で試せる構成にしてあります。

```text
./vonage call +81... "目的"
        |
        | (ローカル管理API)
        v
vonage-agent (FastAPI) ──── Vonage Voice API: POST /v1/calls (NCCO付き)
        ^                              |
        | cloudflared quick tunnel      | 電話が鳴る → 応答
        | https://xxxx.trycloudflare.com|
        |                              v
   /socket  <── audio/l16 16kHz ── Vonage WebSocket 接続
        |
        | 音声を双方向に中継（24k→16k リサンプル、割り込み時は clear）
        v
Gemini Live (gemini-3.8-live) ── end_call ツール → 通話終了 + 結果要約
```

Cloudflare の quick tunnel は起動のたびに URL が変わりますが、サーバーが URL を検知して
**Vonage Application の answer/event/fallback webhook を Application API で自動更新**します。
cloudflared が落ちて再起動し URL が変わった場合も自動で追従します。ダッシュボードでの手動設定は不要です。

## 前提

| 必要なもの | メモ |
| --- | --- |
| Vonage API アカウント | 無料トライアルで可。**トライアル中は発信先がサインアップ時に登録した自分の番号のみ**、発信者番号は Vonage のテスト用 `123456789` を使う（番号の購入は不可） |
| Vonage Application | Dashboard > Applications で作成し **Voice** を有効化。`Application ID` と **秘密鍵 (private.key)** を控える |
| Vonage API key / secret | Dashboard > API settings。webhook 自動更新に使用 |
| Google AI Studio API キー | [aistudio.google.com](https://aistudio.google.com) で発行。`gemini-3.8-live` が使えるか、無料枠の上限は AI Studio の Rate limits ページで確認 |
| cloudflared | quick tunnel 用。`brew install cloudflared` など（`setup.sh` が案内を出します）。固定の公開URLがあるなら不要 |
| Python 3.11+ | `python3 -m venv` が使えること |

## セットアップ

```bash
cd phone-agent/vonage-agent
./setup.sh                 # .venv 作成 + 依存インストール + .env 雛形
# 秘密鍵を private.key として置く（チャットに本文を貼らない）
$EDITOR .env               # VONAGE_*, GEMINI_API_KEY を埋める
./vonage doctor            # 設定・鍵・Vonage API・Gemini・cloudflared を一括チェック
```

`.env` の最小構成:

```env
VONAGE_APPLICATION_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
VONAGE_PRIVATE_KEY_PATH=./private.key
VONAGE_API_KEY=xxxxxxxx
VONAGE_API_SECRET=xxxxxxxxxxxxxxxx
VONAGE_FROM_NUMBER=123456789          # トライアル用テスト発信者番号
GEMINI_API_KEY=AIza...
GEMINI_MODEL=gemini-3.8-live
ALLOWED_TO_NUMBERS=+8190xxxxxxxx       # テスト中は自分の番号だけに限定（推奨）
```

## 起動

```bash
./vonage serve --detach    # バックグラウンド起動。tunnel URL 取得 → webhook 同期まで待って状態を表示
./vonage status            # いつでも状態確認
./vonage logs -f           # ログ追跡
./vonage stop
```

`serve` は次を自動で行います。

1. FastAPI サーバー起動（既定 `:8090`）
2. `cloudflared tunnel --url http://127.0.0.1:8090` を子プロセスとして起動し、ログから `https://xxxx.trycloudflare.com` を取得
3. Vonage Application の `answer_url` / `event_url` / `fallback_answer_url` をその URL に更新（既に一致していれば何もしない）
4. cloudflared が終了したらバックオフ付きで再起動し、新しい URL で 2〜3 をやり直す

固定URL（named tunnel / ngrok / 自前ドメイン）がある場合は `PUBLIC_URL=https://...` を設定すると tunnel は起動せず、その URL で同期だけ行います。
手動で同期したいときは `./vonage sync-webhooks [--url https://...]`。

## 発信

```bash
# まず dry-run（発信しない。設定チェックと NCCO のプレビュー）
./vonage call +8190xxxxxxxx "明日19時に2名で予約できるか確認" --dry-run

# 発信（相手に実際に電話がかかり、料金が発生します）
./vonage call +8190xxxxxxxx "明日19時に2名で予約できるか確認" -i "予約名は山田、電話番号は090-xxxx-xxxx"

# 終了まで待って結果と文字起こしを表示
./vonage call +8190xxxxxxxx "配送予定日を確認" --wait

./vonage status <uuid>       # 状態・結果要約・文字起こし
./vonage hangup <uuid>       # 強制終了
```

通話の流れ: Vonage TTS が「AIアシスタントからのお電話です。」と告知 → WebSocket 経由で Gemini が会話 →
用件が済むと Gemini が `end_call(summary, outcome)` を呼び、挨拶の再生完了を待ってから Voice API で切断します。
`summary` / `outcome`（success / partial / failed / declined / voicemail / wrong_number）は `status` で確認できます。
自動音声ガイダンスに当たった場合は `press_keys` ツールで DTMF を送ります。

`phone-agent/` 直下からも同じことができます: `./call_phone_vonage --dry-run +81... "目的"` / `./phone --provider vonage +81... "目的"`。

## Grok bot などエージェントから使う場合

- 起動確認: `./vonage status`（`webhooks: ok` と `public url` が出ていれば発信可能）
- 発信前に必ず `--dry-run`。ユーザーが明示的に「発信して」と言ったときだけ `--dry-run` なしで実行
- 結果は `./vonage call ... --wait` か `./vonage status <uuid>` で取得（`--json` で機械可読）
- `MAX_CONCURRENT_CALLS=1`（既定）なので同時に複数は発信できません。`hangup` してから次へ
- 管理APIを直接叩くことも可能: トークンは `.runtime/state.json` の `admin_token`

```bash
TOKEN=$(python3 -c "import json;print(json.load(open('.runtime/state.json'))['admin_token'])")
curl -s -X POST http://127.0.0.1:8090/calls -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"to":"+8190xxxxxxxx","objective":"明日19時に2名で予約できるか確認","dry_run":true}'
```

## 着信

Vonage 番号（有料アカウント）に着信すると `answer_url` から同じ WebSocket 接続の NCCO を返し、
`INBOUND_OBJECTIVE` を目的として Gemini が応対します。トライアルの `123456789` は着信には使えません。

## 主な設定（`.env.example` に全項目）

| 変数 | 既定 | 説明 |
| --- | --- | --- |
| `GEMINI_MODEL` | `gemini-3.8-live` | Live API 対応モデル。使えない場合は `gemini-3.1-flash-live-preview` や `gemini-2.5-flash-native-audio-preview-12-2025` を試す |
| `GEMINI_VOICE` | `Kore` | Gemini TTS の声 |
| `AGENT_NAME` / `AGENT_EXTRA_INSTRUCTIONS` | | 名乗り・追加のシステム指示（会社名、営業時間など） |
| `ALLOWED_TO_NUMBERS` | 空 | 設定すると、その番号以外への発信をローカルで拒否 |
| `MAX_CONCURRENT_CALLS` | `1` | 同時通話数の上限 |
| `CALL_MAX_SECONDS` | `900` | 通話の上限秒数（Vonage `length_timer` と NCCO `limit`） |
| `ANNOUNCE_AI` / `ANNOUNCE_TEXT` | `1` / AI告知文 | 会話前に Vonage TTS で読み上げる告知 |
| `VONAGE_AUDIO_RATE` | `16000` | WebSocket の音声レート（8000/16000/24000） |
| `TUNNEL` | `auto` | `auto`: `PUBLIC_URL` 未設定かつ cloudflared があれば quick tunnel / `quick` / `none` |
| `PUBLIC_URL` | 空 | 固定の公開URL |
| `SYNC_WEBHOOKS` | `1` | `0` で Vonage Application を一切変更しない |
| `CLOUDFLARED_ARGS` | 空 | 例: `--protocol http2`（UDP/QUIC が塞がれている環境） |
| `TRANSCRIPT_LOG` | `0` | 文字起こしをログにも出す |

## セキュリティ

- tunnel の URL は公開されます。管理API (`/calls`, `/admin/*`) は `ADMIN_TOKEN`（未設定なら起動ごとに自動生成、`.runtime/state.json` に 0600 で保存）が必須
- Vonage からの WebSocket 接続は NCCO に埋め込んだ `Authorization` ヘッダー（`WS_AUTH_TOKEN`）で検証
- `VONAGE_SIGNATURE_SECRET` を設定すると webhook の Vonage 署名（JWT）も検証
- 110/118/119 と 0570/0990 系はブロック。`ALLOWED_TO_NUMBERS` で許可リスト運用を推奨
- 文字起こし・電話番号はプロセスメモリ内のみ（ログでは番号を下4桁にマスク）。`.env` / `private.key` / `.runtime/` はコミットしない

## トラブルシューティング

| 症状 | 確認すること |
| --- | --- |
| `webhooks: skipped` | `VONAGE_API_KEY` / `VONAGE_API_SECRET` / `VONAGE_APPLICATION_ID` が未設定。手動なら `./vonage sync-webhooks --url ...` |
| `webhooks: error` | ログの HTTP ステータス。401 は API key/secret、404 は Application ID を確認 |
| `public url: (none yet)` | cloudflared が起動できていない。`./vonage logs` と `.runtime/cloudflared.log`。QUIC が塞がれていれば `CLOUDFLARED_ARGS=--protocol http2` |
| 発信 API が 4xx | トライアルでは登録済み番号以外へ発信不可。`from` は `123456789`。JWT エラーなら秘密鍵と Application ID の組み合わせ |
| 電話は鳴るが無音 | `./vonage status <uuid>` の `error`。Gemini のモデル名・キー・無料枠上限（429）を確認。`ws_connected` が false なら Vonage から WebSocket が届いていない（URL/認証） |
| すぐ切れる | `event` の `status`（`rejected`/`busy`/`unanswered`）。`--refresh` で Vonage 側の記録も取得 |
| 音が途切れる | `VONAGE_AUDIO_RATE=8000` を試す。Vonage は 20ms 単位のフレームを要求するため、サーバー負荷が高い環境では遅延が出る |

## 制限・今後

- 通話状態・文字起こしはプロセス内メモリのみ（再起動で消える）。永続化するなら SQLite/Redis へ
- Gemini Live の音声セッションは既定 15 分。`context_window_compression` を有効にしているが、`CALL_MAX_SECONDS` を上限として運用する
- 通話録音・留守電検知（Vonage AMD）・複数インスタンス構成は未対応
- quick tunnel は Cloudflare のベストエフォート。常用するなら named tunnel などの固定 URL を `PUBLIC_URL` に
