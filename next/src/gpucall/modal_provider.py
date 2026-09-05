"""Modal Sandbox effects. Model weights + worker are pinned in a prebuilt image.

Never create an image or resolve a model revision on the job execution path.
No persistent volumes, injected secrets, inbound ports or outbound networking.
Unknown start acknowledgements are recovered by owner-scoped tags, never resubmitted.
"""
from __future__ import annotations

import base64

from .models import Cleanup, ProviderResult, canonical, digest


class ModalProvider:
    def __init__(self, sdk=None):
        if sdk is None:
            import modal as sdk
        self.sdk = sdk

    def _app(self, attempt):
        return self.sdk.App.lookup(attempt.offer.execution.app, create_if_missing=False)

    def _tags(self, token, attempt):
        return {"gpucall_attempt": token, "execution_hash": digest(attempt.offer.execution)}

    def recover(self, token, attempt):
        matches = list(self.sdk.Sandbox.list(app_id=self._app(attempt).app_id, tags=self._tags(token, attempt)))
        if len(matches) > 1:
            raise RuntimeError("multiple resources for one attempt")
        return matches[0].object_id if matches else None

    def start(self, token, attempt, request, profile, inputs):
        existing = self.recover(token, attempt)
        if existing:
            return existing
        execution = attempt.offer.execution
        if not execution.image.startswith("im-"):
            raise ValueError("Modal execution requires a prebuilt immutable image ID")
        sandbox = self.sdk.Sandbox.create(
            "/.uv/.venv/bin/python", "/opt/gpucall-worker/worker.py",
            app=self._app(attempt), name="gc-" + digest(token)[:48], tags=self._tags(token, attempt),
            image=self.sdk.Image.from_id(execution.image), gpu=execution.gpu, region=execution.region,
            timeout=(attempt.offer.maximum_ms + 999)//1000, block_network=True,
            cpu=(1, 1), memory=(8192, 8192),
        )
        # Input delivery is repeatable; the worker's ready marker is written last.
        self._deliver(sandbox, request, profile, inputs, execution=execution)
        return sandbox.object_id

    def _deliver(self, sandbox, request, profile, inputs, execution=None):
        payload = {"instruction": request.instruction, "task": profile.task, "max_tokens": profile.max_tokens,
                   "max_output_bytes": profile.max_output_bytes,
                   "inputs": [{"media_type": ref.media_type, "data": base64.b64encode(data).decode()}
                              for ref, data in zip(request.inputs, inputs, strict=True)]}
        if execution is not None:
            payload.update(model=execution.model, model_revision=execution.model_revision)
        sandbox.filesystem.write_text(canonical(payload), "/tmp/gpucall-input.json")
        sandbox.filesystem.write_text("ready", "/tmp/gpucall-ready")

    def deliver_recovered(self, resource, request, profile, inputs, execution=None):
        sandbox = self.sdk.Sandbox.from_id(resource)
        if sandbox.poll() is not None:
            return
        try:
            sandbox.filesystem.stat("/tmp/gpucall-ready")
        except (FileNotFoundError, self.sdk.exception.SandboxFilesystemNotFoundError):
            self._deliver(sandbox, request, profile, inputs, execution=execution)

    def poll(self, resource, attempt, profile):
        sandbox = self.sdk.Sandbox.from_id(resource)
        if sandbox.poll() is not None:
            return ProviderResult(state="failed", error="lost")
        try:
            info = sandbox.filesystem.stat("/tmp/gpucall-result.json")
        except (FileNotFoundError, self.sdk.exception.SandboxFilesystemNotFoundError):
            return ProviderResult(state="pending")
        if info.size > profile.max_output_bytes * 6 + 4096:
            return ProviderResult(state="failed", error="worker_failed")
        text = sandbox.filesystem.read_text("/tmp/gpucall-result.json")
        return ProviderResult.model_validate_json(text)

    def cleanup(self, token, resource, attempt):
        if resource is None:
            resource = self.recover(token, attempt)
        if resource is None:
            # A missing list entry is not proof that an ambiguous start never happened.
            return Cleanup(stopped=False, proof="unconfirmed")
        sandbox = self.sdk.Sandbox.from_id(resource)
        if sandbox.get_tags() != self._tags(token, attempt):
            return Cleanup(stopped=False, proof="unconfirmed")
        sandbox.terminate()
        stopped = sandbox.poll() is not None
        return Cleanup(stopped=stopped, proof="provider_terminated" if stopped else "unconfirmed")
