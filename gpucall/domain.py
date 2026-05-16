from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import AnyHttpUrl, AnyUrl, BaseModel, ConfigDict, Field, NonNegativeFloat, PositiveInt, model_validator


class ExecutionMode(StrEnum):
    SYNC = "sync"
    ASYNC = "async"
    STREAM = "stream"


class ResponseFormatType(StrEnum):
    TEXT = "text"
    JSON_OBJECT = "json_object"
    JSON_SCHEMA = "json_schema"


class JobState(StrEnum):
    QUEUED = "QUEUED"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    COMPLETED_AFTER_CALLER_TIMEOUT = "COMPLETED_AFTER_CALLER_TIMEOUT"


class DataClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"

    def permits(self, required: "DataClassification") -> bool:
        order = {
            DataClassification.PUBLIC: 0,
            DataClassification.INTERNAL: 1,
            DataClassification.CONFIDENTIAL: 2,
            DataClassification.RESTRICTED: 3,
        }
        return order[self] >= order[required]


class SecurityTier(StrEnum):
    LOCAL = "local"
    SHARED_GPU = "shared_gpu"
    ENCRYPTED_CAPSULE = "encrypted_capsule"
    CONFIDENTIAL_TEE = "confidential_tee"
    SPLIT_LEARNING = "split_learning"


class ExecutionSurface(StrEnum):
    LOCAL_RUNTIME = "local_runtime"
    IAAS_VM = "iaas_vm"
    CONTAINER_INSTANCE = "container_instance"
    MANAGED_ENDPOINT = "managed_endpoint"
    FUNCTION_RUNTIME = "function_runtime"
    SANDBOX_RUNTIME = "sandbox_runtime"
    CLUSTER_RUNTIME = "cluster_runtime"
    LIFECYCLE_ONLY = "lifecycle_only"


class PriceFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class ProviderErrorCode(StrEnum):
    """Provider-side temporary execution failure codes."""

    PROVIDER_RESOURCE_EXHAUSTED = "PROVIDER_RESOURCE_EXHAUSTED"
    PROVIDER_CAPACITY_UNAVAILABLE = "PROVIDER_CAPACITY_UNAVAILABLE"
    PROVIDER_PROVISION_UNAVAILABLE = "PROVIDER_PROVISION_UNAVAILABLE"
    PROVIDER_QUEUE_SATURATED = "PROVIDER_QUEUE_SATURATED"
    PROVIDER_WORKER_INITIALIZING = "PROVIDER_WORKER_INITIALIZING"
    PROVIDER_WORKER_THROTTLED = "PROVIDER_WORKER_THROTTLED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_POLL_TIMEOUT = "PROVIDER_POLL_TIMEOUT"
    PROVIDER_JOB_FAILED = "PROVIDER_JOB_FAILED"
    PROVIDER_CANCELLED = "PROVIDER_CANCELLED"
    PROVIDER_UNHEALTHY = "PROVIDER_UNHEALTHY"
    PROVIDER_BOOTING = "PROVIDER_BOOTING"
    PROVIDER_PREEMPTED = "PROVIDER_PREEMPTED"
    PROVIDER_MAINTENANCE = "PROVIDER_MAINTENANCE"
    PROVIDER_UPSTREAM_UNAVAILABLE = "PROVIDER_UPSTREAM_UNAVAILABLE"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_QUOTA_EXCEEDED = "PROVIDER_QUOTA_EXCEEDED"
    PROVIDER_REGION_UNAVAILABLE = "PROVIDER_REGION_UNAVAILABLE"
    PROVIDER_IMAGE_PULL_DELAY = "PROVIDER_IMAGE_PULL_DELAY"
    PROVIDER_MODEL_LOADING = "PROVIDER_MODEL_LOADING"
    PROVIDER_CONCURRENCY_LIMIT = "PROVIDER_CONCURRENCY_LIMIT"
    PROVIDER_LEASE_EXPIRED = "PROVIDER_LEASE_EXPIRED"
    PROVIDER_STALE_JOB = "PROVIDER_STALE_JOB"
    PROVIDER_ERROR = "PROVIDER_ERROR"


class ApiKeyHandoffMode(StrEnum):
    MANUAL = "manual"
    HANDOFF_FILE = "handoff_file"
    TRUSTED_BOOTSTRAP = "trusted_bootstrap"


class RecipeAdminAutomationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_inbox_auto_materialize: bool = False
    recipe_inbox_auto_validate_existing_tuples: bool = False
    recipe_inbox_auto_activate_existing_validated_recipe: bool = False
    recipe_inbox_auto_promote_candidates: bool = False
    recipe_inbox_auto_billable_validation: bool = False
    recipe_inbox_auto_activate_validated: bool = False
    recipe_inbox_auto_require_auto_select_safe: bool = True
    recipe_inbox_auto_set_auto_select: bool = False
    recipe_inbox_auto_run_validate_config: bool = True
    recipe_inbox_auto_run_launch_check: bool = False
    recipe_inbox_promotion_work_dir: str | None = None
    api_key_handoff_mode: ApiKeyHandoffMode = ApiKeyHandoffMode.MANUAL
    api_key_bootstrap_allowed_cidrs: tuple[str, ...] = ()
    api_key_bootstrap_allowed_hosts: tuple[str, ...] = ()
    api_key_bootstrap_gateway_url: str | None = None
    api_key_bootstrap_recipe_inbox: str | None = None
    onboarding_prompt_url: str | None = None
    onboarding_manual_url: str | None = None
    caller_sdk_wheel_url: str | None = None

    @model_validator(mode="after")
    def validate_admin_automation_chain(self) -> "RecipeAdminAutomationConfig":
        if self.recipe_inbox_auto_promote_candidates and not self.recipe_inbox_auto_materialize:
            raise ValueError("recipe_inbox_auto_promote_candidates requires recipe_inbox_auto_materialize")
        if self.recipe_inbox_auto_validate_existing_tuples and not self.recipe_inbox_auto_materialize:
            raise ValueError("recipe_inbox_auto_validate_existing_tuples requires recipe_inbox_auto_materialize")
        if self.recipe_inbox_auto_activate_existing_validated_recipe and not self.recipe_inbox_auto_validate_existing_tuples:
            raise ValueError("recipe_inbox_auto_activate_existing_validated_recipe requires recipe_inbox_auto_validate_existing_tuples")
        if self.recipe_inbox_auto_billable_validation and not (
            self.recipe_inbox_auto_promote_candidates or self.recipe_inbox_auto_validate_existing_tuples
        ):
            raise ValueError("recipe_inbox_auto_billable_validation requires candidate promotion or existing tuple validation")
        if self.recipe_inbox_auto_activate_validated and not self.recipe_inbox_auto_billable_validation:
            raise ValueError("recipe_inbox_auto_activate_validated requires recipe_inbox_auto_billable_validation")
        if self.recipe_inbox_auto_set_auto_select and not (
            self.recipe_inbox_auto_activate_existing_validated_recipe or self.recipe_inbox_auto_activate_validated
        ):
            raise ValueError("recipe_inbox_auto_set_auto_select requires an auto-activation path")
        if self.recipe_inbox_auto_run_launch_check and not self.recipe_inbox_auto_run_validate_config:
            raise ValueError("recipe_inbox_auto_run_launch_check requires recipe_inbox_auto_run_validate_config")
        return self


class RecipeLatencyClass(StrEnum):
    INTERACTIVE = "interactive"
    STANDARD = "standard"
    BATCH = "batch"
    LONG_RUNNING = "long_running"


class RecipeQualityFloor(StrEnum):
    SMOKE = "smoke"
    DRAFT = "draft"
    STANDARD = "standard"
    HIGH = "high"
    LOSSLESS = "lossless"


class RecipeResourceClass(StrEnum):
    SMOKE = "smoke"
    LIGHT = "light"
    STANDARD = "standard"
    LARGE = "large"
    EXLARGE = "exlarge"
    ULTRALONG = "ultralong"
    DOCUMENT_VISION = "document_vision"


class TupleTrustProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    security_tier: SecurityTier = SecurityTier.SHARED_GPU
    sovereign_jurisdiction: str | None = None
    dedicated_gpu: bool = False
    requires_attestation: bool = False
    supports_key_release: bool = False
    allows_worker_s3_credentials: bool = False
    attestation_type: str | None = None


class SecurityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    restricted_requires: list[SecurityTier] = Field(
        default_factory=lambda: [SecurityTier.CONFIDENTIAL_TEE, SecurityTier.SPLIT_LEARNING]
    )
    confidential_allows_inline: bool = True
    require_data_ref_sha256: bool = True


class AttestationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tuple: str
    gpu_sku: str | None = None
    security_tier: SecurityTier
    confidential_computing_mode: str | None = None
    driver_version: str | None = None
    firmware_version: str | None = None
    worker_image_digest: str | None = None
    nonce: str
    nonce_observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    policy_hash: str
    kms_key_release_decision: Literal["not_required", "approved", "denied"] = "not_required"
    evidence_ref: str | None = None
    verified: bool = False


class KeyReleaseGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key_id: str
    policy_hash: str
    attestation_evidence_ref: str
    recipient: str
    expires_at: datetime


class KeyReleaseRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key_id: str
    policy_hash: str
    required: bool = True
    attestation_required: bool = True
    gateway_may_generate_dek: bool = False


class ArtifactManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    artifact_chain_id: str
    version: str
    classification: DataClassification
    ciphertext_uri: AnyUrl
    ciphertext_sha256: str = Field(min_length=64, max_length=64)
    encryption_nonce: str | None = Field(default=None, min_length=24, max_length=24)
    key_id: str
    producer_plan_hash: str
    attestation_evidence_ref: str | None = None
    parent_artifact_ids: list[str] = Field(default_factory=list)
    legal_hold: bool = False
    retention_until: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ArtifactExportSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_chain_id: str
    version: str
    key_id: str
    parent_artifact_ids: list[str] = Field(default_factory=list)
    retention_until: datetime | None = None
    legal_hold: bool = False

    @model_validator(mode="after")
    def reject_ambiguous_latest(self) -> "ArtifactExportSpec":
        if self.version.strip().lower() == "latest":
            raise ValueError("artifact export version must be explicit; 'latest' is not allowed")
        return self


class SplitLearningSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activation_ref: DataRef
    encoder_hash: str = Field(min_length=64, max_length=64)
    dp_epsilon: NonNegativeFloat | None = None
    dp_delta: NonNegativeFloat | None = None
    irreversibility_claim: Literal["not_claimed", "empirical"] = "not_claimed"


class CompileArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_spec_hash: str
    policy_hash: str
    recipe_hash: str
    tuple_contract_hash: str
    selected_tuple_hash: str | None = None
    selected_tuple: dict[str, Any] | None = None
    governance_hash: str


class DataRef(BaseModel):
    """Reference to sensitive data; payload bytes must not cross the gateway."""

    uri: AnyUrl
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    bytes: PositiveInt | None = None
    expires_at: datetime | None = None
    content_type: str | None = None
    endpoint_url: str | None = None
    region: str | None = None
    gateway_presigned: bool = False


class InlineValue(BaseModel):
    value: str = Field(max_length=8192)
    content_type: str = "text/plain"


class ResponseFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ResponseFormatType = ResponseFormatType.TEXT
    json_schema: dict[str, Any] | None = None
    strict: bool = True

    @model_validator(mode="after")
    def validate_schema_contract(self) -> "ResponseFormat":
        if self.type is ResponseFormatType.JSON_SCHEMA and self.json_schema is None:
            raise ValueError("json_schema is required when response_format.type is json_schema")
        if self.type is not ResponseFormatType.JSON_SCHEMA and self.json_schema is not None:
            raise ValueError("json_schema is only valid when response_format.type is json_schema")
        if self.type is ResponseFormatType.JSON_SCHEMA and isinstance(self.json_schema, dict):
            schema = self.json_schema.get("schema")
            if isinstance(schema, dict):
                strict = self.json_schema.get("strict")
                self.json_schema = schema
                if isinstance(strict, bool):
                    self.strict = strict
        return self


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "developer", "user", "assistant", "tool", "function"] = "user"
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    function_call: dict[str, Any] | None = None
    refusal: str | None = None

    @model_validator(mode="after")
    def validate_openai_message_contract(self) -> "ChatMessage":
        has_content = self.content is not None
        if self.role == "tool":
            if not self.tool_call_id or not has_content:
                raise ValueError("tool messages require content and tool_call_id")
            return self
        if self.role == "function":
            if not self.name or not has_content:
                raise ValueError("function messages require content and name")
            return self
        if self.role == "assistant" and (self.tool_calls or self.function_call or self.refusal is not None):
            return self
        if not has_content:
            raise ValueError("message content is required unless assistant tool_calls/function_call/refusal is present")
        return self


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str
    mode: ExecutionMode
    recipe: str | None = None
    intent: str | None = None
    input_refs: list[DataRef] = Field(default_factory=list)
    inline_inputs: dict[str, InlineValue] = Field(default_factory=dict)
    messages: list[ChatMessage] = Field(default_factory=list)
    requested_tuple: str | None = None
    max_tokens: PositiveInt | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    stop: str | list[str] | None = None
    seed: int | None = None
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    frequency_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    functions: list[dict[str, Any]] | None = None
    function_call: str | dict[str, Any] | None = None
    stream_options: dict[str, Any] | None = None
    n: PositiveInt | None = None
    timeout_seconds: PositiveInt | None = None
    lease_ttl_seconds: PositiveInt | None = None
    response_format: ResponseFormat | None = None
    artifact_export: ArtifactExportSpec | None = None
    split_learning: SplitLearningSpec | None = None
    webhook_url: AnyHttpUrl | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)
    metadata: dict[str, str] = Field(default_factory=dict)
    bypass_circuit_for_validation: bool = False

    @model_validator(mode="after")
    def validate_mode_contract(self) -> "TaskRequest":
        if self.mode is ExecutionMode.ASYNC and self.webhook_url is None:
            return self
        if self.mode is not ExecutionMode.ASYNC and self.webhook_url is not None:
            raise ValueError("webhook_url is only valid for async mode")
        return self


class TuplePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    max_data_classification: DataClassification = DataClassification.CONFIDENTIAL


class CostPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_estimated_cost_usd: NonNegativeFloat | None = None
    max_cold_start_cost_usd: NonNegativeFloat | None = None
    max_idle_cost_usd: NonNegativeFloat | None = None
    require_budget_for_high_cost_tuple: bool | None = None
    high_cost_threshold_usd: NonNegativeFloat | None = None
    require_fresh_price_for_budget: bool | None = None


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    inline_bytes_limit: PositiveInt
    default_lease_ttl_seconds: PositiveInt
    max_lease_ttl_seconds: PositiveInt
    max_timeout_seconds: PositiveInt
    tokenizer_safety_multiplier: NonNegativeFloat = 1.25
    tuples: TuplePolicy
    cost_policy: CostPolicy = Field(default_factory=CostPolicy)
    security: SecurityPolicy = Field(default_factory=SecurityPolicy)
    immutable_audit: bool = True


class Recipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    task: str
    recipe_schema_version: PositiveInt = 3
    intent: str | None = None
    context_budget_tokens: PositiveInt | None = None
    resource_class: RecipeResourceClass | None = None
    latency_class: RecipeLatencyClass = RecipeLatencyClass.STANDARD
    quality_floor: RecipeQualityFloor = RecipeQualityFloor.STANDARD
    cost_ceiling_usd: NonNegativeFloat | None = None
    auto_select: bool = True
    data_classification: DataClassification = DataClassification.CONFIDENTIAL
    allowed_modes: list[ExecutionMode]
    timeout_seconds: PositiveInt
    lease_ttl_seconds: PositiveInt
    token_estimation_profile: str = "generic_utf8"
    max_input_bytes: PositiveInt | None = None
    allowed_mime_prefixes: list[str] = Field(default_factory=list)
    allowed_inline_mime_prefixes: list[str] = Field(default_factory=list)
    default_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    structured_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    system_prompt: str | None = None
    structured_system_prompt: str | None = None
    stop_tokens: list[str] = Field(default_factory=list)
    repetition_penalty: NonNegativeFloat | None = None
    guided_decoding: bool = False
    output_validation_attempts: PositiveInt = 1
    artifact_export: bool = False
    requires_key_release: bool = False
    required_model_capabilities: list[str] = Field(default_factory=list)
    output_contract: str | None = None
    expected_cold_start_seconds: PositiveInt | None = None
    cost_policy: CostPolicy | None = None

    @model_validator(mode="before")
    @classmethod
    def derive_internal_contract(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        schema_version = int(payload.get("recipe_schema_version") or 1)
        if "tokenizer_family" in payload and "token_estimation_profile" not in payload:
            payload["token_estimation_profile"] = payload.pop("tokenizer_family")
        old_resource_fields = {"gpu", "min_vram_gb", "max_model_len"}
        if schema_version >= 3:
            present = old_resource_fields & set(payload)
            if present:
                raise ValueError("recipe_schema_version=3 must not declare tuple resource fields: " + ", ".join(sorted(present)))
        elif old_resource_fields & set(payload):
            if "context_budget_tokens" not in payload and "max_model_len" in payload:
                payload["context_budget_tokens"] = payload["max_model_len"]
            for field in old_resource_fields:
                payload.pop(field, None)
        context_budget = int(payload.get("context_budget_tokens") or _context_budget_for(payload))
        if "max_input_bytes" not in payload:
            payload["max_input_bytes"] = _max_input_bytes_for(payload, context_budget)
        if "timeout_seconds" not in payload:
            payload["timeout_seconds"] = _timeout_for_recipe(payload, context_budget)
        if "lease_ttl_seconds" not in payload:
            payload["lease_ttl_seconds"] = _lease_for_recipe(payload, context_budget)
        return payload


class DerivedRecipeRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context_budget_tokens: PositiveInt
    minimum_vram_gb: PositiveInt
    max_input_bytes: PositiveInt
    timeout_seconds: PositiveInt
    lease_ttl_seconds: PositiveInt


def recipe_requirements(recipe: Recipe) -> DerivedRecipeRequirements:
    context_budget = int(recipe.context_budget_tokens or _context_budget_for(recipe.model_dump(mode="json")))
    return DerivedRecipeRequirements(
        context_budget_tokens=context_budget,
        minimum_vram_gb=_vram_for_recipe(recipe.model_dump(mode="json"), context_budget),
        max_input_bytes=int(recipe.max_input_bytes or _max_input_bytes_for(recipe.model_dump(mode="json"), context_budget)),
        timeout_seconds=int(recipe.timeout_seconds or _timeout_for_recipe(recipe.model_dump(mode="json"), context_budget)),
        lease_ttl_seconds=int(recipe.lease_ttl_seconds or _lease_for_recipe(recipe.model_dump(mode="json"), context_budget)),
    )


InputContract = Literal["text", "chat_messages", "data_refs", "image", "audio", "document", "activation_refs", "artifact_refs"]


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    provider_model_id: str
    capabilities: list[str] = Field(default_factory=list)
    max_model_len: PositiveInt
    min_vram_gb: PositiveInt
    supported_engines: list[str] = Field(default_factory=list)
    input_contracts: list[InputContract] = Field(default_factory=list)
    output_contracts: list[str] = Field(default_factory=list)
    supports_vision: bool = False
    supports_guided_decoding: bool = False
    supports_streaming: bool = False
    trust_remote_code: bool = False
    source: str | None = None
    evidence: list[str] = Field(default_factory=list)
    production_ready: bool = False


class EngineSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: str
    version: str | None = None
    input_contracts: list[InputContract] = Field(default_factory=list)
    output_contracts: list[str] = Field(default_factory=list)
    supports_guided_decoding: bool = False
    supports_streaming: bool = False
    supports_multimodal: bool = False
    supports_data_refs: bool = False
    official_doc_refs: list[str] = Field(default_factory=list)
    production_ready: bool = False

    @property
    def supports_multimedia(self) -> bool:
        return self.supports_multimodal


class ControlledRuntimeRoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    preference: Literal["prefer_when_eligible", "neutral", "last_resort"] = "prefer_when_eligible"
    allowed_tasks: list[str] = Field(default_factory=list)
    allowed_modes: list[ExecutionMode] = Field(default_factory=lambda: [ExecutionMode.ASYNC])
    require_validation_evidence: bool = True


class ControlledRuntimeHealthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_url: AnyHttpUrl | None = None
    timeout_seconds: PositiveInt = 2
    failure_policy: Literal["disable_runtime", "warn_only"] = "disable_runtime"


class ControlledRuntimeDiscoveryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["manual", "localhost_probe", "operator_declared_private_network"] = "manual"
    last_verified_at: datetime | None = None


class ControlledRuntimeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["controlled_runtime"] = "controlled_runtime"
    runtime_boundary: Literal["gateway_host", "private_network", "site_network"]
    network_scope: Literal["localhost", "lan", "vpn", "tailscale", "private_subnet", "manual"]
    operator_controlled: bool
    endpoint: AnyHttpUrl | None = None
    adapter: str | None = None
    model: str | None = None
    max_model_len: PositiveInt | None = None
    input_contracts: list[InputContract] = Field(default_factory=list)
    max_data_classification: DataClassification = DataClassification.CONFIDENTIAL
    trust_profile: TupleTrustProfile = Field(default_factory=TupleTrustProfile)
    routing: ControlledRuntimeRoutingConfig = Field(default_factory=ControlledRuntimeRoutingConfig)
    health: ControlledRuntimeHealthConfig = Field(default_factory=ControlledRuntimeHealthConfig)
    discovery: ControlledRuntimeDiscoveryConfig = Field(default_factory=ControlledRuntimeDiscoveryConfig)


class ExecutionTupleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    account_ref: str | None = None
    adapter: str = "echo"
    execution_surface: ExecutionSurface | None = None
    max_data_classification: DataClassification = DataClassification.CONFIDENTIAL
    trust_profile: TupleTrustProfile = Field(default_factory=TupleTrustProfile)
    gpu: str
    vram_gb: PositiveInt
    max_model_len: PositiveInt
    cost_per_second: NonNegativeFloat
    configured_price_source: str | None = None
    configured_price_observed_at: str | None = None
    configured_price_ttl_seconds: NonNegativeFloat | None = None
    expected_cold_start_seconds: PositiveInt | None = None
    scaledown_window_seconds: NonNegativeFloat | None = None
    min_billable_seconds: NonNegativeFloat | None = None
    billing_granularity_seconds: NonNegativeFloat | None = None
    standing_cost_per_second: NonNegativeFloat | None = None
    standing_cost_window_seconds: NonNegativeFloat | None = None
    endpoint_cost_per_second: NonNegativeFloat | None = None
    endpoint_cost_window_seconds: NonNegativeFloat | None = None
    modes: list[ExecutionMode] = Field(default_factory=lambda: [ExecutionMode.ASYNC])
    endpoint: AnyHttpUrl | None = None
    project_id: str | None = None
    region: str | None = None
    zone: str | None = None
    resource_group: str | None = None
    network: str | None = None
    subnet: str | None = None
    service_account: str | None = None
    provider_params: dict[str, Any] = Field(default_factory=dict)
    target: str | None = None
    stream_target: str | None = None
    endpoint_contract: str | None = None
    input_contracts: list[Literal["text", "chat_messages", "data_refs", "image", "audio", "document", "activation_refs", "artifact_refs"]] = Field(
        default_factory=list
    )
    output_contract: str | None = None
    stream_contract: Literal["none", "token-incremental", "sse"] = "none"
    supports_vision: bool = False
    model: str | None = None
    instance: str | None = None
    image: str | None = None
    key_name: str | None = None
    lease_manifest_path: str | None = None
    ssh_remote_cidr: str | None = None
    declared_model_max_len: PositiveInt | None = None
    model_ref: str | None = None
    engine_ref: str | None = None
    controlled_runtime_ref: str | None = None


class ObjectStoreConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tuple: str = "s3"
    bucket: str
    region: str | None = None
    endpoint: AnyHttpUrl | None = None
    prefix: str = "gpucall"
    presign_ttl_seconds: PositiveInt = 900

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_provider_key(cls, data: Any) -> Any:
        if isinstance(data, dict) and "tuple" not in data and "provider" in data:
            payload = dict(data)
            payload["tuple"] = payload.pop("provider")
            return payload
        return data


class TenantSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    requests_per_minute: PositiveInt | None = None
    daily_budget_usd: NonNegativeFloat | None = None
    monthly_budget_usd: NonNegativeFloat | None = None
    max_request_estimated_cost_usd: NonNegativeFloat | None = None
    object_prefix: str | None = None


class PresignPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    bytes: PositiveInt
    sha256: str = Field(min_length=64, max_length=64)
    content_type: str = "application/octet-stream"


class PresignPutResponse(BaseModel):
    upload_url: AnyHttpUrl
    method: str = "PUT"
    data_ref: DataRef


class PresignGetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_ref: DataRef


class PresignGetResponse(BaseModel):
    download_url: AnyHttpUrl
    method: str = "GET"
    data_ref: DataRef


class TupleObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tuple: str
    latency_ms: NonNegativeFloat
    success: bool
    cost: NonNegativeFloat
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_registry_payload(cls, data: Any) -> Any:
        if isinstance(data, dict) and "tuple" not in data and "provider" in data:
            data = dict(data)
            data["tuple"] = data.pop("provider")
        return data


class TupleScore(BaseModel):
    success_rate: float = 1.0
    p50_latency_ms: float | None = None
    cost_per_success: float | None = None
    samples: int = 0


class CompiledPlan(BaseModel):
    plan_id: str = Field(default_factory=lambda: uuid4().hex)
    policy_version: str
    recipe_name: str
    task: str
    mode: ExecutionMode
    data_classification: DataClassification = DataClassification.CONFIDENTIAL
    tuple_chain: list[str]
    timeout_seconds: PositiveInt
    lease_ttl_seconds: PositiveInt
    token_estimation_profile: str
    token_budget: PositiveInt | None
    max_tokens: PositiveInt | None = None
    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    functions: list[dict[str, Any]] | None = None
    function_call: str | dict[str, Any] | None = None
    stream_options: dict[str, Any] | None = None
    n: PositiveInt | None = None
    input_refs: list[DataRef]
    inline_inputs: dict[str, InlineValue]
    messages: list[ChatMessage] = Field(default_factory=list)
    response_format: ResponseFormat | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    artifact_export: ArtifactExportSpec | None = None
    split_learning: SplitLearningSpec | None = None
    system_prompt: str | None = None
    stop_tokens: list[str] = Field(default_factory=list)
    repetition_penalty: NonNegativeFloat | None = None
    guided_decoding: bool = False
    output_validation_attempts: PositiveInt = 1
    attestations: dict[str, Any] = Field(default_factory=dict)

    def expires_at(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=self.lease_ttl_seconds)


class TupleError(Exception):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int = 502,
        code: str | ProviderErrorCode | None = None,
        raw_output: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.code = str(code) if code is not None else None
        self.raw_output = raw_output


TupleResultKind = Literal["inline", "ref", "artifact_manifest"]


class TupleResult(BaseModel):
    kind: TupleResultKind
    value: str | None = None
    ref: DataRef | None = None
    artifact_manifest: ArtifactManifest | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    output_validated: bool | None = None
    tool_calls: list[dict[str, Any]] | None = None
    function_call: dict[str, Any] | None = None
    finish_reason: str | None = None
    refusal: str | None = None
    openai_choices: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def validate_result_payload(self) -> "TupleResult":
        if self.kind == "ref" and self.ref is None:
            raise ValueError("ref result requires ref")
        if self.kind == "artifact_manifest" and self.artifact_manifest is None:
            raise ValueError("artifact_manifest result requires artifact_manifest")
        if (
            self.kind == "inline"
            and self.value is None
            and not self.tool_calls
            and self.function_call is None
            and self.refusal is None
            and not self.openai_choices
        ):
            raise ValueError("inline result requires value, tool_calls, function_call, refusal, or openai_choices")
        return self


def _context_budget_for(payload: dict[str, Any]) -> int:
    resource_class = str(payload.get("resource_class") or "")
    return {
        "smoke": 32768,
        "light": 8192,
        "standard": 32768,
        "large": 65536,
        "exlarge": 131072,
        "ultralong": 524288,
        "document_vision": 8192,
    }.get(resource_class, 8192)


def _vram_for_recipe(payload: dict[str, Any], context_budget: int) -> int:
    resource_class = str(payload.get("resource_class") or "")
    if context_budget > 524288:
        return 320
    if resource_class == "smoke":
        return 1
    if resource_class == "light":
        return 16
    if resource_class == "document_vision":
        return 48
    if resource_class == "standard":
        return 24
    if resource_class in {"large", "exlarge", "ultralong"}:
        return 80
    if context_budget > 32768:
        return 80
    if context_budget > 8192:
        return 24
    return 16


def _max_input_bytes_for(payload: dict[str, Any], context_budget: int) -> int:
    if str(payload.get("task") or "") == "vision":
        return 16 * 1024 * 1024
    return max(1024 * 1024, min(1024 * 1024 * 1024, context_budget * 1024))


def _timeout_for_recipe(payload: dict[str, Any], context_budget: int) -> int:
    latency_class = str(payload.get("latency_class") or "standard")
    if latency_class == "long_running":
        return 1800
    if latency_class == "batch":
        return 900
    if context_budget >= 131072:
        return 600
    return 180


def _lease_for_recipe(payload: dict[str, Any], context_budget: int) -> int:
    timeout = _timeout_for_recipe(payload, context_budget)
    return max(timeout + 60, 240)


class JobRecord(BaseModel):
    job_id: str
    state: JobState
    plan: CompiledPlan
    owner_identity: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    result_ref: DataRef | None = None
    result: TupleResult | None = None
    error: str | None = None
    provider_error_code: ProviderErrorCode | None = None
