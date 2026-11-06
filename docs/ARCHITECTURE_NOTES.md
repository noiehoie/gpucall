# gpucall アーキテクチャ精読ノート

目的: gpucall 全ファイルを1行ずつ自分で読み、全体構造を正確に記録する。断片的つまみ読みによる誤認を根絶するため。
方法: 依存の下層から順に Read で全行読み、1ファイルごとに理解を追記する。

凡例: ⚠️=過去に自分が誤認していた点の訂正 / ★=設計(conformance改修)に直結する事実。

---

## gpucall/domain.py （1030行, 全データモデル。読了）

pydantic BaseModel 群と enum 群。gpucall の全型の定義元。ほぼ全モデルが `model_config = ConfigDict(extra="forbid")`。

### enum
- `ExecutionMode`: sync/async/stream。
- `ResponseFormatType`: text/json_object/json_schema。
- `JobState`: QUEUED..EXPIRED, COMPLETED_AFTER_CALLER_TIMEOUT。
- `DataClassification`: public<internal<confidential<restricted（`.permits()` で順序比較）。
- `SecurityTier`: local/shared_gpu/encrypted_capsule/confidential_tee/split_learning。
- `ExecutionSurface`: local_runtime/iaas_vm/container_instance/managed_endpoint/function_runtime/sandbox_runtime/cluster_runtime/lifecycle_only。
- `PriceFreshness`: fresh/stale/unknown。
- ★ `ProviderErrorCode`(76-102): **provider 側の一時的失敗コード23種**（PROVIDER_TIMEOUT, PROVIDER_RATE_LIMITED, PROVIDER_MODEL_LOADING, PROVIDER_BOOTING, PROVIDER_CAPACITY_UNAVAILABLE 等）。**柱E(構造的 vs 一時的の分類)はこの enum を「一時的」の定義として使える**。
- `RecipeLatencyClass`/`RecipeQualityFloor`/`RecipeResourceClass`。

### 主要モデル
- ★ `ModelSpec`(614-631): **pydantic BaseModel, `extra="forbid"`**。⚠️ 過去に「frozen dataclass」と繰り返し報告したのは**誤り**。フィールド: name, `provider_model_id`(HF ID), `capabilities: list[str]`, max_model_len, min_vram_gb, supported_engines, `input_contracts: list[InputContract]`, output_contracts, `supports_vision`, supports_guided_decoding, supports_streaming, trust_remote_code, source, evidence, production_ready。**`input_requirements`(最小画像寸法等)は存在しない → 柱Aで新設が要る**。⚠️ `extra="forbid"` なので yml に未定義キーを足すと即エラー。柱Aは ModelSpec にフィールドを正式追加する必要（「default付きで後方互換」は正しいが「dataclassだから」という過去の根拠は虚偽）。
- ★ `InputContract`(611): `Literal["text","chat_messages","data_refs","image","audio","document","activation_refs","artifact_refs"]`。
- ★ `ExecutionTupleSpec`(702-759): tuple の実体。**`input_contracts` フィールドを持つ**(744)。⚠️ 過去「input_contractsはadapter descriptor由来でconfig/tupleに無い」と報告したが、tuple spec 自体に input_contracts がある。他に adapter, execution_surface, gpu, vram_gb, max_model_len, cost_per_second, 価格鮮度3点(configured_price_source/observed_at/ttl), modes, endpoint_contract, output_contract, stream_contract, supports_vision, `model_ref`/`engine_ref`/`controlled_runtime_ref`(他specへの参照)。config/tuples/ が空なのは、tuple がこの spec として別経路(catalog/materialize)で構成されるため（要確認: 次以降のファイル）。
- ★ `ModelSpec`/`EngineSpec`/`ExecutionTupleSpec` の3箇所に `input_contracts` がある。契約の source は1つでない。
- `Recipe`(522-587): task, `required_model_capabilities: list[str]`(555, capability要求側), allowed_modes, timeout/lease, quality_floor, resource_class, guided_decoding, output_contract, allowed_mime_prefixes 等。`derive_internal_contract`(561, before validator): schema_version>=3 は gpu/min_vram_gb/max_model_len を宣言禁止（tuple resource は recipe に書かない設計）。context_budget/timeout/lease を派生。
- `TaskRequest`(379-419): 入口の request。task, mode, recipe, `input_refs: list[DataRef]`, `inline_inputs: dict[str,InlineValue]`, `messages: list[ChatMessage]`, requested_tuple, response_format, `bypass_circuit_for_validation`(検証smokeで使う), 各サンプリング。⚠️ messages/inline_inputs/input_refs は排他でなくフィールドとして共存可（排他検証は compiler 側）。
- `CompiledPlan`(846-885): compile 結果。`tuple_chain: list[str]`(fallback含む), token_budget, messages/inline/refs を保持。
- ★ `TupleError`(888-903): tuple 実行の例外。`retryable: bool`, `code`, `status_code`, `raw_output`。**柱Eの失敗分類は retryable と ProviderErrorCode で既に部分表現されている**。
- ★ `RecipeAdminAutomationConfig`(111-163): watch/自動化の設定。`recipe_inbox_auto_validation_budget_usd=0.10`(121, **柱D対象の固定予算**), `recipe_inbox_auto_validation_max_attempts=3`(123), 自動化チェーンの依存を model_validator で検証。
- ★ `PanopticonRemediationPolicy`(454-468): `exclude_from_routing`(mode="auto")等。**routing除外の remediation が既存**。柱E(quarantine=route除外)は既存の exclude_from_routing 機構と統合すべき（新規発明でなく）。
- `Policy`(504-519): 全ポリシーの束。tuples(allow/deny), cost_policy, security, panopticon_remediation, provider_supply_provisioning, inline_bytes_limit, lease/timeout 上限。
- `ControlledRuntimeSpec`(682-699): operator制御のローカル/private runtime（controlled_runtime adapter）。`require_validation_evidence=True`(664)。
- artifact/attestation/key-release 系: ArtifactManifest, ArtifactExportSpec, AttestationEvidence, KeyReleaseGrant/Requirement, SplitLearningSpec（主権・機密計算）。
- DataRef(309): 機密データ参照。payload は gateway を通さない設計（uri+sha256+bytes+presign）。
- 派生関数(963-1016): `_context_budget_for`(resource_class→token予算), `_vram_for_recipe`, `_max_input_bytes_for`(vision=16MiB固定), `_timeout_for_recipe`, `_lease_for_recipe`。

### 設計への含意（このファイルから）
- 柱A: ModelSpec(pydantic, extra=forbid)に `input_requirements` を正式フィールドとして追加。
- 柱E: 失敗分類は `ProviderErrorCode`(一時的23種)と `TupleError.retryable` を土台にできる。quarantine は `PanopticonRemediationPolicy.exclude_from_routing`(既存)と統合。
- 入力契約(input_contracts)は ModelSpec/EngineSpec/ExecutionTupleSpec の3箇所にある。柱B/C-1はどれを正とするか要整理。

### 次に読む
config.py（Config構築とconfig_hash）、catalog.py（models/workers/recipesロード）で「tupleがどう構成されるか」を確定する。
