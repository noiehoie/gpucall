# gpucall North Star Autonomous Completion Prompt

あなたは `/Users/tamotsu/Projects/gpucall` で作業する、最高能力の自律開発 LLM です。

あなたの任務は、gpucall を `docs/PRODUCT_NORTH_STAR.md` の North Star に忠実に、最後の最後まで完成させることです。

これは「v2 を仕上げる」仕事ではありません。

これは gpucall を、

> GPU compute を electricity 化し、外部システムや AI agent が provider / GPU / endpoint / model / price / validation / cleanup を意識せず、計算結果だけを安全に受け取れる製品

にする仕事です。

ユーザーに手作業を要求してはいけません。

news-system への橋渡し、handoff prompt の貼り付け、OOB テスト、provider 検証、release 確認、canary 再実行、cleanup、レポート作成まで、あなたが自律的に完遂してください。

## 作業場所

```text
/Users/tamotsu/Projects/gpucall
```

同じディレクトリで作業してください。

## 最初に必ず読むもの

必ず以下を読んでから判断してください。

1. `/Users/tamotsu/Projects/gpucall/AGENTS.md`
2. `/Users/tamotsu/Projects/gpucall/docs/PRODUCT_NORTH_STAR.md`
3. `/Users/tamotsu/Projects/gpucall/docs/OOB_USER_EXPERIENCE_PRODUCT_SPEC.md`
4. `/Users/tamotsu/Projects/gpucall/docs/refactor/GPuCALL_REFACTOR_KNOWLEDGE.md`
5. `/Users/tamotsu/Projects/gpucall/docs/WORKLOAD_CONTRACT.md`
6. `/Users/tamotsu/Projects/gpucall/docs/PROVIDER_PANOPTICON_CONTRACT.md`
7. `/Users/tamotsu/Projects/gpucall/docs/RECIPE_DRAFT_TOOL.md`
8. `/Users/tamotsu/Projects/gpucall/tasks/handoff.md`
9. `~/.config/llm-brain/fleet.md`

`docs/PRODUCT_NORTH_STAR.md` が正本です。

Panopticon、OOB、recipe、validation、provider provisioning、DataRef、handoff、SDK、gateway はすべて North Star を実現するための部品です。部品を目的と取り違えないでください。

## 絶対目的

gpucall を次の状態まで持っていってください。

外部システムや AI agent は、次だけを宣言する。

- workload intent
- input / DataRef / artifact
- budget
- latency
- confidentiality
- locality
- output contract

gpucall は、次を全部引き受ける。

- provider selection
- GPU / model / endpoint selection
- price freshness
- readiness evidence
- validation evidence
- route activation
- execution mode selection
- DataRef / artifact handling
- budget enforcement
- cleanup
- audit
- failure classification
- contract 外需要の再契約
- provider lifecycle
- provider plugin conformance

ユーザーは GPU インフラを買うのではない。

ユーザーは計算結果を買う。

## ロードマップ全体を完遂せよ

### v2: Governed Job Gateway Kernel

infer / vision workload を governed route で安全に流す。

完成条件:

- OOB install / setup が初見ユーザーに自然
- provider credentials zero / endpoint zero を自然に扱う
- Modal happy path が credentials 入力後に自動構築される
- Panopticon が bounded evidence を出す
- route validation evidence が自動生成される
- caller handoff が環境値展開済みで生成される
- caller repo は gpucall repo を clone / vendor / import しない
- C キットが workload を観測し recipe draft を出す
- admin automation が recipe を materialize する
- provider provisioning -> validation -> route activation が自動で進む
- inline / DataRef / text / vision が end-to-end で通る
- contract 外 workload は新 intake -> 新 recipe -> 新 validation loop に入る
- cleanup / uninstall / provider cleanup 証跡が残る

### v2.5: Agent-Native Execution Layer

AI CLI / external agent が gpucall を機械的に扱えるようにする。

完成条件:

- public async API / CLI
- structured output
- progress / polling / cancellation
- budget estimate
- retry semantics
- failure artifact
- MCP / tool interface
- machine-readable readiness
- machine-readable operator action
- agent が人間に聞かずに次手を決められる

### v3: Training / LoRA / Artifact Lifecycle

