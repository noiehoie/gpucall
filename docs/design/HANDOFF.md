# gpucall 引継書 — 構築を引き継ぐ後継AIへ

作成: 2026-09-05 / 引き継ぐ相手: gpucall の再構築を担う次のAI / 依頼者: 菅野完(オーナー・唯一の運用者・品質の最終審判)

---

## 0. まず読む順序と、この文書の立ち位置

あなたは gpucall という自作システムの構築を引き継ぐ。だが「今あるコードを完成させる」仕事ではない。**夢見た帝国を弔い、骨だけの正しい姿へ収縮(または白紙から書き直し)する**仕事だ。

読む順序:
1. §1「夢見ていたもの」— 何を目指していたか(理想論・完全版)。これは弔うためにこそ、正確に知れ。
2. §2「現実の評決」— なぜ夢を畳むのか。実測データに基づく。
3. §3「再定義されたミッション」— 作るべきものの一文と骨格。
4. §4「品質のトレードオフ」— 最重要の戦略判断。ここを誤ると製品が死ぬ。
5. §5〜§7 — コードの現状、手痛く学んだ事実、環境の罠。
6. §8〜§11 — 作業規律、未決の判断、禁則、成果物の所在。

併存する英語の正式文書(§11):`docs/design/conformance-governance.md`(ただし後述の通りこれは「機能追加=patch」の設計であり、再設計ではない)、`docs/design/lessons-engineering-discipline.md`、`docs/ARCHITECTURE_NOTES.md`。

**警告(この引継書自体の限界):** 前任(私)は gpucall 45,301行のうち約8%(domain / config / routing / config_fingerprint / validation_evidence / compiler の6ファイル)しか精読していない。file:line 付きの主張はその範囲で裏取り済み。それ以外は未読。前任は本セッションで繰り返し「読まずに断言」して誤認した(§8)。**この引継書の"事実"は6ファイルの範囲でのみ信用し、残りは自分で読んで確かめよ。**

---

## 1. gpucall が夢見ていたもの（理想論・完全版）

gpucall は、ただのプロキシでもルーターでもなかった。**「GPU実行という無法地帯に、初めて"統治"を持ち込む」**という一点に賭けた製品だった。

出発点の痛み: 世に Modal / RunPod / Hyperstack / Baseten はある。GPUは借りられる。vLLMで推論は回る。LiteLLMでOpenAI互換の口も作れる。だが**誰も、その実行が「正しく・安全に・証拠を残して・予算内で」行われたことを保証していなかった**。いくらかかるはずだったのか、なぜそのGPUが選ばれたのか、機密データはどこへ流れたのか、使い終わったリソースは本当に片付いたのか——答えを持つ者がいなかった。クラウドLLMゲートウェイ勢(Portkey / Kong AI Gateway / Cloudflare AI Gateway)はホスト型APIの前に立つだけ。SkyPilot / dstack はGPUをオーケストレートするがガバナンス・監査・fail-closed予算統制まではやらない。**その空白に旗を立てようとした。**

夢の心臓部は4つの理想:

- **決定論的ガバナンス** — 「同じ入力・同じカタログ・同じ証跡なら、必ず同じ判断が出る」。ルーティング決定に推論を一切混ぜない。どのGPUに・どのモデルで・どのエンジンで流すかは、賢いLLMの気まぐれでなく、compilerが recipe と tuple のカタログから機械的に導く、再現可能で監査可能な決定。各判断は「なぜ選ばれ、なぜ他が弾かれたか」を rejection reason として言語化する。判断が説明責任を負う。

- **fail-closed** — 確かめられないなら実行しない。価格が古ければ止まる。検証証跡が無ければルートに載せない。予算超過は課金前に弾く。安全でない方向へは決して倒れない。他人の金と機密を預かる者の作法。

- **カタログの帝国** — models 67、engines、workers 3,236。あらゆるGPU(T4〜H200×4)×あらゆるモデル(0.5B〜Qwen3-235B)×あらゆるプロバイダを tuple として網羅。「どんなワークロードが来ても最適な実行面を必ず選び出せる」ために、可能性を先に全部宣言しておく、と信じた。

