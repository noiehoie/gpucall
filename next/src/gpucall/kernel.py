"""Pure decisions: no clock, filesystem, network, randomness, environment or LLM."""
from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from .models import AttemptPlan, Plan, Profile, Request, Snapshot, digest

KERNEL_VERSION = 1


class Rejected(ValueError):
    def __init__(self, code: str, details: dict | None = None):
        super().__init__(code)
        self.code = code
        self.details = details or {}


def cost(rate: int, milliseconds: int, fixed: int) -> int:
    return fixed + (rate * milliseconds + 999) // 1000


def compile_plan(request: Request, profile: Profile, snapshot: Snapshot, *, allow_fixture: bool = False,
                 validation_execution: str | None = None) -> Plan:
    if validation_execution and (request.privacy != "public" or request.max_attempts != 1):
        raise Rejected("validation_requires_public_single_attempt")
    if request.profile != profile.id:
        raise Rejected("profile_mismatch")
    if request.deadline_ms <= snapshot.at_ms:
        raise Rejected("deadline_expired")
    if len(request.instruction.encode()) + sum(i.size for i in request.inputs) > profile.max_input_bytes:
        raise Rejected("input_too_large")
    images = [i for i in request.inputs if i.media_type.startswith("image/")]
    if profile.task == "vision" and not images:
        raise Rejected("image_required")
    if profile.task == "infer" and images:
        raise Rejected("unexpected_image")
    candidates = []
    rejected = {}
    for offer in sorted(snapshot.offers, key=lambda o: o.execution.id):
        execution = offer.execution
        if validation_execution and execution.id != validation_execution:
            continue
        reasons = []
        if execution.adapter == "fixture" and not allow_fixture:
            reasons.append("fixture_disabled")
        if not offer.observed_ms <= snapshot.at_ms < offer.expires_ms:
            reasons.append("price_not_fresh")
        if not offer.ready:
            reasons.append("not_ready")
        if profile.task not in execution.tasks:
            reasons.append("task_unsupported")
        if request.privacy == "attested":
            reasons.append("attestation_not_implemented")
        elif request.privacy == "trusted_provider" and not execution.trusted:
            reasons.append("provider_not_trusted")
        if request.budget_guarantee == "hard" and not offer.hard_bound:
            reasons.append("hard_budget_unavailable")
        maximum = offer.maximum_ms + offer.stop_grace_ms
        if snapshot.at_ms + maximum > request.deadline_ms:
            reasons.append("deadline_unachievable")
        reserve = cost(offer.rate_microusd_per_second, maximum, offer.fixed_microusd)
        if reserve > request.budget_microusd:
            reasons.append("budget_exceeded")
        matching = [e for e in snapshot.evidence
                    if e.execution_hash == digest(execution) and e.profile_hash == digest(profile)
                    and e.tested_ms <= snapshot.at_ms]
        # Latest evidence wins, including failures. A tie deterministically prefers failure.
        latest = max(matching, key=lambda e: (e.tested_ms, not e.passed, digest(e)), default=None)
        if not validation_execution and (latest is None or not latest.passed or snapshot.at_ms >= latest.expires_ms):
            reasons.append("validation_missing_or_failed")
        if reasons:
            rejected[execution.id] = tuple(reasons)
        else:
            candidates.append(AttemptPlan(offer=offer, evidence=None if validation_execution else latest,
                expected_microusd=cost(offer.rate_microusd_per_second, offer.expected_ms, offer.fixed_microusd),
                reserved_microusd=reserve))
    if not candidates:
        raise Rejected("no_eligible_execution", rejected)
    candidates.sort(key=lambda c: (c.expected_microusd, c.reserved_microusd, c.offer.execution.id))
    attempts = []
    total_cost = total_time = 0
    for index in range(request.max_attempts):
        candidate = candidates[min(index, len(candidates) - 1)]
        duration = candidate.offer.maximum_ms + candidate.offer.stop_grace_ms
        if total_cost + candidate.reserved_microusd > request.budget_microusd:
            break
        if snapshot.at_ms + total_time + duration > request.deadline_ms:
            break
        attempts.append(candidate)
        total_cost += candidate.reserved_microusd
        total_time += duration
    # Canonicalize unordered observations before recording their hash.
    normalized = snapshot.model_copy(update={
        "offers": tuple(sorted(snapshot.offers, key=lambda o: o.execution.id)),
        "evidence": tuple(sorted(snapshot.evidence, key=digest)),
    })
    return Plan(request_hash=digest(request), profile_hash=digest(profile), snapshot_hash=digest(normalized),
                attempts=tuple(attempts), reservation_microusd=total_cost, rejections=rejected,
                validation_execution=validation_execution)


def validate_output(profile: Profile, text: str | None) -> str | None:
    if text is None or len(text.strip()) < profile.min_output_chars:
        return "empty_output"
    if len(text.encode()) > profile.max_output_bytes:
        return "output_too_large"
    if any(token not in text for token in profile.required_literals):
        return "required_literal_missing"
    if profile.output_schema is not None:
        try:
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError("duplicate JSON key")
                    result[key] = value
                return result
            def invalid_constant(value):
                raise ValueError("non-finite JSON number")
            value = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
            if not Draft202012Validator(profile.output_schema).is_valid(value):
                return "output_schema_failed"
        except (ValueError, RecursionError):
            return "invalid_json"
    return None


def current_authorization(attempt: AttemptPlan, request: Request, profile: Profile, bundle, now_ms: int,
                          validation_execution: str | None = None) -> str | None:
    current_profile = next((p for p in bundle.profiles if p.id == profile.id), None)
    current_offer = next((o for o in bundle.snapshot.offers if o.execution.id == attempt.offer.execution.id), None)
    if current_profile is None or digest(current_profile) != digest(profile) or current_offer is None:
        return "execution_contract_changed"
    # Renewed observations may extend freshness, but cannot change purchased execution terms.
    terms = {"observed_ms", "expires_ms", "ready"}
    if digest(current_offer.model_dump(exclude=terms)) != digest(attempt.offer.model_dump(exclude=terms)):
        return "execution_terms_changed"
    snapshot = bundle.snapshot.model_copy(update={"at_ms": now_ms, "offers": (current_offer,)})
    try:
        compile_plan(request, current_profile, snapshot, allow_fixture=bundle.allow_fixture,
                     validation_execution=validation_execution)
    except Rejected as exc:
        return exc.code
    return None


def next_action(job: dict, now_ms: int) -> str:
    """State + explicit time -> action. Effects only report observations back."""
    if job["status"] in {"succeeded", "failed", "cancelled"}:
        return "none"
    if job["status"] == "cleaning":
        return "cleanup"
    stopped = job["cancel_requested"] or now_ms >= job["request"]["deadline_ms"]
    if stopped:
        return "abort" if job["status"] != "queued" else "finish"
    if job["status"] == "queued":
        return "prepare"
    if job["status"] == "starting":
        return "start"
    if job["status"] == "running":
        return "poll"
    raise Rejected("invalid_job_state")


def may_retry(error: str | None, index: int, plan: Plan, cancelled: bool, now_ms: int, deadline_ms: int) -> bool:
    if cancelled or error not in {"capacity", "unavailable", "timeout"} or index + 1 >= len(plan.attempts):
        return False
    offer = plan.attempts[index + 1].offer
    return now_ms + offer.maximum_ms + offer.stop_grace_ms <= deadline_ms
