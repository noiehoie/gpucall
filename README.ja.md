# gpucall v2.0

[English README](README.md)

**gpucall は「どの GPU / model / provider で処理するか」をアプリケーション側に考えさせず、組織側の policy と evidence だけで 100% 決定論的に GPU 実行を統制する gateway です。**

Gemini、GPT、Claude などの hosted AI API に業務データを投げることは、多くの組織にとって原理的には社内リソースの外部送信です。これを避けるには、自社管理の GPU 上に LLM / vLLM / Transformers を載せて処理するのが自然です。しかし GPU を購入して常時運用するには、巨額の初期投資、調達リードタイム、運用要員、余剰キャパシティの問題が発生します。

gpucall は、その中間にある現実的な選択肢を狙います。**データと統制は組織側に残し、GPU の計算力だけをクラウドから借りる**。ただし、クラウド GPU を借りる以上、どの実行先が使われたか、価格は妥当か、検証済みか、policy に合うかを、アプリケーション側の場当たり的な判断ではなく gateway の決定論的 governance で強制します。

OpenAI SDK 互換の facade の背後で、Modal serverless、RunPod managed endpoints、Hyperstack VMs、local runtimes などの heterogeneous GPU 実行面を束ねます。アプリケーション側は `task`、`mode`、input または `DataRef` を渡すだけです。gpucall が recipe、model、engine、execution tuple、price freshness、validation evidence、tenant policy を照合し、実行してよい production tuple だけに route します。

v2.0 MVP の production 対象は `infer` と `vision` です。

## どんなニーズを埋めるか

LLM / Vision を業務システムに組み込むと、現場では次の問題が起きます。

- **社内データを hosted AI API に投げたくない**: SaaS 型 AI API は便利ですが、業務データ、顧客データ、未公開文書、内部分析結果を外部 API に送ること自体が governance 上の問題になります。
- **かといって GPU を買うのは重すぎる**: 自前 GPU は調達費、設置、運用、故障対応、空き時間の無駄が大きく、需要の波にも弱い。必要な時だけクラウド GPU を借りたいという圧力が生まれます。
- **アプリケーション側が model / provider / GPU を選んでしまう**: アプリ側に `claude-haiku`、`gpt-4o`、`modal-h100` のような選択が散らばり、policy と cost control が崩れます。
- **hosted API gateway では足りない**: LiteLLM や Portkey のような API gateway は hosted model provider の統一には強い一方、自前で借りた GPU 実行面の lifecycle、validation、cleanup、billable smoke、price freshness までは主責務にしません。
- **Kubernetes inference stack では前提が重い**: すべての実行面が単一 K8s cluster 内にあるとは限りません。現実には serverless GPU、managed endpoint、IaaS VM、local runtime が混在します。
- **条件を満たせない時に勝手な迂回をしてほしくない**: 使える GPU がない、価格情報が古い、まだ検証していない実行先しかない。そういう時に「とりあえず別の安い model に投げる」動作は、コスト事故や情報管理事故につながります。
- **新しい業務要件を安全に受け付けたい**: 既存の設定で処理できない依頼が来た時、raw prompt や機密ファイルを管理者に渡して相談するのではなく、「何をしたいか」という intent だけを提出し、管理側で安全に審査・設定・検証できる流れが必要です。

gpucall はこの隙間を埋めます。既存アプリは OpenAI 互換 API または gpucall SDK で呼び出し、GPU / provider / model selection は gateway 側に回収します。未知 workload は fail closed し、呼び出し側の補助ツールが sanitized intake を作り、管理側の補助ツールが recipe / tuple / validation pipeline に載せます。

## 最大の売り: 100% 決定論ルーティング

gpucall の routing 判断に LLM は入りません。

どの recipe を選ぶか、どの tuple を候補にするか、どの provider に fallback するか、価格が予算上使えるか、validation evidence が production 昇格済みか、tenant policy に合うか。これらはすべて catalog、policy、runtime evidence、request metadata に対する deterministic evaluation です。

つまり:

