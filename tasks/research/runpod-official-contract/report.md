# RunPod official contract research

調査日: 2026-05-16
モード: thorough
検索クエリ:
- RunPod vLLM OpenAI compatibility
- RunPod serverless endpoint operations run runsync status cancel
- RunPod endpoint API includeTemplate includeWorkers
- RunPod Flash Endpoint parameters requests pricing
- RunPod worker-vLLM GitHub openai_route MAX_CONCURRENCY
- RunPod Python SDK endpoint_url_base run_sync status
- RunPod GPU pool IDs gpu-types

## ソース

| ソース | 短い原文 | 種別 |
|---|---|---|
| [RunPod llms.txt](https://docs.runpod.io/llms.txt) | "complete documentation index" | official docs index |
| [OpenAI compatibility](https://docs.runpod.io/serverless/vllm/openai-compatibility) | "Supported endpoints" | official docs |
| [vLLM environment variables](https://docs.runpod.io/serverless/vllm/environment-variables) | "Environment variables" | official docs |
| [Send requests](https://docs.runpod.io/serverless/endpoints/send-requests) | "send requests" | official docs |
| [Operation reference](https://docs.runpod.io/serverless/endpoints/operation-reference) | "Endpoint Operations" | official docs |
| [Job states](https://docs.runpod.io/serverless/endpoints/job-states) | "Request job states" | official docs |
| [List endpoints API](https://docs.runpod.io/api-reference/endpoints/list-endpoints) | "includeTemplate" | official API docs |
| [Get endpoint API](https://docs.runpod.io/api-reference/endpoints/get-endpoint) | "includeWorkers" | official API docs |
| [Create endpoint API](https://docs.runpod.io/api-reference/endpoints/create-endpoint) | "workersMin" | official API docs |
| [GPU types](https://docs.runpod.io/references/gpu-types) | "GPU pools" | official docs |
| [Flash parameters](https://docs.runpod.io/flash/configuration/parameters) | "Cost-optimized, allows scale to zero" | official docs |
| [Flash requests](https://docs.runpod.io/flash/requests) | "Queue-based endpoints" | official docs |
| [Flash pricing](https://docs.runpod.io/flash/pricing) | "Idle timeout duration" | official docs |
| [worker-vLLM repository](https://github.com/runpod-workers/worker-vllm) | "MAX_CONCURRENCY" | official repo |
| [runpod-python repository](https://github.com/runpod/runpod-python) | "endpoint_url_base" | official repo |
| [runpod-flash repository](https://github.com/runpod/flash) | "DEFAULT_ENDPOINT_URL" | official repo |

## 発見事項

1. RunPod は data plane、REST management、GraphQL/control plane を分けている。公式 Flash repo でも data plane は `https://api.runpod.ai/v2`、REST management は `https://rest.runpod.io/v1`、GraphQL/control plane は `https://api.runpod.io` と別定数になっている。gpucall の `RUNPOD_API_BASE` と `RUNPOD_REST_API_BASE` の分離は正しい。

2. RunPod Serverless queue contract は `/run`、`/runsync`、`/status/{job_id}`、`/stream/{job_id}`、`/cancel/{job_id}`、`/purge-queue`、`/health`。payload は `input` に包む。gpucall の generic `runpod-serverless` adapter はこの契約に沿っている。

3. worker-vLLM OpenAI compatible endpoint は `https://api.runpod.ai/v2/{endpoint_id}/openai/v1`。docs が列挙する vLLM routes は `/chat/completions`、`/completions`、`/models`。gpucall v2 の `runpod-vllm-serverless` を `/chat/completions` に限定する設計は安全側。

4. 公式 worker-vLLM repo には `/responses` と `/messages` の実装分岐も存在する。ただし docs の Supported endpoints table には出ていない。gpucall は OpenAI facade admission、output normalization、validation artifact が揃うまで、この repo 実装を production scope に含めるべきではない。

5. worker-vLLM の `model` は、deployed Hugging Face model または `OPENAI_SERVED_MODEL_NAME_OVERRIDE` と一致する必要がある。gpucall の `runpod_vllm_config_findings()` は `MODEL_NAME` / `OPENAI_SERVED_MODEL_NAME_OVERRIDE` と tuple `model` の整合を見ており、方向性は正しい。

6. worker-vLLM の重要 env は `MODEL_NAME`、`MAX_MODEL_LEN`、`OPENAI_SERVED_MODEL_NAME_OVERRIDE`、`RAW_OPENAI_OUTPUT`、`MAX_CONCURRENCY`、`BASE_PATH`、`GPU_MEMORY_UTILIZATION`。gpucall はこのうち `RAW_OPENAI_OUTPUT` を production tuple contract として必須化していない。stream を production で無効にする限り直ちに blocker ではないが、stream 対応時は必須。

7. RunPod Flash `Endpoint` は `name=` decorator mode、`id=` existing endpoint client mode、`image=` provisioning/client mode の三つを分ける必要がある。queue-based Flash は `api.runpod.ai/v2/{id}/run|runsync|status`、load-balanced Flash は `https://{id}.api.runpod.ai/{path}`。gpucall は Flash queue adapter と LB adapter を混ぜてはいけない。

8. Flash / Serverless の warm workers と `idle_timeout` は cost contract である。`workers=(0,n)` は scale-to-zero、`workers=(1,n)` や positive `workersMin` は standing spend。gpucall の設計思想どおり、production activation 前に明示承認と cost evidence が必要。

9. Endpoint inventory は `GET /endpoints?includeTemplate=true&includeWorkers=true` が最低線。gpucall は現在この両方を付けている。ただし identity matching は endpoint id に限定すべきで、name fallback は false evidence の原因になる。

10. RunPod official `gpu-types.md` が明示する pool IDs は `AMPERE_16`、`AMPERE_24`、`ADA_24`、`AMPERE_48`、`ADA_48_PRO`、`AMPERE_80`、`ADA_80_PRO`、`HOPPER_141`。調査時点の fetched docs では `BLACKWELL_180` と `RUNPOD_B200` は pool ID として確認できなかった。

11. 現在の gpucall active config は RunPod production endpoint を持っていない。コマンド確認では RunPod worker target placeholder が 1689 件、current shell の `RUNPOD_API_KEY` / `GPUCALL_RUNPOD_API_KEY` は missing、`config/object_store.yml` も missing。

12. `RunpodVllmServerlessAdapter` は `endpoint_id` 未指定時に `GPUCALL_RUNPOD_FLASH_ENDPOINT_ID` を見る。これは serverless worker-vLLM と Flash endpoint の境界を壊す可能性がある。tuple `target` 必須に寄せるか、少なくとも serverless 用 env var に分けるべき。

13. official worker-vLLM vision DataRef path は、gpucall が image URL を OpenAI content part として転送する方式であり、worker-side SHA verification ではない。現在の code は metadata に `sha256` を要求するが、RunPod worker が URL bytes を hash 検証するわけではない。DataRef SHA verification を production invariant にするなら custom gpucall worker path が必要。

14. Flash adapter は endpoint-id mode で既存 endpoint を呼ぶにもかかわらず `cleanup_required=True` を返す。owned resource id が handle meta に無い場合、validation artifact は cleanup を完了したと主張できない。

15. `config/candidate_sources/runpod_serverless.yml` は RunPod vLLM family に `stream_contract: sse` を持つが、generated workers は `stream_contract: none` で、adapter も stream を拒否する。現在の generated output は安全側だが、source catalog が不整合なので修正対象。

## 矛盾・対立する情報

- worker-vLLM docs は supported endpoints として `/chat/completions`、`/completions`、`/models` を示す一方、official repo には `/responses` と `/messages` の分岐もある。production scope は docs に寄せ、repo-only routes は explicit opt-in にする。

- RunPod docs の job state は `RUNNING` を示す一方、SDK / operation examples には `IN_PROGRESS` 系が見える。gpucall は non-terminal unknown state を in-progress 扱いにして fail closed timeout へ寄せるのが安全。

- RunPod docs は warm workers を cost/performance tradeoff として扱うが、gpucall live billing guard は approval の有無を見ず `workersMin > 0` / active pods を block している。設計文書の「承認済み warm workers は可能」と code behavior がずれている。

- `gpu-types.md` の physical GPU list には B200 / Blackwell-class GPU があるが、pool ID table には `BLACKWELL_180` / `RUNPOD_B200` が無い。candidate source の refs は公式 pool ID としては未確認。

## 確認できなかったこと

- 実 RunPod account inventory。current shell では RunPod API key が見えないため、`GET /endpoints?includeTemplate=true&includeWorkers=true` の live payload は取得していない。

- 実 endpoint id と gpucall candidate tuple の deterministic mapping。placeholder を endpoint id に変える材料がまだ無い。

- RunPod object-store/DataRef live canary。`config/object_store.yml` が無く、object-store env も current shell では確認できていない。

- `BLACKWELL_180` / `RUNPOD_B200` が現在の RunPod API で有効な endpoint GPU selector かどうか。fetched official docs 内では pool ID として確認できなかった。

- Flash load-balanced endpoint を gpucall production egress として使う妥当性。今回は queue-based Flash と worker-vLLM を中心に読んだ。

## 取得に失敗したソース

| URL | 理由 |
|---|---|
| https://docs.runpod.io/flash/tutorials/text-generation-transformers | 4 byte response only; correct path was later fetched as `/tutorials/flash/text-generation-with-transformers` |

## この調査の限界

- 調査は official docs と official repos の静的読み込みが中心で、live RunPod endpoint、billing dashboard、real inventory は未確認。

- RunPod docs は変化し得る。今回の durable evidence は 2026-05-16 に fetch した snapshot と repo commit `87d7365` / `13e25e7` / `838018d` に基づく。

- gpucall code の全 provider surface を再監査したわけではない。対象は RunPod egress contract と、その contract に直接関係する gpucall files に限定した。

## コマンド証跡

```text
for repo in worker-vllm runpod-python runpod-flash; do printf "%s " "$repo"; git -C .state/runpod-official-contract-research/repos/$repo rev-parse --short HEAD; done
worker-vllm 87d7365
runpod-python 13e25e7
runpod-flash 838018d

if [ -n "${RUNPOD_API_KEY:-}" ] || [ -n "${GPUCALL_RUNPOD_API_KEY:-}" ]; then echo runpod_key=present; else echo runpod_key=missing; fi
runpod_key=missing

if [ -f config/object_store.yml ]; then echo object_store_config=present; else echo object_store_config=missing; fi
object_store_config=missing

rg -n "RUNPOD_ENDPOINT_ID_PLACEHOLDER|target:" config/workers | awk '...'
worker_targets=3250
worker_placeholders=1689
```