- **DataRef とデータ主権** — 機密ペイロードはゲートウェイを通さない。presign参照だけが流れ、実データはオブジェクトストアに留まる。使い終われば回収し、削除レシートを取り、absentを検証する。学習成果物すら暗号化して手元に引き戻す。「OpenAIにもAnthropicにも手札を一枚も渡さない。GPUだけ借りて、データも学習も全部回収する」。

その上に野心が積まれた: **Provider Panopticon**(実行面を経路外から一望監視)、**エージェント・ネイティブ**(estimate / failure-taxonomy / MCP)、そして五合目まで描かれたロードマップ——**v2 統治カーネル → v2.5 エージェント対応 → v3 学習/LoRA/成果物ライフサイクル → v4 マネージド化・RBAC・課金 → v5 プロバイダ・プラグイン・プラットフォーム**。最終地平線は「**誰もが自分のプロバイダを差し込め、誰もが安全にレンタルGPUを統治しながら使える、GPU実行の"発券・統治・清算"の標準基盤**」。tenant・予算・監査・出荷受け入れ検査(production_acceptance / shipment_gap / handoff_package)があるのは、**いつか本物の顧客がこれを買い、乗り、信頼する日**を見据えていたから。コードの8割は、まだ存在しない顧客への先回りのラブレターだった。

一言で: **「GPUを借りて動かすという行為に、初めて"統治"と"良心"と"証拠"を与える標準基盤」。速さでも安さでもなく、"信頼できることそのもの"を売ろうとした。市場の誰もやっていない一点で。**

---

## 2. 現実の評決（2026-09時点）

### 2.1 市場の判定
2026年5月の市場調査(LiteLLM / Portkey / OpenRouter / SkyPilot / dstack / Modal等 / vLLM stack / KServe / Kong AI Gateway / TrueFoundry / BentoML と比較)の結論: 差別化(レンタルGPU実行面のガバナンス)は既存製品の組合せで代替可能な面が大きく、**事業化は断念済み**。ただし「fail-closed証跡・決定論・主権回収」は市場に単一製品としては存在しない — そこだけは本物の空白。

### 2.2 本番の実測(これが全ての土台)
本番 netcup の実績台帳(`tenant_usage.db` 1,391件 / `registry.db` 観測1,123件)を数えた:

| 事実 | 数字 |
|---|---|
| トラフィックを運んだ tuple | **8個(全てModal。RunPod/IaaS本番実績ゼロ)** |
| 直近60日で生きている tuple | **5個** (vision L4 Qwen2.5-VL-3b:491 / T4 0.5b:343 / a10g:78 / H200 Qwen3-32B:15 / distill:2) |
| catalog の worker 定義 | **3,236 — 99.85%は一度も使われていない** |
| 使われた recipe | 8 / 34 |
| tenant | news-system が98.7%(1,373件)。llm-brain は生涯2件 |
| コード規模 | 45,301行(cli 3,942 / migrate 2,943 / recipe_admin 2,482 が巨大) |

**実質 caller は news-system 一本。** 帝国の顧客は最初から一人だった。

### 2.3 病理の診断
実証された欠陥(Florence-2のcapability偽宣言 / 16px合成画像で検証構造的失敗 / chat_messages×vision検証不能 / config1行変更で全route無効化 / watch予算$0.10で大型route再検証不能)は個別バグではない。**「3,236の可能性を宣言してから検証で追いかける」設計が、物理的に検証不能(235B検証は$8/回、全数検証は成立しない)**であることの症状。加えて決定的な実証: **前任(LLM込みの運用者)がシステム全体を1コンテキストに収められなかった**。個人主権スタックにとって「運用者が一晩で全部読めること」は美学でなく稼働要件。45k行はそれを超えている。

### 2.4 評決
- 設計思想・野心: **A**(この抽象度を一人で設計しきったのは非凡)
- コアのコード品質(読んだ範囲): **B**(型厳密、rejection構造化、evidence gate実装)
- fail-closedの完全性: **C**(宣言vs実能力の突合が無い=宣言を無検証で信じる急所)
- 実装の完成度: **C−**(chat×vision欠陥、catalog↔worker乖離、「たまたま落ちて発覚」する脆さ)
- 規模・保守性: **C**(45k行/一人、巨大ファイル、重複)
- 市場競争力(事業製品): **D** / 個人主権基盤としての適合: **B+**
- **総合: C+**。ただし「事業製品」ならC〜C−、「菅野個人の道具」ならB。

