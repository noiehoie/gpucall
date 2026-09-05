"""Explicitly synthetic supply for offline demonstrations and fault injection."""
from __future__ import annotations

import json
from pathlib import Path

from .models import Bundle, Cleanup, Evidence, Execution, Offer, Profile, ProviderResult, Snapshot, digest
from .store import Artifacts


def fixture_bundle(now_ms: int) -> Bundle:
    profiles = (Profile(id="extract", revision=1, task="infer", output_schema={
        "type": "object", "properties": {"text": {"type": "string"}, "images": {"type": "integer"}},
        "required": ["text", "images"], "additionalProperties": False}),
        Profile(id="vision", revision=1, task="vision"))
    execution = Execution(id="fixture", adapter="fixture", tasks=("infer", "vision"), model="synthetic",
                          model_revision="1", image="fixture-v1", gpu="none", region="local", trusted=True)
    offer = Offer(execution=execution, observed_ms=now_ms, expires_ms=now_ms+3600_000, ready=True,
                  rate_microusd_per_second=10, expected_ms=1000, maximum_ms=2000, hard_bound=True)
    evidence = tuple(Evidence(execution_hash=digest(execution), profile_hash=digest(p), tested_ms=now_ms,
                     expires_ms=now_ms+3600_000, passed=True, artifact_hash=digest({"fixture": p.id})) for p in profiles)
    return Bundle(profiles=profiles, snapshot=Snapshot(at_ms=now_ms, offers=(offer,), evidence=evidence),
                  account_limit_microusd=1000, allow_fixture=True)


class FixtureProvider:
    """A durable simulated remote. It never calls a GPU or claims real model quality."""
    def __init__(self, root: Path):
        self.files = Artifacts(root)
        self.output_override = None
        self.error = None
        self.pending = False
        self.cleanup_failures = 0
        self.starts = 0
        self.crash_after_start = False
        self.actual_microusd = 0

    def _path(self, token):
        return self.files.root / (digest(token) + ".json")

    def start(self, token, attempt, request, profile, inputs):
        existing = self.recover(token, attempt)
        if existing:
            return existing
        self.starts += 1
        output = self.output_override if self.output_override is not None else json.dumps({
            "text": request.instruction, "images": sum(i.media_type.startswith("image/") for i in request.inputs)}, ensure_ascii=False)
        payload = {"token": token, "output": output, "error": self.error, "stopped": False,
                   "actual_microusd": self.actual_microusd}
        from .models import canonical
        path = self._path(token)
        with path.open("x", encoding="utf-8") as handle:
            path.chmod(0o600)
            handle.write(canonical(payload))
            handle.flush()
            import os
            os.fsync(handle.fileno())
        if self.crash_after_start:
            raise RuntimeError("simulated lost acknowledgement")
        return token

    def recover(self, token, attempt):
        return token if self._path(token).exists() else None

    def poll(self, resource, attempt, profile):
        if self.pending:
            return ProviderResult(state="pending")
        data = json.loads(self._path(resource).read_text())
        return ProviderResult(state="failed" if data["error"] else "succeeded", output=data["output"],
                              error=data["error"], actual_microusd=data["actual_microusd"])

    def cleanup(self, token, resource, attempt):
        if self.cleanup_failures:
            self.cleanup_failures -= 1
            return Cleanup(stopped=False, proof="unconfirmed")
        path = self._path(token)
        if path.exists():
            path.unlink()
        return Cleanup(stopped=not path.exists(), proof="fixture_absent")
