import json
import pytest
from types import SimpleNamespace

from gpucall.fixtures import fixture_bundle
from gpucall.kernel import compile_plan
from gpucall.modal_provider import ModalProvider
from gpucall.models import Request

NOW = 1_800_000_000_000


class Missing(Exception):
    pass


class Files:
    def __init__(self):
        self.data = {}

    def write_text(self, text, path):
        self.data[path] = text

    def read_text(self, path):
        return self.data[path]

    def stat(self, path):
        if path not in self.data:
            raise Missing()
        return SimpleNamespace(size=len(self.data[path].encode()))


class Sandbox:
    def __init__(self, tags):
        self.object_id = "sb-owned"
        self.tags = tags
        self.filesystem = Files()
        self.code = None

    def poll(self):
        return self.code

    def get_tags(self):
        return self.tags

    def terminate(self):
        self.code = 137


class SDK:
    def __init__(self):
        self.created = []
        self.resources = []
        self.exception = SimpleNamespace(SandboxFilesystemNotFoundError=Missing)
        self.App = SimpleNamespace(lookup=lambda *a, **kw: SimpleNamespace(app_id="ap-owned"))
        self.Image = SimpleNamespace(from_id=lambda image: image)
        self.Sandbox = SimpleNamespace(create=self.create, list=self.list,
                                       from_id=lambda id: next(r for r in self.resources if r.object_id == id))

    def list(self, app_id, tags):
        assert app_id == "ap-owned"
        return [r for r in self.resources if r.tags == tags]

    def create(self, *args, **kwargs):
        self.created.append((args, kwargs))
        resource = Sandbox(kwargs["tags"])
        self.resources.append(resource)
        return resource


def setup():
    b = fixture_bundle(NOW)
    r = Request(key="modal", profile="extract", instruction="private instruction", deadline_ms=NOW+60000, budget_microusd=100)
    attempt = compile_plan(r, b.profiles[0], b.snapshot, allow_fixture=True).attempts[0]
    execution = attempt.offer.execution.model_copy(update={"adapter": "modal", "image": "im-pinned", "gpu": "T4"})
    attempt = attempt.model_copy(update={"offer": attempt.offer.model_copy(update={"execution": execution})})
    sdk = SDK()
    return sdk, ModalProvider(sdk), attempt, r, b.profiles[0]


def test_modal_start_is_owned_network_blocked_and_immutable():
    sdk, provider, attempt, request, profile = setup()
    resource = provider.start("owned-token", attempt, request, profile, [])
    args, kwargs = sdk.created[0]
    assert args[0] == "/.uv/.venv/bin/python"
    assert kwargs["block_network"] is True
    assert kwargs["image"] == "im-pinned"
    assert kwargs["timeout"] == 2
    assert "secrets" not in kwargs and "volumes" not in kwargs
    assert "private instruction" not in str(kwargs)
    assert provider.start("owned-token", attempt, request, profile, []) == resource
    assert len(sdk.created) == 1


def test_provider_native_missing_file_is_pending_and_redelivery_works():
    sdk, provider, attempt, request, profile = setup()
    resource = provider.start("owned-token", attempt, request, profile, [])
    assert provider.poll(resource, attempt, profile).state == "pending"
    sdk.resources[0].filesystem.data.clear()
    provider.deliver_recovered(resource, request, profile, [], execution=attempt.offer.execution)
    assert "/tmp/gpucall-ready" in sdk.resources[0].filesystem.data
    sdk.resources[0].filesystem.write_text(json.dumps({"state": "succeeded", "output": "result"}), "/tmp/gpucall-result.json")
    assert provider.poll(resource, attempt, profile).output == "result"


def test_cleanup_requires_ownership_and_actual_stopping():
    sdk, provider, attempt, request, profile = setup()
    resource = provider.start("owned-token", attempt, request, profile, [])
    assert not provider.cleanup("wrong-token", resource, attempt).stopped
    assert sdk.resources[0].poll() is None
    assert provider.cleanup("owned-token", resource, attempt).proof == "provider_terminated"
    assert not provider.cleanup("missing-token", None, attempt).stopped


def test_full_job_token_produces_valid_sdk_resource_name():
    pytest.importorskip("modal")
    from modal._utils.name_utils import check_object_name
    sdk, provider, attempt, request, profile = setup()
    token = "gc-" + "a" * 64 + "-0"
    provider.start(token, attempt, request, profile, [])
    kwargs = sdk.created[0][1]
    check_object_name(kwargs["name"], "Sandbox")
    assert kwargs["tags"]["gpucall_attempt"] == token
