# Sugano Fleet gpucall Onboarding Prompt

This is the environment-specific prompt for Sugano's private fleet. It is not
the generic product prompt. Use it only for systems that are allowed to reach
the trusted gpucall gateway on the Tailscale network.

Replace `<system-name>` with the stable system name, then paste the prompt below
into the external system's coding agent.

```text
あなたはこのリポジトリを初めて触る coding agent です。目的は、このシステムの LLM / Vision / GPU 呼び出しを gpucall v2 に正しく受容させることです。

重要: gpucall ルーター本体の repository を clone してはいけません。必要な仕様は公開 docs URL から読むだけです。gpucall 側の router / gateway 実装をこのリポジトリに持ち込まないこと。

この環境の gpucall gateway は既に netcup 上で稼働しています。

- Gateway URL: http://gpucall.example.internal:18088
- System name: <system-name>
- Onboarding prompt: https://raw.githubusercontent.com/noiehoie/gpucall3/main/docs/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md
- Onboarding manual: https://raw.githubusercontent.com/noiehoie/gpucall3/main/docs/EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md
- Recipe draft helper wheel: https://raw.githubusercontent.com/noiehoie/gpucall3/main/sdk/python/dist/gpucall_sdk-2.0.0a2-py3-none-any.whl

最初に必ず bootstrap で gateway API key と recipe inbox を取得してください。API key を画面、ログ、完了報告に表示してはいけません。表示してよいのは prefix だけです。

```bash
set -eu

export GPUCALL_BASE_URL="http://gpucall.example.internal:18088"
export GPUCALL_SYSTEM_NAME="<system-name>"

bootstrap_json="$(
  curl -fsS -X POST "$GPUCALL_BASE_URL/v2/bootstrap/tenant-key" \
    -H 'content-type: application/json' \
    -d "{\"system_name\":\"$GPUCALL_SYSTEM_NAME\"}"
)"

python3 - <<'PY' "$bootstrap_json"
import json, sys
data = json.loads(sys.argv[1])
handoff = data.get("handoff") or {}
required = ["GPUCALL_BASE_URL", "GPUCALL_API_KEY", "GPUCALL_RECIPE_INBOX"]
missing = [k for k in required if not handoff.get(k)]
if missing:
    raise SystemExit(f"bootstrap response missing: {missing}")
print("bootstrap_ok")
print("tenant=" + data.get("tenant", ""))
print("api_key_prefix=" + handoff["GPUCALL_API_KEY"][:8])
print("recipe_inbox=" + handoff["GPUCALL_RECIPE_INBOX"])
PY

mkdir -p .gpucall
python3 - <<'PY' "$bootstrap_json" > .gpucall/env
import json, sys
data = json.loads(sys.argv[1])
handoff = data["handoff"]
for key in ["GPUCALL_BASE_URL", "GPUCALL_API_KEY", "GPUCALL_RECIPE_INBOX", "GPUCALL_ONBOARDING_PROMPT_URL", "GPUCALL_ONBOARDING_MANUAL_URL"]:
    print(f'export {key}="{handoff.get(key, "")}"')
print('export GPUCALL_SYSTEM_NAME="<system-name>"')
PY

