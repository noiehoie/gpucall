"""Durable effect interpreter. One OS-released worker lock prevents competing runners."""
from __future__ import annotations

import fcntl
import os

from .kernel import current_authorization, may_retry, next_action, validate_output
from .models import Bundle, Plan, Profile, Request
from .store import Store


class Runtime:
    def __init__(self, store: Store, bundle: Bundle, providers: dict):
        self.store, self.bundle, self.providers = store, bundle, providers

    def tick(self, now_ms: int) -> int:
        fd = os.open(self.store.root / "runner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            changed = 0
            if self.store.account()["overrun"]:
                for job in self.store.jobs(active_only=True):
                    self.store.cancel(job["id"], now_ms)
            for job in self.store.jobs(active_only=True):
                try:
                    changed += self._step(job, now_ms)
                except Exception as exc:
                    # Ambiguous side effects remain pending. Never expose provider text/secrets.
                    current = self.store.get(job["id"])
                    code = "effect_unconfirmed:" + type(exc).__name__
                    if current["error"] != code:
                        current["error"] = code
                        self.store.save(current, now_ms, "effect_unconfirmed")
                        changed += 1
            return changed
        finally:
            os.close(fd)

    def _step(self, job: dict, now_ms: int) -> int:
        action = next_action(job, now_ms)
        if action == "none":
            return 0
        request = Request.model_validate(job["request"])
        profile = Profile.model_validate(job["profile"])
        plan = Plan.model_validate(job["plan"])
        attempt = plan.attempts[job["attempt"]]
        provider = self.providers[attempt.offer.execution.adapter]
        token = f"gc-{job['id']}-{job['attempt']}"
        if action == "prepare":
            active = sum(j["status"] in {"starting", "running", "cleaning"} for j in self.store.jobs(active_only=True))
            if active >= self.bundle.max_running:
                return 0
            reason = current_authorization(attempt, request, profile, self.store.enrich(self.bundle), now_ms,
                                           plan.validation_execution)
            if reason:
                job.update(status="failed", error=reason, reserved=0)
            else:
                job.update(status="starting", error=None, start_sent=False)
            self.store.save(job, now_ms, "prepared")
        elif action == "finish":
            job.update(status="cancelled" if job["cancel_requested"] else "failed", reserved=0,
                       error="cancelled" if job["cancel_requested"] else "deadline_expired")
            self.store.save(job, now_ms, "finished_without_execution")
        elif action == "start":
            if not job["start_sent"]:
                reason = current_authorization(attempt, request, profile, self.store.enrich(self.bundle), now_ms,
                                               plan.validation_execution)
                if reason:
                    job.update(status="failed", error=reason, reserved=0)
                    self.store.save(job, now_ms, "start_rejected")
                    return 1
                inputs = [self.store.artifacts.get(i.sha256) for i in request.inputs]
                job["start_sent"] = True
                job = self.store.save(job, now_ms, "start_intent")
                if job["cancel_requested"]:
                    # The intent exists but no provider call was made.
                    job.update(start_sent=False, status="cleaning", outcome="cancelled", error="cancelled")
                    self.store.save(job, now_ms, "cancel_before_create")
                    return 1
                # No automatic repeat of this create call after its outcome becomes ambiguous.
                resource = provider.start(token, attempt, request, profile, inputs)
            else:
                resource = provider.recover(token, attempt)
                if resource is not None and hasattr(provider, "deliver_recovered"):
                    provider.deliver_recovered(resource, request, profile,
                                               [self.store.artifacts.get(i.sha256) for i in request.inputs],
                                               execution=attempt.offer.execution)
            if resource is None:
                if job["error"] == "submission_unknown":
                    return 0
                job["error"] = "submission_unknown"
                self.store.save(job, now_ms, "reconciliation_required")
            else:
                job.update(resource=resource, status="running", error=None)
                self.store.save(job, now_ms, "started")
        elif action == "poll":
            result = provider.poll(job["resource"], attempt, profile)
            if result.state == "pending":
                return 0
            error = result.error if result.state == "failed" else validate_output(profile, result.output)
            if result.state == "failed" and not error:
                error = "worker_failed"
            if not error:
                job["output"] = self.store.artifacts.put(result.output.encode())
            job.update(status="cleaning", outcome="failed" if error else "succeeded", error=error,
                       actual_microusd=result.actual_microusd)
            self.store.save(job, now_ms, "result_observed")
        elif action == "abort":
            job.update(status="cleaning", outcome="cancelled" if job["cancel_requested"] else "failed",
                       error="cancelled" if job["cancel_requested"] else "deadline_expired")
            self.store.save(job, now_ms, "abort_intent")
        elif action == "cleanup":
            if job["start_sent"]:
                receipt = provider.cleanup(token, job["resource"], attempt)
                if not receipt.stopped:
                    return 0
                actual = job["actual_microusd"]
                charge = attempt.reserved_microusd if actual is None else actual
                basis = "reserved_upper_bound" if actual is None else "provider_reported"
                job["receipts"].append({"attempt": job["attempt"], **receipt.model_dump(), "at_ms": now_ms,
                                        "charged_microusd": charge, "cost_basis": basis})
                job["cost_basis"] = "reserved_upper_bound" if any(r["cost_basis"] == "reserved_upper_bound" for r in job["receipts"]) else "provider_reported"
                job["charged"] += charge
                job["reserved"] -= attempt.reserved_microusd
                if charge > attempt.reserved_microusd:
                    job.update(error="provider_cost_overrun", outcome="failed")
            if may_retry(job["error"], job["attempt"], plan, job["cancel_requested"], now_ms, request.deadline_ms):
                job.update(attempt=job["attempt"]+1, status="queued", resource=None, start_sent=False,
                           actual_microusd=None, outcome=None)
            else:
                status = "cancelled" if job["cancel_requested"] else job["outcome"]
                job.update(status=status or "failed", reserved=0)
            self.store.save(job, now_ms, "cleanup_confirmed")
        return 1


def public_job(job: dict) -> dict:
    return {key: job[key] for key in ("id", "status", "attempt", "cancel_requested", "reserved", "charged",
                                      "cost_basis", "output", "error", "receipts", "created_ms", "updated_ms")}