---

## 3. 再定義されたミッション（骨だけの姿）

夢を「魂」と「帝国」に割れ。**魂は残す。帝国は弔う。**

### 3.1 一文のミッション
> **ローカルLLM(艦隊のOllama等)から来る、GPUを必要とする要求を、最適かつ最も安価なレンタルGPUにルーティングして実行し、使い終わったら片付ける。それを決定論で、fail-closedで。**

「レンタル」の一語が全てを背負う。ホスト型APIに振るなら不要な fit/cost/validation/cleanup を、"借りた容量に振る"ことが否応なく必須にする。gpucallがLiteLLMより大きいのは夢が大きかったからでなく、狙う先が"レンタル"だったから。それだけ。

### 3.2 落とせない構成要素(動詞4つ)
1. **決める — カーネル(compiler)**: recipe(需要) × カタログ(供給) → 最安で適合するtuple。budgetとvalidation evidenceでgate。ルーティング頭脳は既に存在する(compiler.py `_tuple_fit_key` が「まず適合、次に最安」で並べる)。**新規に作るな、瓦礫から抜き出せ。**
2. **測る — 痩せたパノプティコン**: 名前は残るが中身は別物。残すのは「価格が今も最安か」「エンドポイントは生きてるか」だけの経路外センサー。今のPanopticonの9割(remediation policy / provisioning / 3,236監視 / worker-context probe)は帝国の装置。捨てる。
3. **実行する — dispatcher + workerアダプタ(Modal一個)**: 借りたGPUで走らせ結果を持ち帰る。ルーターは振り分けるだけでなく転送して返す。
4. **回収する — reaper(sovereignty)**: 使い終わったら片付ける。経路外cronでいいが落とせない(「レンタル」の税)。sovereignty reap は既に動く。

### 3.3 データ2軸(混同するな)
- **レシピ(需要側)** = callerが「何を欲しいか」(品質下限 quality_floor / 遅延 / capability / task)
- **供給カタログ(供給側)** = 「借りられるGPU/モデルと、その仕様と価格」(実測15本前後のtuple/model)
- カーネルはこの2つを突き合わせる装置。「レシピシステムだけ」では供給カタログが抜ける。両方要る。

### 3.4 目標規模
**45k → 10k行前後。** worker 3,236→実証済み+意図的予備の15前後。models 67→8前後。migrate.py 2,943行削除(git履歴に生きていればいい)。production_acceptance / installed_product_acceptance / shipment_gap / handoff_package / readiness ≈2,700行削除(顧客のいない出荷セレモニー)。本番実績ゼロのRunPod/IaaS面(≈4,500行)の大半削除。tenant機構簡素化。**「運用者が一晩で全部読める」を稼働要件として明文化せよ。**

### 3.5 収縮(C) と 撤退(D) は同じ場所に着く
白紙から魂だけ書いても(B)、削って10kにしても(C)、行き着く先は同じ大きさ・同じ形。違いは「帝国の亡霊を背負って歩くか、置いて歩くか」だけ。そして撤退基準: 収縮後6ヶ月で第二の実caller(月100件以上)が現れない、または保守時間が月数時間を超え続けるなら、薄いライブラリ(D:各callerがModal直叩き+sovereignty cron)に降りろ。カーネル化しておけばDへの移行も最小コスト。**Cは継続と撤退どちらに転んでも損しない手。**

---

## 4. 品質のトレードオフ（最重要の戦略判断・ここを誤ると製品が死ぬ）

### 4.1 動かせない天井
**Opusはレンタルできない。** gpucallが借りられる供給の上限はQwen3-235B / DeepSeek-R1 / Llama級のオープンモデル。本物の推論・本物の日本語文章生成・複雑な多段タスクを gpucall に流せば、cc(Claude, Opus/Sonnet)に比べ品質は測定可能なほど下がる。この差は縮まらない。

