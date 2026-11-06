# Research: gpucall（レンタルGPU実行面のガバナンスゲートウェイ）の市場における存在価値
- 調査日: 2026-07-02
- モード: thorough
- ソース: 取得48件 / 失敗7件
- 検索クエリ: "LiteLLM features budget" / "Portkey AI gateway features" / "LLM gateway self-hosted model serving governance" / "SkyPilot features multi-cloud GPU" / "dstack GPU orchestration features" / "Kong AI Gateway features" / "Cloudflare AI Gateway features" / "TrueFoundry AI gateway GPU" / "OpenRouter how it works" / "Shadeform GPU marketplace" / "SF Compute GPU market" / "Prime Intellect GPU aggregator" / "Vast.ai how it works" / "sovereign AI market 2025 2026" / "self-hosted LLM enterprise adoption 2025" / "EU AI Act data governance requirements inference" / "do you need an AI gateway criticism" / "self-hosting LLM not worth it cost" / "GPU serverless cold start problem production" / "build vs buy LLM infrastructure 2025" / "LiteLLM problems production" ほか

---

## 発見事項

### 1. LLMゲートウェイ勢（LiteLLM / Portkey / Kong / Cloudflare / OpenRouter）は「既存エンドポイントへのプロキシ」であり、GPU実行面のライフサイクル統治はしない

予算上限・レート制限・キー管理・ログは高度に実装済み。ただし取得した公式文書のどこにも、推論エンドポイント/GPUインフラ自体のデプロイ・検証・クリーンアップ・ライフサイクル管理の記述は存在しなかった。