- 同じ入力、同じ catalog、同じ policy、同じ live evidence なら、同じ routing decision になります。
- なぜその tuple が選ばれたか、なぜ別 tuple が reject されたかを audit できます。
- LLM による「なんとなくの model routing」や prompt classification は gateway runtime に入りません。
- unknown / stale / unvalidated / over-budget は fail closed します。

gpucall は「賢そうに選ぶ router」ではありません。**後から説明でき、再現でき、監査できる GPU governance router** です。

## 製品の形

gpucall は単なる gateway binary ではなく、3つの部品で構成される製品です。

- **Gateway runtime scripts**: 決定論的な request admission、recipe selection、tuple routing、policy enforcement、audit、validation gate、cleanup、fail-closed execution を担います。
- **呼び出し側補助ツール**: SDK に同梱される `gpucall-recipe-draft` です。外部システムは raw content や provider / GPU / model / tuple 選択を渡さず、sanitized workload intent、preflight metadata、post-failure intake、low-quality-success feedback を提出できます。
- **管理側補助ツール**: gateway 側に同梱される `gpucall-recipe-admin` です。呼び出し側 intake をレビューし、recipe intent を materialize し、不足する execution contract を導出し、candidate tuple を isolated config と billable validation を通じて昇格し、最後に production activation を許可します。

責任境界は製品契約の一部です。呼び出し側は workload intent を記述するだけです。管理側は catalog、tuple、validation evidence、production promotion を管理します。gateway は検証済み policy decision だけを実行します。

## 既存 router / inference stack ではない理由

gpucall は、すべての LLM gateway、Kubernetes inference stack、GPU provisioner を置き換えようとしているわけではありません。gpucall が埋めるのは、より狭い control-plane の隙間です。つまり、heterogeneous leased GPU surface をまたいで、gateway が recipe selection、tuple routing、validation evidence、price freshness、cleanup、audit を所有する policy-enforced execution です。

隣接システムは別のレイヤーを解いています。