### 4.2 だが答えは「何を流すか」で全部変わる
本番実測が運んでいたのは vision-OCR/文書抽出・翻訳・JSON抽出・ランク・要約。**これはフロンティア推論でなく、構造化された中難度のバルク処理**で、ここではオープンモデルとccの差は**小さい**。しかもcompilerが品質ハーネス(guided decoding / output validation再試行 / 要約のfact-lock=数字と固有名詞をverbatim保持)を被せる。3Bでも「賢くなくても信頼できる」抽出になる。

### 4.3 news-system の題(一番きつい戦場)
「ニュース要約PDFを日本語の読み物として高水準で」——これは抽出でなく**"文章を書く"仕事**で、gpucallの弱点が最も出る。
- 現カタログ(Qwen中心)ではSonnet級の日本語は厳しい(Qwen系の日本語は"通じる"が翻訳調・硬い)。
- だが**日本語特化の大型オープンモデル**(Swallow / PLaMo / Sarashina / calm3 / ELYZA — カタログ未収録だがレンタル可)ならQwenより明確に上。Sonnet級に届くかは**未検証・断言不可**。真のリスクはピーク品質でなく一貫性(時々ぎこちない段落を混ぜる=「高水準を維持」が危ない)。

### 4.4 決定的な問い直し(後継者が最初に確認すべきこと)
**news-systemがgpucallを使う駆動因は「秘匿」か「コスト」か? — オーナーに必ず確認せよ。答えで設計が変わる。**
- **コストなら**(ニュースは大半公開情報→漏洩損失なし): パイプラインを割れ。バルク(OCR/抽出/翻訳/ランク)はgpucall(安く主権も損なわない)、**最後の"日本語要約を書く一手だけをSonnetに投げる**(公開入力・公開出力、秘匿すべきものが無いなら主権で縛る理由が無い)。品質が要る場所だけccの賢さを買い、量が要る場所はgpucallで削る。両取り。
- **秘匿なら**(一部ソースが機密): 書く工程もオープンモデルに縛られる。日本語特化モデルを選び品質リスクを飲み、テストで詰める。

### 4.5 唯一正直な決め方
推測でもベンチ数字でもなく——**同じ実際の紙面で要約を「Sonnet / Qwen3-235B / 日本語特化オープン大型」で並べ、出所を隠してオーナー(プロの書き手=品質の唯一の審判)が読み比べて評点する盲検テスト**。これが「読み物として高水準か」の唯一の基準。

---

## 5. コードの現状と構造（実測・file:line は6ファイル範囲のみ信用可）