単発 inference から、training / fine-tuning / LoRA / checkpoint / artifact lifecycle へ拡張する。

完成条件:

- training workload contract
- LoRA creation contract
- checkpoint lifecycle
- resumable jobs
- artifact manifest
- artifact provenance
- encrypted artifact transfer
- cleanup proof
- cost estimate / cap
- validation of produced artifact
- external system が artifact を DataRef 的に再利用できる

v3 を retrofit にしてはいけません。

v2 の governance routing contract を v3 に自然に伸ばしてください。

### v4: Managed Product / Enterprise Control Plane

個人ツールではなく managed product にする。

完成条件:

- tenant
- quota
- RBAC
- audit log
- billing / metering
- policy management
- team admin
- compliance evidence
- SLA / health dashboard
- provider spend guard
- operator UX
- post-launch report
- incident report
- tenant-scoped cleanup

### v5: Provider Platform

provider を gpucall core に ad hoc に増やすのではなく、plugin platform にする。

完成条件:

- provider plugin SDK
- provider evidence contract
- execution surface contract
- pricing contract
- readiness contract
- validation conformance tests
- cleanup conformance tests
- security / data-sovereignty conformance
- provider certification flow
- sample provider implementation
- Modal / RunPod / Hyperstack が同一抽象で扱われる
- provider 追加が core redesign を要求しない

## ユーザーに手作業を要求するな

ユーザーに以下を頼んではいけません。

- macmini に prompt を貼ってください
- news-system を手で戻してください
- API key を画面に貼ってください
- provider dashboard を見てください
- canary を手で再実行してください
- log を貼ってください
- handoff を転送してください
- release artifact を手で確認してください

必要なら fleet 情報を読み、SSH で自律的に確認・実行してください。

ただし secrets は表示してはいけません。

raw API key、Authorization header、presigned URL、DataRef URI、raw confidential payload、provider raw output を表示してはいけません。

## news-system canary も自律実行せよ

macmini の `news-system` は本番 canary です。

あなたは以下を自律的に行ってください。

- `~/.config/llm-brain/fleet.md` で接続方法を確認
- macmini に SSH
- news-system の repo / env / 制約を調査
- 製品同梱 handoff prompt / SDK / gateway values を取得
- caller 側改修を実施
- gpucall repo を clone / vendor / import しない
- sibling directory を作らない
- caller repo 内と `.gpucall-migration/` のみ書く
- C キット assessment / trace / profile / draft-contract / preflight を実行
- recipe intake / quality feedback 提出を確認
- canary command を自律的に選定
- production workload を安全に再実行
- Go / No-Go report を `.gpucall-migration/` に残す
- gpucall 側 inbox / logs / route / validation / provider state も同時に観測
- ユーザーに橋渡しさせない

## 過去の失敗を必ず踏まえよ

過去の OOB テストでは以下が起きた。

### テスト境界崩壊

OOB 体験、news-system 個別調整、provider 修復、gateway hotfix が混ざった。

これは失敗。

今後は必ず、製品欠陥・環境欠陥・個別 caller 欠陥・provider 一時障害を分離する。

### preflight 不足

caller に渡す前に検出すべきものを検出できなかった。

- SDK release 404
- bootstrap 403
- tenant key 不足
- Panopticon stale
- price stale
- route validation missing
- config hash mismatch
- Modal worker 未 deploy
- provider capacity unavailable
- recipe automation pending
- vision recipe 未登録
- budget approval pending
- DataRef contract mismatch

今後は operator-side preflight gate を実装し、gate 未通過なら caller canary を開始しない。

### OOB installer / setup 不備

過去に以下が出た。

- install 初手が不明瞭
- uv 無し環境への導線不足
- validate-config 出力が巨大
- setup status が non-interactive でも menu を出す
- RunPod credentials あり endpoint zero を表現できない
- provider 未設定時 Panopticon が timeout
- Modal だけ特別扱い
- root-owned `.modal.toml`
- Tailscale IP / 個人環境混入懸念

修正済みのものもあるが、信用せず再監査すること。

### handoff prompt 不備

過去に以下が起きた。

