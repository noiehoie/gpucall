# gpucall

[![CI](https://github.com/noiehoie/gpucall/actions/workflows/ci.yml/badge.svg)](https://github.com/noiehoie/gpucall/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![SDK License: Apache-2.0](https://img.shields.io/badge/SDK-Apache--2.0-green)](sdk/python/LICENSE)
[![Release](https://img.shields.io/github/v/release/noiehoie/gpucall)](https://github.com/noiehoie/gpucall/releases)

**レンタルGPU推論のための fail-closed ガバナンスゲートウェイ。**
アプリケーションは「何が要るか」（task・intent・予算）だけを宣言し、
「どこで実行するか」— Modal / RunPod / Hyperstack / ローカルランタイム — は
gpucall が決める。判断材料は検証可能な証跡のみ: ルート検証結果・価格鮮度・
プロバイダ readiness probe・予算台帳。**証跡が無ければ流さない。例外なし。**

[English README](README.md)

```text
caller / AIエージェント              gpucall gateway                     プロバイダ
─────────────────    ─────────────────────────────────────────    ────────────────
POST /v2/tasks/sync   compile: recipe → tuple chain（決定論的）      Modal functions
  task: infer     →   gates:   ルート検証証跡は新鮮か？          →   RunPod endpoints
  intent: rank         価格証跡は新鮮か？予算は確保できるか？        Hyperstack VMs
  budget, inputs       provider readinessは確認済みか？無ければ拒否   local (vLLM/Ollama)
                      audit:   試行記録・コスト確定・cleanup証跡・
                               失敗タクソノミ
                                        ▲
                      Provider Panopticon（実行パス外の監視）:
                      endpoint存在・health・queue深度・価格観測
                      → 鮮度境界付きスナップショット
```

## なぜ存在するか

「業務データを hosted AI API に出せない」と決めた瞬間、2つの事実が衝突する。

1. **レンタルGPUは安くてどこにでもある** — serverless function、managed
   endpoint、spot VM、マーケットプレイス。ただし実行面ごとにライフサイクル・
   価格・故障モード・後始末の義務が全部違う。
2. **アプリコードがGPUを選び始めた瞬間**、`modal-h100` のような文字列が
   ビジネスロジックに漏れ、どのルートが検証済みか・いくらかかったか・
   データが後始末されたかを誰も言えなくなる。

既存レイヤーは隣の問題を解いている: LLM APIゲートウェイ（LiteLLM/Portkey）は
「既存エンドポイント」へのプロキシでありエンドポイント自体のライフサイクルは
持たない。GPUオーケストレータ（SkyPilot/dstack)は計算資源を供給するが、
ルート検証証跡や fail-closed 予算ルーティングの概念を持たない。gpucall は
その間の層を所有する: **(recipe × tuple × mode × provider) のルートを
「証明されるまで信用しない」し、証明し続ける。**

ルーティング判断にLLMは一切関与しない。同じ入力・同じカタログ・同じ証跡
なら同じルート。拒否は機械可読な failure artifact（分類・呼び出し側の
次アクション・責任者付き）で返る。

## Status — 評価の前に読め

- **v2（infer / vision）: production稼働中。** 作者のニュース分析パイプ
  ラインが毎日これで回っている — 90Kトークン級のランキング、新聞紙面の
  vision OCR、JSON抽出。このパイプラインが永続カナリアであり、全リリースが
  その回帰スイートを通過する。
- **メンテナは1人。** AI支援開発を全面的に使い、品質ゲートは決定論的テスト
  （CIで1,000件超）。その前提でレビューせよ。
- **APIはpre-1.0。** マイナーバージョンで契約が壊れることがある（リリース
  ノートに記載）。
- プロバイダ: Modalが推奨happy path。RunPod/Hyperstackはアダプタ+非生成
  probe+供給プランまで。ローカルランタイム（Ollama/OpenAI互換/vLLM）は
  第一級。
- v2.5のagent-native層（estimate・failure taxonomy・MCPサーバ）は出荷済み。
  training/artifactライフサイクル（v3）は設計段階（`tasks/*-plan.md`）。

## Quickstart

CLIをインストール（`uv`が無ければ入れる。固定するなら `GPUCALL_REF=<ref> sh`）:

```bash
curl -fsSL https://raw.githubusercontent.com/noiehoie/gpucall/main/install.sh | sh
```

ガイド付きセットアップ — 初回は**クラウド認証情報ゼロ**で動く（local trial）:

```bash
gpucall setup
gpucall setup starter-plan --profile local-trial --output gpucall.setup.yml
gpucall setup apply --file gpucall.setup.yml --yes
gpucall serve --config-dir ~/.config/gpucall --port 18088
```

課金前に見積る（ルートをコンパイルするだけ。予算確保も実行もしない）:

```bash
curl -s localhost:18088/v2/estimate -X POST -H 'content-type: application/json' \
  -d '{"task":"infer","mode":"sync","intent":"summarize_text",
       "inline_inputs":{"prompt":{"value":"...", "content_type":"text/plain"}}}'
```

Pythonから:

```python
from gpucall_sdk import GPUCallClient

with GPUCallClient("http://127.0.0.1:18088") as client:
    print(client.estimate(prompt="hello", intent="summarize_text"))
    print(client.infer(prompt="hello", intent="summarize_text"))
```

SDKは別配布（Apache-2.0）:
[`gpucall_sdk-2.0.71-py3-none-any.whl`](https://github.com/noiehoie/gpucall/releases/download/v2.0.71/gpucall_sdk-2.0.71-py3-none-any.whl)
— caller側アプリはgatewayパッケージを一切importしない。

## 中核概念

| 概念 | 意味 |
| --- | --- |
| **recipe** | ワークロード契約: intent・予算・モード |
| **tuple** | 実行可能ルート1本: GPU × モデル × 実行面 |
| **route validation evidence** | 「この正確なルートが動いた」証明 |
| **Provider Panopticon** | 実行パス外のプロバイダ監視 |
| **fail closed** | 不明・鮮度切れ・価格不明 → 拒否 |

正式な文法は [docs/PRODUCT_NORTH_STAR.md](docs/PRODUCT_NORTH_STAR.md) と
[docs/OOB_USER_EXPERIENCE_PRODUCT_SPEC.md](docs/OOB_USER_EXPERIENCE_PRODUCT_SPEC.md)。

## 何が違うか

- **証跡ゲート付きルーティング**: 新鮮なprovider証跡と、recipe・tuple・
  mode・config hash単位のルート検証証跡が両方揃って初めて本番ルートになる。
  configが変わると証跡は無効化され、admin serviceが明示予算内で自動再検証。
- **予算はハード上限**: dispatch前にatomicなreserve/commit/releaseで強制。
  請求書が来てから眺めるダッシュボードではない。
- **cleanupは証跡**: ownership tag付きプロバイダ資源、cleanup manifest、
  lease回収、監査記録（`gpucall cleanup-audit`）。
- **AIエージェントが第一級caller**: 非課金 `POST /v2/estimate`、決定論的
  `GET /v2/failure-taxonomy`、polling/cancel付き非同期ジョブ、MCP stdio
  サーバ（`gpucall-mcp`）— [docs/AGENT_NATIVE_EXECUTION.md](docs/AGENT_NATIVE_EXECUTION.md)。
- **需要が供給を作る（統治付き）**: 未知のワークロードは拒否で終わらない。
  callerはサニタイズ済みintake（生プロンプトは送らない）を提出し、admin
  パイプラインがrecipe draft化→供給の選定・プロビジョニング→予算内のbillable検証
  →ルート有効化まで進める。全ステップが artifact を残す。
- **プロバイダ適合性はテスト可能**: `gpucall provider-conformance` が
  組込み13アダプタ全部に、将来のプラグインと同じ契約チェックを課す。

## これは何ではないか

- hosted プロバイダ向けLLM APIゲートウェイではない — OpenAI/Anthropic/
  Bedrockの前段なら LiteLLM / Portkey を使え。
- 汎用GPUオーケストレータでも学習スケジューラでもない — 対話的クラスタや
  スイープなら SkyPilot / dstack を使え。
- モデルマーケットプレイスでもhostedサービスでもない。プロバイダ契約は
  あんたが持ち込み、gpucallは「その使われ方」を統治する。

## 開発

```bash
uv sync
uv run pytest
uv run gpucall validate-config --config-dir config
uv run gpucall security scan-secrets
uv run gpucall provider-conformance
```

リリースゲートは [docs/PUBLIC_RELEASE_CHECKLIST.md](docs/PUBLIC_RELEASE_CHECKLIST.md)。
実カナリア運用のレポートは失敗も含めて `tasks/` にコミットしてある。
[CONTRIBUTING.md](CONTRIBUTING.md) / [SECURITY.md](SECURITY.md) も参照。

## ライセンス

- Gateway（本リポジトリ）: [AGPL-3.0-only](LICENSE)。ネットワーク越しの
  提供も頒布に含まれるため、hostedな派生物はソース公開義務を負う。
- Python SDK（`sdk/python/`）: [Apache-2.0](sdk/python/LICENSE)。caller側
  アプリにcopyleft義務は及ばない。
- 商用ライセンスはissueで相談。
