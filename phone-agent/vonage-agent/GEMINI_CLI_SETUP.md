# Gemini CLI で電話AIエージェントを使う（無料構成）

Gemini CLI と対話しながら「〇〇に電話して△△を確認して」と頼むと、AIが電話をかけて結果を報告してくれる環境の作り方です。
追加の月額費用はかかりません。

```text
あなた ⇄ Gemini CLI（ターミナル）
            │ ./agent call …（発信前にあなたが承認）
            ▼
     vonage-agent サーバー ─ cloudflared ─ Vonage ─ 相手の電話
            │
            └─ Gemini Live（電話の会話を担当）
```

| 使うもの | 費用 |
| --- | --- |
| Gemini CLI（個人のGoogleアカウントでログイン） | 無料（1日1,000リクエストまで） |
| Google AI Studio の APIキー（電話の音声AI用） | 無料枠あり |
| Vonage | 無料トライアルのクレジット |
| cloudflared quick tunnel | 無料・アカウント不要 |
| 動かすPC | 手元のMac / Linux / Windows(WSL) |

> PCとサーバーを起動している間だけ使えます。

---

## 1. 必要なもの

- **Node.js 20 以上**（Gemini CLI 用）: `node -v` で確認。無ければ <https://nodejs.org/> から LTS 版を入れる
- **Python 3.11 以上**（電話サーバー用）: `python3 --version` で確認
- **git**
- **Googleアカウント**、**Vonageアカウント**
- Windows の場合は **WSL（Ubuntu）** を入れて、以下をすべて WSL のターミナルで実行してください

## 2. リポジトリの取得と電話サーバーのセットアップ

```bash
git clone https://github.com/cocabot/ai-phone-agent.git
cd ai-phone-agent/phone-agent/vonage-agent
./setup.sh
```

`setup.sh` が Python の仮想環境・依存パッケージ・`.env`・cloudflared をまとめて用意します。

## 3. APIキー類を `.env` に設定

`.env` をエディタで開いて埋めます（詳細は [README.md](README.md) の「クイックスタート」）。

1. **Vonage**（<https://dashboard.nexmo.com/>）
   - ダッシュボードの API key / API secret → `VONAGE_API_KEY` / `VONAGE_API_SECRET`
   - Applications → Create a new application
     - 「Generate public and private key」で落ちてくる秘密鍵を `vonage-agent/private.key` に保存
     - Voice を有効化（Answer URL / Event URL は `https://example.com/answer` などの仮の値でOK。起動時に自動で書き換わります）
     - 保存後の Application ID → `VONAGE_APPLICATION_ID`
   - Numbers で番号を取得 → `VONAGE_FROM_NUMBER`
2. **Gemini（電話の音声AI）**: <https://aistudio.google.com/apikey> でキーを作成 → `GEMINI_API_KEY`
3. **誤発信防止（強く推奨）**: 自分の携帯番号を `CALL_ALLOWLIST=+8190xxxxxxxx` に設定。
   Vonage トライアルでは、ダッシュボードで登録した番号にしか発信できません。

確認:

```bash
./agent doctor        # すべて [OK] になればOK（ローカルサーバーはまだ停止中でよい）
./agent test-gemini   # 音声AIの応答テスト（data/gemini_test.wav に保存）
```

## 4. Gemini CLI のインストールとログイン

```bash
npm install -g @google/gemini-cli
gemini --version
```

初回起動でログイン方法を聞かれたら **「Login with Google」** を選び、ブラウザで個人のGoogleアカウントにログインします。
（APIキーでのログインより、Googleログインの方が無料枠が大きいです）

## 5. 使う

ターミナルを2つ開きます。

**ターミナル1：電話サーバー**（起動したままにする）

```bash
cd ai-phone-agent/phone-agent/vonage-agent
./agent start
```

ログに `public url -> https://xxxx.trycloudflare.com` と `vonage webhooks synced` が出れば準備完了です。

**ターミナル2：Gemini CLI**

```bash
cd ai-phone-agent/phone-agent/vonage-agent
gemini
```

このディレクトリの `GEMINI.md`（操作手順と安全ルール）が自動で読み込まれます。`/memory show` で確認できます。

### 話しかけ方の例

```text
> 状態を確認して
> 090-xxxx-xxxx に電話して、明日19時に2名で予約できるか聞いて。予約名は山田。
> さっきの通話の文字起こしを見せて
> 最近の通話履歴を一覧にして
> 今の通話を切って
```

発信の流れ:

1. Gemini CLI が内容を整理し、`./agent call … --dry-run` で事前確認
2. 「この内容で発信します」とあなたに確認
3. `./agent call … --wait --json` の実行前に、Gemini CLI の **確認ダイアログ** が出る → 内容を見て **Allow once（今回だけ許可）** を選ぶ
4. 通話終了まで待ち、結果（成功/一部/失敗）と要約を報告

## 6. 安全のための設定（最初から入っています）

- `.gemini/settings.json` で、読み取りだけのコマンド（`./agent status` / `doctor` / `calls` / `show`）は確認なしで実行できるようにしてあります。
  **発信（`./agent call`）と切断は毎回確認ダイアログが出ます。**
- 発信の確認ダイアログで **「Always allow」は選ばないでください**。以後、確認なしで発信できるようになってしまいます。
- `gemini --yolo`（全コマンド自動承認）では使わないでください。
- サーバー側でも、緊急番号・特殊番号への発信拒否、`CALL_ALLOWLIST`、同時通話1本、最大10分の制限がかかります。

## 7. よくあるトラブル

| 症状 | 対処 |
| --- | --- |
| Gemini CLI が `GEMINI.md` を読んでいない | `phone-agent/vonage-agent` で起動したか確認。`/memory refresh` で再読み込み |
| 毎回コマンドの確認を聞かれる | 「フォルダを信頼するか」を聞かれたら Trust を選ぶ（プロジェクト設定の読み込みに必要な場合があります） |
| `サーバーに接続できません` | ターミナル1で `./agent start` が動いているか確認 |
| `トンネル未接続: … port 7844 …` | 会社・学校などのネットワーク制限。`.env` に `CLOUDFLARED_PROTOCOL=http2` を追加して再起動。だめなら別のネットワークで |
| `Vonage同期: NG` | `./agent doctor` の NG 項目を確認（APIキー/シークレット/Application ID/秘密鍵） |
| 発信が `rejected` / `failed` | トライアル中は登録済みの番号にしか発信できない。Vonage ダッシュボードで番号を登録 |
| 相手に「ただいま応答できません」と流れる | 音声AIの接続失敗。`./agent test-gemini` でキー・モデル・無料枠の上限を確認。`GEMINI_MODEL` を変えて試す |
| Gemini CLI の上限に達した | 無料枠は1日単位でリセットされます |

## 8. 終了するとき

- Gemini CLI: `/quit`（または Ctrl+C を2回）
- 電話サーバー: ターミナル1で Ctrl+C（cloudflared も一緒に止まります）

次に起動するとトンネルURLは変わりますが、Vonage の設定は自動で更新されるので何もしなくて大丈夫です。
