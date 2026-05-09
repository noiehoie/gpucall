# External System Adaptation Prompt

For new integrations, prefer the fuller onboarding package:

- [EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md](EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md)
- [EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md](EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md)

This legacy prompt remains useful when a single code agent needs one compact
instruction block.

Use this prompt when asking another code agent to migrate an existing system to gpucall v2.0. It is intentionally prescriptive: external systems should send task intent and data references, while gpucall owns recipe selection, tuple routing, governance, fallback, lease handling, and audit.

```text
あなたは既存システムの実装担当エージェントです。
このシステムの LLM / Vision / GPU 推論呼び出しを gpucall v2.0 Gateway 経由へ移行してください。
実コード・設定・テストを直接読み、必要な修正を最後まで実装し、テスト結果まで報告してください。

# 1. 前提

gpucall v2.0 Gateway は gateway.example.internal 上で稼働しています。

Gateway URL:

  https://gpucall-gateway.example.internal

この URL は Tailscale Tailnet 内からのみ到達可能です。
public internet には公開されていません。

環境変数:

  GPUCALL_BASE_URL=https://gpucall-gateway.example.internal
  GPUCALL_API_KEY=<実トークン>

注意:
- `GPUCALL_API_KEY` は gpucall 管理者が対象システム/tenant 向けに発行した gateway API key である。
- 外部システムは API key を自力取得しない。
- provider credential や gpucall 管理側 `credentials.yml` の provider API key を caller 用 key として使わないこと。
- `GPUCALL_API_KEY=dummy` は禁止。
- API key をコード、テスト fixture、README、ログ、レポートに平文出力しないこと。
- API key 未設定時は明確なエラー、または integration test skip にすること。

# 2. gpucall v2.0 の基本思想

外部システムは tuple を選ばない。
外部システムは通常 recipe も選ばない。

外部システムは以下だけを送る。

- task
- mode
- inline_inputs または input_refs
- max_tokens 等の実行希望

recipe / tuple / GPU / model / engine / fallback / circuit breaker / governance / lease / audit は gpucall Gateway 側が決定する。

通常 payload に以下を含めないこと。

- `requested_tuple`
- `recipe`

これらは public task endpoint では拒否される。recipe / tuple 選択は gateway の責務であり、外部システムは要求だけを送る。

# 3. API endpoint

v1 風の `/infer` は使わないこと。

gpucall v2.0 では mode ごとに endpoint が分かれています。

  sync   -> POST /v2/tasks/sync
  async  -> POST /v2/tasks/async
  stream -> POST /v2/tasks/stream

HTTP セマンティクスを混同しないこと。

- sync は 200 OK で result を返す
- async は 202 Accepted で job_id を返す
- stream は SSE を返す

# 4. 正しい最小 payload

sync infer の最小 payload:

  {
    "task": "infer",
    "mode": "sync",
    "inline_inputs": {
      "prompt": {
        "value": "hello",
        "content_type": "text/plain"
      }
    },
    "max_tokens": 64
  }

vision の最小 payload:

  {
    "task": "vision",
    "mode": "sync",
    "input_refs": [
      {
        "uri": "...",
        "sha256": "...",
        "bytes": 12345,
        "content_type": "image/png",
        "expires_at": "..."
      }
    ],
    "max_tokens": 64
  }

重要:
- `input_refs` は必ず list。
- OpenAI 風の `messages` をそのまま送らない。
- 必要なら wrapper 内で `messages` から `inline_inputs.prompt.value` へ変換する。
- `model: gpucall:*` のような値を Gateway routing に使わない。

# 5. 実装要件

## 5.1 Client wrapper

gpucall 専用 client wrapper を実装または修正してください。

必須:
- `GPUCALL_BASE_URL` を環境変数から読む
- `GPUCALL_API_KEY` を環境変数から読む
- 全 task endpoint に `Authorization: Bearer <token>` を付ける
- API key 未設定時に明確なエラーを出す
- 401 では「GPUCALL_API_KEY を確認」とわかるエラーにする
- `recipe` / `requested_tuple` は payload に含めない

期待実装例:

  payload = {
      "task": task,
      "mode": mode,
      "inline_inputs": {
          "prompt": {
              "value": prompt,
              "content_type": "text/plain",
          }
      },
      "max_tokens": max_tokens,
  }

## 5.2 Sync

`POST /v2/tasks/sync` を使う。

Response 例:

  {
    "plan_id": "...",
    "result": {
      "kind": "inline",
      "value": "...",
      "ref": null,
      "usage": {}
    }
  }

`result.value` を呼び出し元へ返す。

## 5.3 Async

`POST /v2/tasks/async` を使う。

202 Accepted で `job_id` を受け取る。
その後:

  GET /v2/jobs/{job_id}

を polling する。

完了 state:

- COMPLETED
- FAILED
- CANCELLED
- EXPIRED

async 完了時に `result` が無い、または `result_ref: null` のケースを graceful に扱うこと。
local stub では空文字列になる場合がある。

## 5.4 Stream

`POST /v2/tasks/stream` を使う。

SSE の heartbeat:

  : heartbeat

は正常な接続維持信号。
エラー扱いしないこと。

## 5.5 DataRef / 大容量入力

大きな prompt、画像、ファイルを Gateway 本文に直接送らないこと。

閾値を超える入力は以下の流れにする。

1. SHA256 を計算
2. `POST /v2/objects/presign-put`
3. 返された `upload_url` に PUT
4. 返された `data_ref` を `input_refs: [data_ref]` として task API に送る

`input_refs` は list 形式。

presigned URL はログに出さない。

# 6. Error handling

以下の扱いを実装してください。

- 401: API key 未設定/不一致。`dummy` は不可と明示。
- 400: payload/schema/governance error。
- 404: 古い endpoint `/infer` を使っている可能性。
- 429: rate limit。backoff 可能にする。
- 5xx: Gateway/provider 障害。request/job id を残す。

ログに prompt 本文や secret を出さないこと。

# 7. Secret / logging policy

以下をログ、テスト出力、例外、完了レポートに絶対に出さない。

- GPUCALL_API_KEY
- Authorization header
- prompt 本文
- image/file 本文
- presigned upload URL
- presigned download URL
- signed DataRef URL
- provider API key

表示が必要なら必ず redacted にする。

例:

  GPUCALL_API_KEY=<set>
  upload_url=<redacted>

# 8. CI / dependency management

このリポジトリが uv 管理の場合、CI でも uv を使うこと。
グローバル pip / system Python への install は避ける。

GitHub Actions 例:

  - uses: astral-sh/setup-uv@v5

  - run: uv sync --extra dev

  - run: uv run pytest

`pip install -e ".[dev]"` は使わない。

# 9. 必須テスト

以下のテストを追加または更新してください。

## 9.1 Payload construction

- payload に `recipe` が含まれない
- payload に `requested_tuple` が含まれない
- payload に GPU や provider 指定が含まれない
- `task` と `mode` は含まれる
- `inline_inputs.prompt.value` 形式になっている
- `input_refs` は list 形式

## 9.2 Auth

- `GPUCALL_API_KEY` 未設定時に明確なエラー
- `GPUCALL_API_KEY=dummy` では 401 をわかりやすく扱う
- 実トークンはログに出ない

## 9.3 Endpoint

- `/infer` を使っていない
- sync は `/v2/tasks/sync`
- async は `/v2/tasks/async`
- stream は `/v2/tasks/stream`

## 9.4 DataRef

- 閾値超過入力で presign-put が呼ばれる
- PUT upload 後、`input_refs: [data_ref]` で task API に送る
- presigned URL がログに出ない

## 9.5 Integration skip

CI やローカルで `GPUCALL_API_KEY` が無い場合、実 gateway integration test は skip すること。

# 10. 実行すべき smoke test

環境変数:

  export GPUCALL_BASE_URL=https://gpucall-gateway.example.internal
  export GPUCALL_API_KEY=<実トークン>

A. healthz:

  curl -sS "$GPUCALL_BASE_URL/healthz"

期待:

  {"status":"ok"}

B. readyz:

  curl -sS "$GPUCALL_BASE_URL/readyz"

期待:

  {"status":"ready"}

詳細な readiness は gateway 管理者向けの `/readyz/details` で確認する。外部システム側の smoke は `/readyz` の最小応答だけを期待する。

C. recipe 省略 sync infer:

  curl -sS -X POST "$GPUCALL_BASE_URL/v2/tasks/sync" \
    -H "Authorization: Bearer $GPUCALL_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{
      "task": "infer",
      "mode": "sync",
      "inline_inputs": {
        "prompt": {
          "value": "Say ok only.",
          "content_type": "text/plain"
        }
      },
      "max_tokens": 8
    }'

期待:
- HTTP 200
- `plan_id` がある
- `result.kind` がある
- `result.value` が取得できる

D. DataRef smoke:
- 30KB 超のテキストまたは画像を用意
- `/v2/objects/presign-put`
- PUT upload
- `/v2/tasks/sync` に `input_refs: [data_ref]`
- 成功確認

# 11. 完了条件

以下をすべて満たしたら完了。

1. healthz / readyz が通る
2. dummy API key を使っていない
3. `/infer` を使っていない
4. `/v2/tasks/sync|async|stream` を使っている
5. payload に `recipe` が含まれない
6. payload に `requested_tuple` が含まれない
7. payload に GPU や provider 指定が含まれない
8. sync infer が通る
9. async infer が graceful に動く
10. DataRef smoke が通る
11. secret / prompt / presigned URL がログに出ていない
12. CI が uv 管理になっている
13. 関連 unit tests が通る
14. 実 gateway smoke が通る

# 12. 報告形式

最後に以下を報告してください。

1. 変更ファイル
2. 主な修正内容
3. テスト結果
4. smoke test 結果
5. secret redaction 確認
6. 残存 Finding があれば severity 付きで列挙
7. Go / No-Go 判定

注意:
報告にも API key の実値を絶対に出さないでください。
`GPUCALL_API_KEY=<set>` と書いてください。
```

## Common Failure Modes

- Using `/infer` instead of `/v2/tasks/sync`.
- Sending `GPUCALL_API_KEY=dummy`.
- Sending `input_refs` as an object instead of a list.
- Treating async completion with no inline result as a hard crash.
- Always sending `recipe: text-infer-standard`, which pins external systems to gateway internals.
- Sending `requested_tuple` from application code.
- Printing the gateway API key in completion reports.
- Using `pip install -e ".[dev]"` in repositories whose policy requires `uv`.