| ソース | 原文引用 | 種別 |
|--------|----------|------|
| [LiteLLM Docs: Budgets](https://docs.litellm.ai/docs/proxy/users) | "After the key crosses it's `max_budget`, requests fail" | 企業 |
| [LiteLLM Docs: Budget Routing](https://docs.litellm.ai/docs/proxy/provider_budget_routing) | "Skips providers that have exceeded their budget [...] If all providers exceed budget, raises an error" | 企業 |
| [LiteLLM Docs: Providers](https://docs.litellm.ai/docs/providers) | "Call your custom torch-serve / internal LLM APIs via LiteLLM" | 企業 |
| [Portkey Docs: AI Gateway](https://portkey.ai/docs/product/ai-gateway) | "Route requests to privately hosted or local models using custom host URLs" | 企業 |
| [Kong AI Gateway Docs](https://developer.konghq.com/ai-gateway/) | "Kong AI Gateway routes AI requests to various providers through a provider-agnostic API." | 企業 |
| [Cloudflare AI Gateway Features](https://developers.cloudflare.com/ai-gateway/features/) | "Set cost-based budgets that track cumulative dollar spend across requests" | 企業 |
| [OpenRouter FAQ](https://openrouter.ai/docs/faq) | "OpenRouter is a proxy that sends your requests to the model provider for it to be completed." | 企業 |
| [OpenRouter Blog: Model Routing](https://openrouter.ai/blog/insights/model-routing/) | "By default, OpenRouter sends your request to the cheapest reliable provider, weighted by the inverse square of price." | 企業 |

⚡ **推論**: gpucallの「gatewayがrecipe/tuple/検証証跡/価格鮮度で実行面そのものを統治する」層は、この勢力の守備範囲外。ただし予算enforcement・コスト追跡・フォールバックはLiteLLM/Portkeyが既に成熟しており、その部分単体では差別化にならない。

### 2. GPUオーケストレータ勢（SkyPilot / dstack）はプロビジョニング＋コスト最適化＋自動クリーンアップまで。「証跡・fail-closed・監査」の語彙は持たない

| ソース | 原文引用 | 種別 |
|--------|----------|------|
| [SkyPilot Docs: Overview](https://docs.skypilot.co/en/latest/overview.html) | "SkyPilot's provisioner natively optimizes for cost and capacity, choosing the infra option that is the cheapest and available." | 企業 |
| [SkyPilot Docs: Managed Jobs](https://docs.skypilot.co/en/latest/examples/managed-jobs.html) | "The job is launched on a temporary SkyPilot cluster, managed end-to-end, and automatically cleaned up" | 企業 |
| [SkyPilot Docs: Autostop](https://docs.skypilot.co/en/latest/reference/auto-stop.html) | "automatically stops (or tears down) a cluster after it becomes idle" | 企業 |
| [dstack Docs](https://dstack.ai/docs/) | "dstack is a unified control plane for GPU provisioning and orchestration that works with any GPU cloud, Kubernetes, or on-prem clusters." | 企業 |
| [dstack Docs: Fleets](https://dstack.ai/docs/concepts/fleets/) | "By default, instances of a backend fleet stay `idle` for 3 days [...] it is automatically terminated." | 企業 |

収集エージェントの明示的確認: SkyPilot/dstackの取得ページに「audit trails / budget hard limits (fail-closed) / pre-flight validation / cleanup guarantees / data sovereignty evidence」に該当する原文は検出されなかった（近接機能はautostop/autodown、dstackの`max_price`リファレンス項目、"Projects for team management and resource isolation"のみ）。

⚡ **推論**: gpucallの「検証証跡が無ければ流さない」「クリーンアップの証跡を残す」というfail-closedガバナンス語彙は、検索範囲内ではオーケストレータ勢にも存在しない。ただし彼らの想定ユーザー（インフラを触るAIエンジニア）にとって、この不在は「困っていない」可能性がある。

### 3. 最も近い競合はTrueFoundry — deploy＋gatewayの統合。ただし性格はKubernetesベースのMLOpsプラットフォーム

| ソース | 原文引用 | 種別 |
|--------|----------|------|
| [TrueFoundry Docs: Overview](https://www.truefoundry.com/docs/platform/overview) | "It provides the following two key modules: **AI Engineering** and **AI Gateway**." / "Truefoundry doesn't provide compute. You bring your own cloud account or on-prem hardware." | 企業 |
| [TrueFoundry: AI Gateway](https://www.truefoundry.com/ai-gateway) | "Deploy LLaMA, Mistral, Falcon, and more with zero SDK changes. Full compatibility with vLLM, SGLang, KServe, and Triton." / "Run your own models in VPC, hybrid, or air-gapped environments." | 企業 |
| [TrueFoundry: GPU Workloads](https://www.truefoundry.com/gpu-workloads) | "Launch GPU workloads across AWS, Azure, and GCP or On-Prem" / "Automatically stop unused GPU workloads with custom idle policies" | 企業 |
| [TrueFoundry Blog（自社比較・バイアス注意）](https://www.truefoundry.com/blog/a-definitive-guide-to-ai-gateways-in-2026-competitive-landscape-comparison) | "Infrastructure awareness: GPUs, concurrency, and runtime behavior are visible and controlled" | 企業 |

⚡ **推論**: 「モデルのセルフホストデプロイ＋ゲートウェイ統治」を一体で売る商用プレイヤーは既に存在する。gpucallとの差は、TrueFoundryがKubernetes/BYOC前提のエンタープライズMLOps製品であるのに対し、gpucallはserverless GPU（Modal/RunPod）とルート単位の検証証跡・fail-closedに寄せている点。この差が顧客にとって購買理由になるかは本調査では確認できない。

### 4. GPU価格の集約・比較・市場化は既にコモディティ — ただし全プレイヤーがガバナンス層を持たない

| ソース | 原文引用 | 種別 |
|--------|----------|------|
| [Shadeform](https://shadeform.com/) | "Deploy high quality GPUs at the best prices across 30+ top clouds worldwide." | 企業 |
| [SF Compute](https://sfcompute.com/) | "Need to cancel? Just sell back what you don't need." / "Buy H100s from $2.04/hr" | 企業 |
| [DCD: SF Compute調達](https://www.datacenterdynamics.com/en/news/sf-compute-raises-40m-for-ai-compute-marketplace-offering/) | "This unlocks liquidity, efficiency, and price discovery in a market that has historically been rigid and hard to access." | 業界紙 |
| [Prime Intellect Blog](https://www.primeintellect.ai/blog/compute) | "Fragmented Pricing: H100 GPU costs fluctuate wildly from $1.2 to $18 per hour across providers." | 企業 |
| [Vast.ai](https://vast.ai/) | "Prices set by supply and demand across 20,000+ GPUs. Transparent. Programmatically queryable." | 企業 |
| [GPUPerHour](https://gpuperhour.com/) | "GPUPerHour tracks 17,888 cloud GPU offers from 28 providers, with prices updated every 60 seconds." | その他 |

収集エージェントの明示的確認: いずれの公式ページにも「検証・監査・予算fail-closed・クリーンアップ証跡」に相当する原文記述は確認できなかった。

⚡ **推論**: gpucallの「価格鮮度evidence」は単独では価値が薄い（60秒毎更新の無料比較サイトすらある）。価値が出るとすれば「価格evidence×検証evidence×予算fail-closedをルーティング判定に接続した」複合部分のみ。

### 5. 需要側: 主権AI・規制起点のガバナンス需要は実在し数字も大きいが、「レンタルGPUセルフホスト推論」の採用率はソースによって激しく食い違う

| ソース | 原文引用 | 種別 |
|--------|----------|------|
| [EU AI Act Article 12（欧州委員会）](https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-12) | "High-risk AI systems shall technically allow for the automatic recording of events (logs) over the lifetime of the system." | 官公庁 |
| [Help Net Security](https://www.helpnetsecurity.com/2026/04/16/eu-ai-act-logging-requirements/) | "Annex III obligations take effect August 2, 2026" / "the penalty is up to 15 million euros or 3% of worldwide annual turnover" | 業界紙 |
| [Precedence Research](https://www.precedenceresearch.com/sovereign-ai-infrastructure-market) | "The global sovereign AI infrastructure market size accounted for USD 15.00 billion in 2025" [...] "CAGR of 28.00% from 2026 to 2035" | 企業 |
| [MarketsandMarkets](https://www.marketsandmarkets.com/Market-Reports/sovereign-ai-market-142339478.html) | "The global sovereign AI market was valued at approximately USD 40.0 billion in 2025" | 企業 |
| [Gartner（BizTechReports転載）](https://www.biztechreports.com/news-archive/2026/2/17/global-ai-regulations-fuel-billion-dollar-market-for-ai-governance-platforms-gartner-february-18-2026) | "spending on AI data governance expected to reach $492 million in 2026 and surpass $1 billion by 2030" | 業界紙 |
| [Menlo Ventures](https://menlovc.com/perspective/2025-the-state-of-generative-ai-in-the-enterprise/) | "Open-source LLMs hold only 11% of today's market" | 企業 |
| [Databricks](https://www.databricks.com/blog/state-ai-enterprise-adoption-growth-trends) | "76% of organizations choose open source LLMs" | 企業 |
| [Clarion.ai（Gartner引用・ベンダー資料）](https://clarion.ai/insights-deploying-llms-on-premise-data-sovereign-enterprise/) | "by 2030, more than 75% of European and Middle Eastern enterprises will geopatriate their virtual workloads [...] Today that figure sits below 5%." — Gartner Predicts 2026 | 企業 |

### 6. 反論側: セルフホスト推論の経済性はhosted APIに対してほぼ常に不利、serverless GPUのcold startは製品レベルの問題、gateway層は複雑性が正当化される場合のみ

| ソース | 原文引用 | 種別 |
|--------|----------|------|
| [DETECTX](https://www.detectx.com.au/cost-comparison-api-vs-self-hosting-for-open-weight-llms/) | "Should we self-host an LLM instead of using an API? 𝗔𝗻𝘀𝘄𝗲𝗿: Almost never." / "self-hosting would be on the order of $30+ per day for 1M tokens – over 100× the API cost." | 企業 |
| [TianPan.co](https://tianpan.co/blog/2026-04-15-build-vs-buy-llm-infrastructure) | "Staff is the dominant cost, not hardware." / "APIs win decisively" / "The teams that get this right don't pick a side. They build a routing layer early [...] and let the numbers tell them which traffic should move." | ブログ・SNS |
| [regolo.ai](https://regolo.ai/scale-to-zero-cold-start-latency-why-serverless-gpu-breaks-real-time-ai-and-how-to-fix-it/) | "'serverless' GPU deployment [...] often introduces catastrophic cold start delays of 30 to 60 seconds when scaling from zero" | 企業 |
| [Spheron Blog](https://www.spheron.network/blog/gpu-cold-start-llm-inference-2026/) | "Scale-to-zero is a dev/staging optimization, not a production strategy" | 企業 |
| [ngrok blog](https://ngrok.com/blog/ai-gateways-2025) | "the question actually isn't whether an AI gateway is good, but whether your complexity warrants it." | 企業 |
| [LiteLLM GitHub Issue #21046](https://github.com/BerriAI/litellm/issues/21046) | "Request throughput to a model on plain GPU hardware is around 16 req/s. With LiteLLM inbetween, throughput drops to ~ 9 req/s" | その他（一次情報） |
| [Jimmy Song](https://jimmysong.io/blog/ai-gateway-in-depth/) | "many current AI traffic tools are still unproven in large-scale production, requiring careful evaluation before investing in critical business." | ブログ・SNS |
| [Hacker News（Algolia API経由）](https://news.ycombinator.com/item?id=34911273) | "you generally ended up with a subjectively worse model AND a lower inference speed - which resulted in me canning my idea as it was simply too expensive." | ブログ・SNS |

---

## 矛盾・対立する情報

| 論点 | ソースA | ソースB |
|------|---------|---------|
| セルフホスト/オープンソースLLM採用率 | "Open-source LLMs hold only 11% of today's market" — [Menlo Ventures](https://menlovc.com/perspective/2025-the-state-of-generative-ai-in-the-enterprise/) | "76% of organizations choose open source LLMs" — [Databricks](https://www.databricks.com/blog/state-ai-enterprise-adoption-growth-trends)（分母が異なる: Menloは支出シェア、Databricksは自社顧客の採用有無とみられるが本文では確認できず） |
| sovereign AI市場規模（2025） | "USD 15.00 billion in 2025"（インフラ限定） — [Precedence Research](https://www.precedenceresearch.com/sovereign-ai-infrastructure-market) | "approximately USD 40.0 billion in 2025"（広義） — [MarketsandMarkets](https://www.marketsandmarkets.com/Market-Reports/sovereign-ai-market-142339478.html) |
| セルフホストの経済合理性 | "Almost never [...] over 100× the API cost" — [DETECTX](https://www.detectx.com.au/cost-comparison-api-vs-self-hosting-for-open-weight-llms/) | "71% of AI infrastructure now operates outside public cloud environments" — ESG 2025（[Clarion.ai経由](https://clarion.ai/insights-deploying-llms-on-premise-data-sovereign-enterprise/)、原本未確認） |

---

## 確認できなかったこと

- **「レンタルGPU実行面の検証証跡・価格鮮度evidence・クリーンアップ証跡・fail-closedルーティング」を一体の製品として提供する既存プレイヤー**: 検索範囲内では見つからなかった。各収集エージェントが個別に「該当記述なし」を確認している。ただし非公開のエンタープライズ製品・SIer内製は検索で拾えない。
- SF Computeの市場メカニズム詳細docs: リダイレクト+404で本文取得不可。
- Gartnerプレスリリース原本: gartner.com 403（転載で代替）。
- Menlo「11%」とDatabricks「76%」の分母定義: 各本文内に明確な定義記述なし。
- dstack `max_price` の正確な挙動（fail-closedか否か）: リファレンス項目の存在のみ確認、説明文なし。

---

## 取得に失敗したソース

| URL | 理由 |
|-----|------|
| https://dstack.ai/ | JSレンダリング依存で本文取得不可 |
| https://docs.sfcompute.com/docs/how-the-market-works | 307→404 |
| https://www.gartner.com/en/newsroom/press-releases/2026-02-17-... | 403 Forbidden |
| https://www.statista.com/statistics/1661777/... | リダイレクト超過 |
| https://app.daily.dev/posts/litellm-has-some-serious-issues-in-production-... | 308リダイレクト未追跡 |
| https://apxml.com/courses/mlops-for-large-models-llmops/... | 403 Forbidden |
| https://news.ycombinator.com/item?id=34911273（直接） | 429（Algolia API経由で代替取得済み） |

---

## この調査の限界

- 検索エンジン: WebSearch（結果はSEO・アルゴリズムにバイアスされている）
- 言語: 英語中心（日本語市場の個別動向は未調査）
- アクセス不可: Gartner/Statista等のペイウォール原本、非公開エンタープライズ製品、SIer内製ソリューション
- ソースの大半がベンダー公式文書（corporate）であり、機能の「不在」は文書上の不在であって実装上の不在の証明ではない
- 時点: 2026-07-02時点のWeb情報。以後の変化は反映されない
- 同一基盤モデルのサブエージェントによる収集のため、抽出判断に共通バイアスの可能性

**この調査は出発点であり、結論ではありません。重要な判断に使う場合は一次ソースの直接確認を推奨します。**
