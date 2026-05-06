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
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


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


class ProviderTrustProfile(BaseModel):
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

    provider: str
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
    provider_contract_hash: str
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
        return self


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"] = "user"
    content: str


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str
    mode: ExecutionMode
    recipe: str | None = None
    input_refs: list[DataRef] = Field(default_factory=list)
    inline_inputs: dict[str, InlineValue] = Field(default_factory=dict)
    messages: list[ChatMessage] = Field(default_factory=list)
    requested_provider: str | None = None
    requested_gpu: str | None = None
    max_tokens: PositiveInt | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    timeout_seconds: PositiveInt | None = None
    lease_ttl_seconds: PositiveInt | None = None
    response_format: ResponseFormat | None = None
    artifact_export: ArtifactExportSpec | None = None
    split_learning: SplitLearningSpec | None = None
    webhook_url: AnyHttpUrl | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_mode_contract(self) -> "TaskRequest":
        if self.mode is ExecutionMode.ASYNC and self.webhook_url is None:
            return self
        if self.mode is not ExecutionMode.ASYNC and self.webhook_url is not None:
            raise ValueError("webhook_url is only valid for async mode")
        return self


class ProviderPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    max_data_classification: DataClassification = DataClassification.CONFIDENTIAL


class CostPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_estimated_cost_usd: NonNegativeFloat | None = None
    max_cold_start_cost_usd: NonNegativeFloat | None = None
    max_idle_cost_usd: NonNegativeFloat | None = None
    require_budget_for_high_cost_provider: bool | None = None
    high_cost_threshold_usd: NonNegativeFloat | None = None


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    inline_bytes_limit: PositiveInt
    default_lease_ttl_seconds: PositiveInt
    max_lease_ttl_seconds: PositiveInt
    max_timeout_seconds: PositiveInt
    tokenizer_safety_multiplier: NonNegativeFloat = 1.25
    providers: ProviderPolicy
    cost_policy: CostPolicy = Field(default_factory=CostPolicy)
    security: SecurityPolicy = Field(default_factory=SecurityPolicy)
    immutable_audit: bool = True


class Recipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    task: str
    recipe_schema_version: PositiveInt = 1
    intent: str | None = None
    context_budget_tokens: PositiveInt | None = None
    resource_class: RecipeResourceClass | None = None
    latency_class: RecipeLatencyClass = RecipeLatencyClass.STANDARD
    quality_floor: RecipeQualityFloor = RecipeQualityFloor.STANDARD
    cost_ceiling_usd: NonNegativeFloat | None = None
    auto_select: bool = True
    data_classification: DataClassification = DataClassification.CONFIDENTIAL
    allowed_modes: list[ExecutionMode]
    min_vram_gb: PositiveInt
    max_model_len: PositiveInt
    timeout_seconds: PositiveInt
    lease_ttl_seconds: PositiveInt
    tokenizer_family: str
    gpu: str | None = None
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
        old_resource_fields = {"gpu", "min_vram_gb", "max_model_len"}
        if schema_version >= 2:
            present = old_resource_fields & set(payload)
            if present:
                raise ValueError("recipe_schema_version=2 must not declare provider resource fields: " + ", ".join(sorted(present)))
            context_budget = int(payload.get("context_budget_tokens") or _context_budget_for(payload))
            payload["max_model_len"] = context_budget
            payload["min_vram_gb"] = _vram_for_recipe(payload, context_budget)
            payload["gpu"] = None
            if "max_input_bytes" not in payload:
                payload["max_input_bytes"] = _max_input_bytes_for(payload, context_budget)
            if "timeout_seconds" not in payload:
                payload["timeout_seconds"] = _timeout_for_recipe(payload, context_budget)
            if "lease_ttl_seconds" not in payload:
                payload["lease_ttl_seconds"] = _lease_for_recipe(payload, context_budget)
        return payload


InputContract = Literal["text", "chat_messages", "data_refs", "image", "activation_refs", "artifact_refs"]


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


class ProviderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    adapter: str = "echo"
    execution_surface: ExecutionSurface | None = None
    max_data_classification: DataClassification = DataClassification.CONFIDENTIAL
    trust_profile: ProviderTrustProfile = Field(default_factory=ProviderTrustProfile)
    gpu: str
    vram_gb: PositiveInt
    max_model_len: PositiveInt
    cost_per_second: NonNegativeFloat
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
    input_contracts: list[Literal["text", "chat_messages", "data_refs", "image", "activation_refs", "artifact_refs"]] = Field(
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


class ObjectStoreConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "s3"
    bucket: str
    region: str | None = None
    endpoint: AnyHttpUrl | None = None
    prefix: str = "gpucall"
    presign_ttl_seconds: PositiveInt = 900


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


class ProviderObservation(BaseModel):
    provider: str
    latency_ms: NonNegativeFloat
    success: bool
    cost: NonNegativeFloat
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ProviderScore(BaseModel):
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
    provider_chain: list[str]
    timeout_seconds: PositiveInt
    lease_ttl_seconds: PositiveInt
    tokenizer_family: str
    token_budget: PositiveInt | None
    max_tokens: PositiveInt | None = None
    temperature: float | None = None
    input_refs: list[DataRef]
    inline_inputs: dict[str, InlineValue]
    messages: list[ChatMessage] = Field(default_factory=list)
    response_format: ResponseFormat | None = None
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


class ProviderError(Exception):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int = 502,
        code: str | None = None,
        raw_output: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.code = code
        self.raw_output = raw_output


ProviderResultKind = Literal["inline", "ref", "artifact_manifest"]


class ProviderResult(BaseModel):
    kind: ProviderResultKind
    value: str | None = None
    ref: DataRef | None = None
    artifact_manifest: ArtifactManifest | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    output_validated: bool | None = None


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
    if resource_class == "smoke":
        return 1
    if resource_class == "light":
        return 16
    if resource_class in {"standard", "document_vision"}:
        return 80 if resource_class == "document_vision" else 24
    if resource_class in {"large", "exlarge", "ultralong"}:
        return 80
    if context_budget > 524288:
        return 320
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
    result: ProviderResult | None = None
    error: str | None = None