chmod 600 .gpucall/env
```

`.gpucall/env` は secret handoff です。git に入れてはいけません。git repository なら `.gitignore` に `.gpucall/` を追加してください。

次に仕様書を取得して読んでください。

```bash
curl -fsS https://raw.githubusercontent.com/noiehoie/gpucall3/main/docs/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md -o /tmp/gpucall-onboarding-prompt.md
curl -fsS https://raw.githubusercontent.com/noiehoie/gpucall3/main/docs/EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md -o /tmp/gpucall-onboarding-manual.md
sed -n '1,430p' /tmp/gpucall-onboarding-prompt.md
sed -n '1,430p' /tmp/gpucall-onboarding-manual.md
```

作業ルール:

1. まず `rg` で LLM / Vision / GPU 呼び出し箇所を棚卸しする。
   - OpenAI SDK
   - Anthropic SDK
   - Gemini / Google Generative AI
   - Ollama
   - vLLM
   - RunPod / Modal / GPU job client
   - 独自 HTTP LLM endpoint
   - model/provider/GPU を caller 側で指定している箇所

2. gpucall に移行する。
   - text/chat 系は OpenAI-compatible facade または `/v2/tasks/*` を使う。
   - file / image / vision は OpenAI-facade base64 inline に逃げない。`/v2/objects/presign-put` と DataRef 経由にする。
   - caller 側で provider / GPU / model / tuple / fallback order を選ばない。
   - caller が送るのは task/mode/input/DataRef まで。recipe/provider/tuple 選択は gateway の責務。
   - direct hosted API fallback は本番 default で禁止。残す場合は dev/test only とし、明示 env `LLM_DIRECT_API=1` の時だけ動く fail-closed にする。

3. DataRef は live gateway の OpenAPI schema を優先する。
   - Presign endpoint は `/v2/objects/presign-put`。
   - Presign request は `name`, `bytes`, `sha256`, `content_type`。
   - `PUT` 後、返却された `data_ref` object をそのまま `input_refs` に入れる。
   - `input_refs` は `uri` field を持つ DataRef object の list。string list ではない。
   - `/v2/upload/presign` や `{"data_ref": "..."}` を task input として使わない。

4. unknown workload は必ず preflight を送る。
   - 「recipe がないから従来 API を使い続ける」は禁止。
   - `gpucall-recipe-draft` は router repo clone ではなく、wheel から `uv tool run` で実行する。
   - generated-only で終わるのは禁止。必ず `$GPUCALL_RECIPE_INBOX` に submit する。

```bash
source .gpucall/env

uv tool run \
  --from https://raw.githubusercontent.com/noiehoie/gpucall3/main/sdk/python/dist/gpucall_sdk-2.0.0a2-py3-none-any.whl \
  gpucall-recipe-draft --help
```

preflight 例。実際の workload に合わせて intent / bytes / required-model-len / business-need は変更すること。

```bash
source .gpucall/env

uv tool run \
  --from https://raw.githubusercontent.com/noiehoie/gpucall3/main/sdk/python/dist/gpucall_sdk-2.0.0a2-py3-none-any.whl \
  gpucall-recipe-draft preflight \
  --task infer \
  --mode sync \
  --intent summarize_text \
  --business-need "summarize internal operational text without sending raw prompt to hosted AI APIs" \
  --content-type text/plain \
  --bytes 16000 \
  --required-model-len 32768 \
  --remote-inbox "$GPUCALL_RECIPE_INBOX" \
  --source "$GPUCALL_SYSTEM_NAME"
```

5. canary を必ず実行する。
   - bootstrap 済みなので「接続情報なしで skip」は禁止。
   - minimum canary は text 1件。
   - vision/file path を実装した場合は DataRef canary も実行する。
   - HTTP status、gateway error code、redaction、fallback 無効化を確認する。

6. エラー分類を厳密に行う。
   - `NO_AUTO_SELECTABLE_RECIPE`: recipe 未整備。Circuit breaker を開かない。preflight/intake を送る。
   - `NO_ELIGIBLE_TUPLE`: provider capacity / policy / validation / price freshness 等で実行先なし。Circuit breaker を雑に開かない。
   - `500 Internal Server Error`: gateway-side internal error。原因を推測しない。「likely recipe missing」「probably tuple not activated」などと書かない。HTTP status、response body、endpoint、request class、bootstrap/auth/presign/preflight の成否だけを報告し、`root_cause=unverified` と書く。
   - provider runtime 5xx: provider 障害として扱う。
   - timeout: cold start と provider failure を区別できない場合、安易に circuit breaker failure に加算しない。

7. secret / prompt / URL をログに出さない。
   - API key は prefix だけ。
   - prompt 本文はログ禁止。
   - presigned URL / DataRef URI / bearer token はログ禁止。
   - 完了報告にも secret は出さない。

8. テストを追加する。
   - gpucall path が使われること。
   - direct hosted API fallback が default で無効なこと。
   - `LLM_DIRECT_API=1` の時だけ dev/test fallback が動くこと。
   - unknown workload が preflight 対象になること。
   - vision/file が DataRef 経由になること。
   - DataRef presign が `/v2/objects/presign-put` に `name`, `bytes`, `sha256`, `content_type` を送ること。
   - `input_refs` が `uri` field を持つ object list であること。
   - API key / prompt / presigned URL / DataRef URI がログに出ないこと。
   - circuit breaker が governance error で開かないこと。

9. 最後に完了報告を出す。
   報告には実コマンド出力を含めること。「できたはず」で終わらせない。

完了報告フォーマット:

```text
Completion Report

1. Bootstrap
- gateway: http://gpucall.example.internal:18088
- tenant: <system-name>
- api_key_prefix: <prefix only>
- recipe_inbox: <path>
- result: OK / FAILED

2. Inventory
| # | File:Line | Function | Current backend | Classification | Action |
|---|-----------|----------|-----------------|----------------|--------|

3. Changed files
| File | Action |

4. Preflight submissions
| intent | task | mode | required_model_len | submitted | inbox path/result |
すべて submitted=true であること。generated-only は No-Go。

5. Canary
- text canary: HTTP status / result
- vision/DataRef canary if applicable: HTTP status / result
- fallback disabled check: PASS/FAIL
- gateway 500 があった場合: root_cause=unverified と書く。推測を書かない。

6. Tests
実行したコマンドと出力を貼る。

7. Redaction
- API key logged: NO
- prompt logged: NO
- presigned/DataRef URL logged: NO

8. Remaining risk
本当に残るものだけを書く。接続情報不足は今回あり得ない。

9. Go / No-Go
Go 条件:
- bootstrap OK
- preflight submitted
- live canary executed
- tests pass
- direct hosted API fallback disabled by default
- image/file path uses DataRef production path
```

作業を開始してください。途中で「接続情報がない」「helper がない」「gateway repo を clone できない」と言って止まらないこと。接続情報は bootstrap で取得する。helper は wheel から `uv tool run` で実行する。gateway repo clone は不要かつ禁止です。
```