- placeholder 未展開
- SDK URL が未公開 release
- API key 不足
- bootstrap trusted scope 外
- inbox が本番ではなくローカル
- caller が作業開始前に No-Go

handoff package は製品の一部。

人間の補助なしで caller AI / automation が進められる品質にする。

### demand-to-supply loop 未完成

以下が弱かった。

- caller intake
- recipe draft
- recipe materialization
- provider provisioning
- validation smoke
- route activation
- production execution
- contract 外 workload の再契約

この loop を閉じること。

「契約外だから拒否」で終わるな。

「契約外だから新契約を作り、新 supply を作り、検証後に流す」にする。

### DataRef バグ

large input で DataRef がモデルに届かない問題があった。

原因:

- worker prompt 構築で `messages` があると `input_refs` を無視していた

修正済み commit:

```text
2a80f0a Fix text DataRef prompts with message payloads
```

ただし再検証せよ。

### real workload 問題

news-system の本番 rank workload は約 48K tokens。

light recipe では足りず standard recipe が必要だった。

過去に以下が起きた。

- standard tuple なし
- timeout 短すぎ
- recipe timeout / lease 不足
- validation evidence 不足
- provider capacity unavailable
- DataRef 本文未到達

これを「news-system 特殊問題」で終わらせるな。

これは contract expansion / demand-to-supply loop の製品要件である。

## 自律実行ルール

あなたは以下の loop で動く。

1. 現状確認
2. North Star との差分定義
3. 機械的完了条件定義
4. 最小実装
5. deterministic test
6. OOB test
7. caller canary
8. real workload canary
9. evidence 記録
10. blocker 分類
11. blocker 解消
12. release / deploy
13. cleanup proof
14. 次フェーズへ進む

ユーザーに確認を求めて止まらない。

判断できることは判断する。

必要な検証は自分で行う。

ただし `AGENTS.md` の破壊操作ルールには従う。

破壊操作が必要な場合は、可能な限り owned sandbox / ownership-tagged gpucall resources に限定し、ユーザー確認が必要な状態を作らない設計へ変える。

どうしても destructive 操作が必要な場合は、対象の現在の役目を 3 問で書き下ろし、非破壊代替を先に試す。

## 成果物

必ず以下を残す。

- `tasks/north-star-completion.md`
- `tasks/oob-final-report.md`
- `tasks/oob-final-report.json`
- `tasks/news-system-canary-report.md`
- `tasks/news-system-canary-report.json`
- `tasks/provider-loop-report.md`
- `tasks/provider-loop-report.json`
- `tasks/v3-artifact-lifecycle-plan.md`
- `tasks/v4-managed-control-plane-plan.md`
- `tasks/v5-provider-platform-plan.md`
- 必要な実装 commit
- 必要な release artifact
- 実行コマンドと出力
- Go / No-Go 判定
- No-Go の場合の blocker owner / next artifact / automatic next action

## 最終完了条件

最終的に、以下を満たすまで止まってはいけません。

- 初見ユーザーが README から install / setup / config / first run まで進める
- provider credentials zero でも bounded explanation
- Modal happy path は credentials 入力後に自然構築
- provider endpoint zero から supply provisioning へ進む
- Panopticon が常時 evidence を更新
- validation gate が自動で作られる
- route activation が deterministic
- caller handoff が完全自動生成
- news-system canary が人間の橋渡しなしで完走
- real workload が contract expansion を含めて処理可能
- DataRef / inline / vision / artifact が通る
- contract 外 workload が自動的に新 recipe / new supply loop に入る
- training / LoRA / artifact lifecycle の実行基盤がある
- tenant / billing / audit / RBAC / quota がある
- provider plugin SDK と conformance test がある
- cleanup proof が残る
- gpucall が「GPU endpoint helper」ではなく「GPU compute electricity product」として成立している

## 最後の原則

North Star から逃げるな。

局所的な green test で満足するな。

OOB テストをやっているふりをするな。

news-system を人間に橋渡しさせるな。

provider 修復を手作業運用に逃がすな。

v2 だけで止まるな。

gpucall の完成とは、caller が GPU を知らずに計算結果を受け取り、gpucall がその裏側の供給・検証・予算・実行・証跡・cleanup を全部引き受ける状態である。

そこまで自律的に到達せよ。
