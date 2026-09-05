"""Wire contracts. Money is integer micro-USD; time is integer Unix milliseconds."""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator

Natural = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]
Positive = Annotated[int, Field(strict=True, gt=0, le=2**53 - 1)]
Name = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def canonical(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Input(Record):
    sha256: Digest
    media_type: Literal["text/plain", "image/png", "image/jpeg"]
    size: Positive


class Request(Record):
    key: Name
    profile: Name
    instruction: str = Field(min_length=1, max_length=32768)
    inputs: tuple[Input, ...] = Field(default=(), max_length=16)
    deadline_ms: Positive
    budget_microusd: Positive
    max_attempts: int = Field(default=1, strict=True, ge=1, le=5)
    privacy: Literal["public", "trusted_provider", "attested"] = "trusted_provider"
    # 'reserved' explicitly authorizes bounded reservations, not an absolute invoice guarantee.
    budget_guarantee: Literal["hard", "reserved"] = "hard"


class Profile(Record):
    id: Name
    task: Literal["infer", "vision"]
    revision: Positive
    max_tokens: int = Field(default=512, strict=True, ge=1, le=32768)
    max_input_bytes: int = Field(default=16*1024*1024, strict=True, ge=1, le=16*1024*1024)
    max_output_bytes: int = Field(default=1024*1024, strict=True, ge=1, le=1024*1024)
    min_output_chars: Natural = 1
    required_literals: tuple[str, ...] = ()
    output_schema: dict | None = None

    @model_validator(mode="after")
    def valid_schema(self):
        if self.output_schema is not None:
            Draft202012Validator.check_schema(self.output_schema)
            # No remote schema retrieval and no network-dependent validation.
            def walk(node):
                if isinstance(node, dict):
                    for key, value in node.items():
                        if key in {"$ref", "$dynamicRef"} and not str(value).startswith("#"):
                            raise ValueError("only local JSON schema references are permitted")
                        walk(value)
                elif isinstance(node, list):
                    for value in node:
                        walk(value)
            walk(self.output_schema)
        return self


class Execution(Record):
    id: Name
    adapter: Literal["fixture", "modal"]
    tasks: tuple[Literal["infer", "vision"], ...]
    model: str = Field(min_length=1, max_length=256)
    model_revision: str = Field(min_length=1, max_length=128)
    # Modal image ID, built independently of the execution path, includes worker + weights.
    image: str = Field(min_length=1, max_length=256)
    gpu: str = Field(min_length=1, max_length=64)
    region: Name
    trusted: bool = Field(default=False, strict=True)
    app: Name = "gpucall-next"


class Offer(Record):
    execution: Execution
    observed_ms: Positive
    expires_ms: Positive
    ready: bool = Field(strict=True)
    # Aggregate compute price, including CPU and memory. No hidden component prices.
    rate_microusd_per_second: Natural
    fixed_microusd: Natural = 0
    expected_ms: Positive
    maximum_ms: Positive
    stop_grace_ms: Natural = 0
    hard_bound: bool = Field(default=False, strict=True)
    price_source: str = Field(default="fixture", min_length=1, max_length=2048)

    @model_validator(mode="after")
    def bounds(self):
        if self.expires_ms <= self.observed_ms or self.expected_ms > self.maximum_ms:
            raise ValueError("invalid observation or runtime interval")
        if self.execution.adapter == "modal" and self.hard_bound:
            raise ValueError("Modal adapter does not certify absolute invoice bounds")
        if self.execution.adapter == "modal":
            if self.maximum_ms % 1000:
                raise ValueError("Modal lifetime must be whole seconds for budget accounting")
            if not self.price_source.startswith("https://"):
                raise ValueError("Modal price requires an explicit source")
        return self


class Evidence(Record):
    execution_hash: Digest
    profile_hash: Digest
    tested_ms: Positive
    expires_ms: Positive
    passed: bool = Field(strict=True)
    artifact_hash: Digest


class Snapshot(Record):
    at_ms: Positive
    offers: tuple[Offer, ...]
    evidence: tuple[Evidence, ...]

    @model_validator(mode="after")
    def unique(self):
        ids = [o.execution.id for o in self.offers]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate execution IDs")
        return self


class AttemptPlan(Record):
    offer: Offer
    evidence: Evidence | None
    expected_microusd: Natural
    reserved_microusd: Natural


class Plan(Record):
    request_hash: Digest
    profile_hash: Digest
    snapshot_hash: Digest
    attempts: tuple[AttemptPlan, ...]
    reservation_microusd: Natural
    rejections: dict[str, tuple[str, ...]]
    validation_execution: Name | None = None


class Bundle(Record):
    profiles: tuple[Profile, ...]
    snapshot: Snapshot
    account_limit_microusd: Natural
    allow_fixture: bool = Field(default=False, strict=True)
    max_running: int = Field(default=4, strict=True, ge=1, le=64)

    @model_validator(mode="after")
    def unique(self):
        ids = [p.id for p in self.profiles]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate profile IDs")
        return self


class ProviderResult(Record):
    state: Literal["pending", "succeeded", "failed"]
    output: str | None = None
    error: Literal["capacity", "unavailable", "timeout", "invalid_input", "worker_failed", "lost"] | None = None
    actual_microusd: Natural | None = None


class Cleanup(Record):
    stopped: bool = Field(strict=True)
    proof: Literal["fixture_absent", "provider_terminated", "unconfirmed"]
