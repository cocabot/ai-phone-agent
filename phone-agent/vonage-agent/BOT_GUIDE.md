# Bot operating guide (Grok Bot など)

このディレクトリを操作するボット向けの手順とルールです。コマンドはすべて `phone-agent/vonage-agent/` で実行します。

## 絶対ルール

1. **ライブ発信はオーナーが明示的に「発信して」と指示した時だけ**。番号と用件が曖昧なら発信前に確認する。番号を推測・捏造しない。
2. 発信前に必ず `./agent call 番号 "用件" --dry-run` を実行し、`ready=True` を確認する。
3. `.env`・`private.key`・APIキーの中身を表示・送信しない。`cat .env` もしない。
4. 110 / 119 などの緊急番号、大量発信、なりすまし、嫌がらせ目的には使わない（サーバー側でも拒否される）。

## 起動（常駐）

```bash
./agent status --json || (nohup ./agent start > data/server.log 2>&1 &)
sleep 8 && ./agent status
```

- `公開URL` が表示され、`Vonage同期: OK` なら着信・発信の準備完了。トンネルURLが変わっても自動で再同期される。
- `Vonage同期: NG` → `./agent doctor` の NG 項目をオーナーに報告する。
- `トンネル未接続` → ネットワーク制限。`.env` に `CLOUDFLARED_PROTOCOL=http2` を追加して再起動を提案する。

## 発信

```bash
./agent call +819012345678 "用件を1〜2文で" --notes "予約名・日時など補足" --dry-run
./agent call +819012345678 "用件を1〜2文で" --notes "予約名・日時など補足" --wait --json
```

- `goal` には「何を確認・依頼し、どうなれば成功か」を書く。例: 「9/30 19時に2名で予約可能か確認し、可能なら山田の名前で予約する。不可なら空いている時間を聞く」
- `--wait --json` の結果の `outcome`（success / partial / failed）と `summary` をオーナーに報告する。必要なら `transcript` も要約して伝える。
- 409（別の通話が進行中）なら `./agent status` で確認し、終わるまで待つ。

## 確認・中断

```bash
./agent calls              # 一覧
./agent show <ID>          # 詳細と文字起こし
./agent hangup <ID>        # オーナーに止めてと言われたら
```

## トラブルシュート

| 症状 | 対処 |
| --- | --- |
| `サーバーに接続できません` | 起動手順を実行 |
| `公開URLが未確定です` | 数秒待って再実行。続く場合は `data/server.log` を確認 |
| 相手に「ただいま応答できません」と流れた | Gemini接続失敗。`./agent test-gemini` でキー・モデル・無料枠上限を確認 |
| HTTP 401 (Vonage) | APIキー/シークレット、Application ID と秘密鍵の組み合わせを確認するようオーナーに依頼 |
| 発信が `failed` / `rejected` | トライアルでは未認証の番号に発信できない。Vonageダッシュボードで番号を登録するようオーナーに依頼 |