- 場所: `/Users/tamotsu/Projects/gpucall`(macstudio母艦=開発)。本番=netcup 100.91.94.11:18088(`~/bin/gpucall-gateway-start.sh` で起動、admission 4/6/8固定)。最新 v2.0.71。mainブランチがcanonical、AGPL-3.0公開。
- 精読済み(file:line信用可): domain.py(1030) / config.py(423) / routing.py(455) / config_fingerprint.py(31) / validation_evidence.py(313) / compiler.py(900)。詳細は §6 と `docs/ARCHITECTURE_NOTES.md`。
- **未読(自分で読め)**: cli.py(3942) / migrate.py(2943) / recipe_admin.py(2482) / worker_contracts/* / execution_surfaces/*(4523) / sdk/*(1466) / mcp/*(2236) / panopticon群 / dispatcher / app。
- 状態DB(netcup `~/.local/state/gpucall/`): `state.db`(jobs=完了後掃除で0件) / `registry.db`(observations/circuit_breakers/route_quality_failures) / `tenant_usage.db`(実績台帳=使用実測の源) / `idempotency.db` / `artifacts.db`。
- 別スレッドで Sonnet が determinism refactor(I1-I9: 重複除去・FP等価・マジックナンバー命名)を進行していた。I1-I4程度がcommit済みの可能性。核の正規化なので活かせる。

---

## 6. 手痛く学んだ技術的事実（後継者が同じ穴を踏まないために・全て6ファイル精読で裏取り済み）

- **ModelSpec は pydantic BaseModel(`extra="forbid"`)**。frozen dataclassではない(domain.py:614)。未定義キーをymlに足すと即エラー。新フィールドはスキーマに既定値付きで正式追加せよ。
- **tuple は config/tuples に無い**。`config/surfaces/*.yml` と `config/workers/*.yml` を `worker_ref` で結合して合成される(config.py:132-176、`payload={**surface,**worker}`)。`input_contracts` はこの合成tupleが持つ。
- **`validate_config`(config.py:293-399)は既に大量の宣言整合性を突合**: tuple.model vs model.provider_model_id(343-347) / tuple.max_model_len ≤ model.max_model_len(348) / engine / adapter descriptor。**欠けているのはcapability宣言 vs worker実受理集合の突合だけ。**
- **capabilityマッチングは宣言vs宣言**(routing.py:364-369: `recipe.required_model_capabilities ⊆ model.capabilities`)。model.capabilitiesが真実かは検証しない=偽宣言が通る正確な理由。
- **config指紋はモノリシック単一SHA256**(config_fingerprint.py:17-31、全yml。除外はcredentials.ymlとtenants/のみ)。1ファイル変更で全route evidence無効化。
- **検証evidenceは(tuple,recipe,mode)粒度**(validation_evidence.py:17)。config_hash + git commit の両方に紐付き(219-225)、strict時mismatchで無効。passed判定(228-231)で失敗時error.code露出。
- **★chat_messages契約のvisionは現compilerで構造的に成立しない**: compile(174-287)が唯一経路。`_validate_request_against_recipe`(380-431)で 383-384 が messages+input_refs併用を禁止、385-386 が vision に image input_ref必須。→ 画像をchat contentに埋めるとinput_ref画像が無く弾かれる。**合成入力ビルダだけでは直せない=compilerのvision判定変更を要する。** これがRunPod 7B vision GovernanceErrorの真因。
- **tuple選択 `_tuple_fit_key`(compiler.py:479-498)**: (local_preference, route_quality_penalty[cap99], reliability_tier[success≥0.5→0], vram_gb, max_model_len-required, budget_reservation)。同点は registry.rank(観測実績)。**"まず適合、次に最安"の頭脳は既にここにある。**
- **ProviderErrorCode(domain.py:76-102)=一時的失敗23種** / **TupleError.retryable(888)**。失敗分類(構造的=GovernanceError vs 一時的)の土台。
- **PanopticonRemediationPolicy.exclude_from_routing(domain.py:457)** が既存=routing除外remediation。隔離は新設せず既存に繋ぐ。
- コスト推定は「min(秒単価)」でない: cost_per_second / expected_cold_start_seconds / min_billable_seconds / scaledown_window / standing_cost をモデル化済み(domain.py ExecutionTupleSpec)。**"最安"はジョブ全体コスト推定問題。ここは削るな=レンタルGPUルーターの本当のエンジニアリング。**

---

## 7. 環境の罠（絶対に踏むな・前任が全部踏んだ）

- **git操作は Bash に `dangerouslyDisableSandbox: true` が必須。** sandbox内gitは実FSの.gitに届かず、commitが「消える」ように見える。前任は虚偽の進捗報告を繰り返した。
- **Edit/Write tool は実FSに反映されない**(`success`を返すが未反映)。ファイル編集は Bash(python heredoc) で行え。
- **worktree は `~/.worktrees/<repo>/<name>`(sandbox外・実ディスク)** に `dangerouslyDisableSandbox` で作る。WorktreeCreate hook(`~/.claude/scripts/worktree-event-logger.sh`)がこの場所に作る正規の仕組みだが `jq` 依存で、jq未インストールだとEnterWorktreeが失敗する。同一working dirで複数エージェント並行はgit HEAD競合で互いの作業を壊す — 必ずworktree分離。
- **rtk(トークン節約プロキシ)のgit表示がバグる**ことがある。`command git` + `dangerouslyDisableSandbox` で確認せよ。ツールのsuccess/表示を鵜呑みにするな、独立手段で裏取り。
- gateway再起動は必ず `ssh netcup '~/bin/gpucall-gateway-start.sh'`(admission limits維持)。他のnetcup webサービスを止めるな。**akagiプロジェクトには触るな。**
- billable検証(tuple-smoke): H200系cold start 6-10分、budget 32B系$2.5/235B$8。opus-4-8系はtemperature/top_p/top_kを400拒否。

---

## 8. 作業規律（9則・前任が破って学んだ）

1. 環境の書き込み永続性を最小実験で確認してから作業。ツールのsuccessを信じず独立readで反映検証。
2. ツール失敗は一次情報(トレース/ログ/hook本体)で真因を見る。早合点で回避策に走らない。
3. 事実主張・設計は該当コードを1行ずつ読んでから。断片のつまみ読みで全体を推論しない。
4. 完了・完成の主張は独立した権威ある手段(生git/実FS/別プロセス)で裏取りしてから。
5. 未検証は「確定事実」と「仮定」に峻別し明示。完璧を装わない。
6. 指示の目的を最初に言語化し、手段が奉仕しているか都度照合。目的を見失ったら止めて確認。
7. 並行作業は状態(git HEAD/working tree)の隔離を最初に確保。
8. コスト・波及(課金/本番config変更)を伴う操作は影響範囲を把握してから。本番configを把握前に変えると config_hash波及で全route無効化する。
9. オーナーの前提が事実と違えば迎合せず事実で正す。確認できないことを断言もしない。

---

## 9. 未決の判断（後継者がオーナーと決めること）

1. **news-systemの駆動因=秘匿かコストか**(§4.4)。これで日本語要約工程をgpucallに残すかSonnetに割るかが決まる。最優先で確認。
2. **収縮(C)か白紙書き直し(B)か撤退準備(D)か**。前任推奨=C(削って10k)。だが第二システム症候群に注意(同じ夢から書き直せば同じ肥大に再到達)。
3. **日本語品質の盲検テスト**(§4.5)を実施し、日本語特化オープンモデルをカタログに追加するか判断。
4. **合成入力の契約導出化 + chat×vision対応**(compiler変更を含む唯一の本質的技術修正)を、収縮後に入れるか。

---

## 10. やってはいけないこと

- **帝国を作り直すな。** worker 3,236を統治する機構(conformance層)を足すより、3,220を消す方が正しい。病因は規模。規模を増やすな。
- **conformance統治層を3,236 tupleに被せるな**(`docs/design/conformance-governance.md` はこの誤った方向=patchの設計。反面教師として読め。あれは「conformance層が必要になる時点で元の設計が間違っている」ことの証拠でもある)。
- **ccクラスの品質が要る仕事を gpucall に流すな**(§4)。トリアージを怠って何でもgpucallに流すと品質が如実に低くなる。防波堤はquality_floorの正直さと「ccクラスが要る仕事はgpucallに入れない」という艦隊の鉄則。
- **ツールのsuccess/表示を独立検証せず信じるな。読まずに断言するな。**(§8)

---

## 11. 既存成果物の所在

- 本引継書: `docs/design/HANDOFF.md`
- (反面教師)機能追加=patch設計: `docs/design/conformance-governance.md`(英語、v0.2.0。§9に多角レビュー統合済み)
- 作業規律lesson(英語): `docs/design/lessons-engineering-discipline.md`
- 6ファイル精読記録(file:line): `docs/ARCHITECTURE_NOTES.md`
- 状況記録: `~/.claude/plans/gpucall-conformance-STATUS.md`
- グローバル教訓memory: `~/.claude/auto-memory/feedback_work_discipline_lessons_20260710.md`

---

## 一言で(後継者へ)

**夢は美しかった。だが顧客は最初から一人(news-system)だった。帝国を弔い、「ローカルLLMのGPU要求を、最安の適合レンタルGPUに振って実行し、片付ける」——その骨だけに収縮せよ。ルーティングの頭脳(compiler)は既にある。抜き出して10kに畳め。日本語要約の品質だけは、秘匿かコストかをオーナーに問い、コストなら書く一手をSonnetに割け。そして——読まずに断言するな。ツールのsuccessを信じるな。前任はそれで全部間違えた。**