| Category | Examples | 得意なこと | gpucall の境界 |
| :--- | :--- | :--- | :--- |
| LLM API gateways | [LiteLLM](https://docs.litellm.ai/), [Portkey AI Gateway](https://portkey.ai/docs/product/ai-gateway) | 多数の hosted model provider への unified API、virtual keys、fallback、cost tracking、guardrails、observability | gpucall は hosted API provider selection だけでなく、leased GPU execution surface と production tuple promotion を管理します |
| Model/provider marketplaces | [OpenRouter](https://openrouter.ai/docs/guides/routing/provider-selection) | SaaS API 背後の model provider routing | gpucall は recipe、tuple、validation artifact、object-store DataRef、provider lifecycle に対する operator-owned governance を前提にしています |
| Kubernetes inference stacks | [llm-d](https://llm-d.ai/) と Kubernetes inference-gateway patterns | Kubernetes 内の高性能 distributed inference、KV-cache-aware routing、prefill/decode separation、cluster-native operations | gpucall はすべての実行面が単一 Kubernetes cluster 内にあることを要求しません。Modal functions、RunPod endpoints、Hyperstack VMs、local runtimes を同一 governance contract で正規化します |
| GPU provisioning tools | GPU cloud provisioners and cluster schedulers | GPU capacity の取得や scheduling | gpucall は capacity を deterministic routing の入力の1つとして扱い、そこに recipe fit、model/engine compatibility、security policy、validation evidence、cost freshness、cleanup/audit contracts を加えます |

意図的に重ならない部分があります。gpucall の差別化された surface は、次の組み合わせです。

- **Heterogeneous execution governance**: Modal serverless functions、RunPod managed endpoints、Hyperstack VMs、local runtimes を、呼び出し側が選んだ provider ではなく execution tuple として表現します。
- **Deterministic four-catalog routing**: recipe、model、engine、execution tuple の compatibility を LLM-based routing なしで評価します。
- **Validation evidence before production**: YAML entry があるだけでは tuple を信用しません。review、endpoint configuration、billable validation、activation gate を通じて production 昇格します。
- **Price freshness as policy input**: configured price と live price evidence を分離します。strict budget mode では stale / unknown price data に対して fail closed できます。
- **Data-plane-less integration**: 外部システムは gateway に raw payload bytes や provider choice を渡さず、`DataRef` と sanitized recipe request を提出できます。

## LLM 境界

gateway runtime は deterministic governance runtime です。recipe、tuple、provider、GPU、model、price、stock state、fallback order、cleanup action、production promotion の選択に LLM を使ってはいけません。

LLM inference が許されるのは、deterministic routing が production tuple を選択し、worker payload を選ばれた execution surface に渡した後だけです。その時点で provider worker は、呼び出し側の task を処理するために vLLM、Transformers、worker-vLLM、または宣言済み model engine を実行できます。

呼び出し側補助ツールと管理側補助ツールは boundary tools です。呼び出し側補助ツールは deterministic のままで、sanitized intake だけを作ります。仮に LLM-assisted recipe authoring を使う場合でも、それは sanitized intake に対する audited 管理側 workflow に限定されます。production activation には、なお deterministic materialization、validation evidence、launch checks、deployment が必要です。

## Quickstart

```bash
gpucall init
gpucall configure
gpucall validate-config
gpucall doctor
gpucall tuple-audit
gpucall execution-catalog candidates --recipe text-infer-standard
gpucall lease-reaper
gpucall cost-audit
gpucall cleanup-audit
gpucall launch-check --profile static
gpucall release-check
docker compose -p gpucall up -d --build
gpucall smoke
gpucall cost-audit --live
gpucall cleanup-audit
gpucall launch-check --profile production --url http://127.0.0.1:18088
gpucall audit verify
```

production-like runtime layout は XDG に従います。

- Config: `$XDG_CONFIG_HOME/gpucall` または `~/.config/gpucall`
- State: `$XDG_STATE_HOME/gpucall` または `~/.local/state/gpucall`
- Cache: `$XDG_CACHE_HOME/gpucall` または `~/.cache/gpucall`

## MVP Scope

v2.0 で production-supported なタスク:

- Tasks: `infer`, `vision`
- Draft control-plane recipe contracts: `transcribe`, `convert`, `train`, `fine-tune`, `split-infer`
- Modes: `sync`, `async`, `stream`
- Object store: Cloudflare R2 endpoint override を含む S3-compatible API
- Deployment: Docker Compose
- State: デフォルトは SQLite WAL。`GPUCALL_DATABASE_URL` による Postgres job/idempotency backend
- Optional deployment manifests: Helm、systemd、Postgres DDL、Prometheus alerts、Grafana dashboard

v2.0 で production-supported ではないもの:

- TEE / sovereign execution のための high-confidential provider live connections

## Secrets

secret は YAML に入れてはいけません。`gpucall configure`、environment variables、または deployment secret manager を使います。

```bash
gpucall security scan-secrets
```

Provider YAML には resource shape と routing metadata だけを置くべきです。

## License

Copyright (c) 2026 Sugano Tamotsu. All rights reserved.

この repository は evaluation、integration review、security discussion のために public です。別途書面による license がない限り、open source ではありません。

## SaaS v1 Operations

外部 SaaS operation は、tenant quota YAML と credentials-managed tenant API keys を使います。詳しくは [docs/SAAS_V1_OPERATIONS.md](docs/SAAS_V1_OPERATIONS.md) を参照してください。

## Python SDK

```python
from gpucall_sdk import GPUCallClient

with GPUCallClient("http://127.0.0.1:18088") as client:
    print(client.infer(prompt="hello"))
```

async polling はデフォルトで隠蔽されます。

```python
from gpucall_sdk import AsyncGPUCallClient

async with AsyncGPUCallClient("http://127.0.0.1:18088") as client:
    job = await client.infer(mode="async", prompt="hello")
```

file は presigned PUT で configured object store に upload され、gateway には `DataRef` として渡されます。SDK は separate `gpucall-sdk` package として配布されます。gateway wheel には SDK package は含まれません。

## TypeScript SDK

```ts
import { GPUCallClient } from "@gpucall/sdk";

const client = GPUCallClient.fromEnv("http://127.0.0.1:18088");
const result = await client.infer({ prompt: "hello" });
```

## External System Migration

他の product / service を gpucall に適応させるときは、[docs/EXTERNAL_SYSTEM_ADAPTATION_PROMPT.md](docs/EXTERNAL_SYSTEM_ADAPTATION_PROMPT.md) の one-shot migration prompt を使ってください。外部システムは通常、`task`、`mode`、input data または `DataRef` だけを送ります。recipe と provider selection は gateway の責任です。

productized migration には deterministic migration kit を使います。

```bash
gpucall-migrate assess /path/to/project --source example-caller-app
gpucall-migrate preflight /path/to/project --source example-caller-app
gpucall-migrate canary /path/to/project --command "uv run python -m src.pipeline.main"
gpucall-migrate patch /path/to/project
gpucall-migrate onboard /path/to/project --source example-caller-app
```

migration kit は source files を scan し、direct OpenAI / Anthropic paths を分類し、呼び出し側 routing selectors を検出し、sanitized preflight commands を生成し、optional canaries を実行し、`.gpucall-migration` に JSON / Markdown reports を書きます。これは deterministic で、LLM を呼びません。

呼び出し側 workload が installed recipe catalog と production tuples に存在しない場合、gpucall は推測や弱い model への routing をせず fail closed します。gpucall が `200 OK` を返しても、呼び出し側 business validator が output を拒否した場合、それは low-quality success feedback として扱います。どちらの場合も、SDK に同梱された `gpucall-recipe-draft` helper で sanitize し、gpucall 管理者に recipe intent request を提出します。詳しくは [docs/RECIPE_DRAFT_TOOL.md](docs/RECIPE_DRAFT_TOOL.md) を参照してください。

unknown workload は silent routing ではなく structured governance error を返します。

- `422 NO_AUTO_SELECTABLE_RECIPE`: request を正直に記述する installed recipe がありません。
- `503 no eligible provider after policy, recipe, and circuit constraints`: recipe は存在しますが、現在実行可能な eligible provider がありません。

response には、redacted request metadata、rejection reasons、`caller_action`、redaction guarantee を含む `failure_artifact` が入ります。

この場合は independent helper を実行します。

```bash
gpucall-recipe-draft preflight --task vision --intent understand_document_image --content-type image/png --bytes 2000000 --output preflight-intake.json
gpucall-recipe-draft intake --error gpucall-error.json --intent <calling-app-intent> --output intake.json --remote-inbox admin@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox
gpucall-recipe-draft quality --task vision --intent understand_document_image --quality-failure-kind insufficient_ocr --remote-inbox admin@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox
gpucall-recipe-draft compare --preflight preflight-intake.json --failure intake.json --output drift-report.json
gpucall-recipe-draft draft --input intake.json --output recipe-draft.json
gpucall-recipe-draft submit --intake intake.json --draft recipe-draft.json --remote-inbox admin@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox
```

呼び出し側補助ツールは deterministic で、LLM を呼びません。sanitized intake と optional local draft summary を作り、gpucall 管理者が workload class を supported recipe にすべきか判断できるようにします。`--inbox-dir` または `--remote-inbox` を使うと、helper は sanitized intake を approved operator inbox に直接提出します。remote submission は SSH を使い、gateway API は呼びません。管理側が accept-all policy を採用する場合、gateway 側の `gpucall-recipe-admin materialize --accept-all` helper は sanitized intake を canonical recipe YAML に変換できます。ただし、draft または materialized recipe は、その後も `validate-config`、tests、launch checks、deployment を通らなければ subsequent requests には使えません。

gateway API を追加せず、file-based automation だけで運用する場合、管理側は次を実行できます。

```bash
gpucall-recipe-admin watch --inbox-dir /path/to/inbox --output-dir config/recipes --accept-all
```

persistent operator host では、per-command flag ではなく config で同じ route を開けます。これはデフォルトでは disabled です。

```yaml
# config/admin.yml
recipe_inbox_auto_materialize: true
```

この file が存在すると、`gpucall-recipe-admin watch` と `process-inbox` は `--accept-all` なしで sanitized 呼び出し側 submissions を materialize できます。この route は reviewed recipe YAML と static catalog-readiness report だけを書きます。billable smoke validation と production activation は別の明示的 promotion step です。provider cost を発生させたり active routing を変更したりする可能性があるためです。

Inbox processing は、提出された original JSON を audit source of truth として `inbox/processed` または `inbox/failed` に保存します。また、`inbox/recipe_requests.db` に SQLite WAL index を維持します。そこには request id、source、task、intent、status、file paths、SHA-256、timestamps が入り、operator は database を canonical payload store として扱わずに request history を query できます。

operator inbox と runtime readiness は、billable validation なしで query できます。

```bash
gpucall-recipe-admin inbox list --inbox-dir /path/to/inbox
gpucall-recipe-admin inbox status --inbox-dir /path/to/inbox --request-id rr-...
gpucall-recipe-admin inbox materialize --inbox-dir /path/to/inbox --output-dir config/recipes --accept-all
gpucall-recipe-admin inbox readiness --inbox-dir /path/to/inbox --config-dir config
gpucall readiness --config-dir config --intent translate_text
```

## Routing

gpucall は deterministic governance router であり、Modal-only proxy ではありません。Recipe と provider selection rules は [docs/ROUTING_POLICY.md](docs/ROUTING_POLICY.md) にあります。
Recipe / model / engine / provider matching の capability catalog rules は [docs/CAPABILITY_CATALOG.md](docs/CAPABILITY_CATALOG.md) にあります。
RunPod Flash production validation は [docs/RUNPOD_FLASH.md](docs/RUNPOD_FLASH.md) にあります。
RunPod Serverless catalog expansion rules は [docs/RUNPOD_SERVERLESS_CATALOG.md](docs/RUNPOD_SERVERLESS_CATALOG.md) にあります。

## Zero-Trust Contracts

Provider definitions は recipe compute requirements とは別に `trust_profile` を宣言します。restricted workloads は dedicated GPU providers、または attestation support を持つ `confidential_tee` や `split_learning` などの approved security tiers にだけ route されます。shared GPU providers は execution 前に reject されます。Governance hash は request、policy、recipe、provider contract、worker-readable DataRef set に対して deterministic に計算され、runtime IDs は除外されます。

Workers はデフォルトで gateway-presigned HTTP(S) DataRefs を consume します。ambient `s3://` worker credentials は、non-default worker environment で明示 opt-in されない限り disabled です。Chained artifacts は append-only Artifact Registry に encrypted `ArtifactManifest` entries として記録されます。gateway は lineage、version、checksums、key ids、attestation references を保存し、plaintext artifact bytes は保存しません。

Provider-independent v2.1 control-plane contracts は `train`、`fine-tune`、`split-infer` について実装済みです。explicit artifact export versions、key-release requirements、attestation-bound execution gates、split-learning activation refs、artifact manifest validation を含みます。Azure / GCP sovereign TEE と split-learning workers の provider adapters は別の実装作業として残っています。

## Object Lifecycle

Cloudflare R2 または S3-compatible buckets では、gpucall prefix に lifecycle expiration を設定してください。MVP としては次が保守的な設定です。

- Prefix: `gpucall/`
- Expire objects after: 1-7 days
- Keep public access disabled
- Limit API token permission to object read/write for the bucket

## Provider Failures

Provider outages、remote capacity exhaustion、authentication failures、provider-side queueing は gateway SLA の外です。gateway は retryability を記録し、circuit breakers を開き、deterministic fallback chain を進みます。

## Launch Checks

```bash
gpucall validate-config
gpucall doctor
gpucall tuple-audit
gpucall cost-audit
gpucall cleanup-audit
gpucall launch-check --profile static
gpucall seed-liveness text-infer-standard --count 100 --budget-usd 0.10
gpucall registry show
gpucall smoke
gpucall cost-audit --live
gpucall cleanup-audit
gpucall launch-check --profile production --url http://127.0.0.1:18088
gpucall audit verify
gpucall post-launch-report
```

Production launch checks には gateway auth、object-store credentials、live gateway smoke result、complete provider cost metadata、live provider cost/resource audit access、cleanup audit success、provider-validation JSON artifacts が必要です。Static launch checks は local config validation 用に残っています。
