import itertools

import pytest
from pydantic import ValidationError

from gpucall.fixtures import fixture_bundle
from gpucall.kernel import Rejected, compile_plan, validate_output
from gpucall.models import Evidence, Profile, Request, canonical, digest

NOW = 1_800_000_000_000


def request(**changes):
    return Request(key="one", profile="extract", instruction="hello", deadline_ms=NOW+60000,
                   budget_microusd=100, **changes)


def test_order_is_irrelevant_and_cost_is_first():
    b = fixture_bundle(NOW)
    first = b.snapshot.offers[0]
    second = first.model_copy(update={"execution": first.execution.model_copy(update={"id": "large-cheap", "gpu": "H200"}),
                                       "rate_microusd_per_second": 1})
    e = b.snapshot.evidence[0].model_copy(update={"execution_hash": digest(second.execution)})
    answers = set()
    for offers in itertools.permutations((first, second)):
        for evidence in itertools.permutations((*b.snapshot.evidence, e)):
            p = compile_plan(request(), b.profiles[0], b.snapshot.model_copy(update={"offers": offers, "evidence": evidence}), allow_fixture=True)
            assert p.attempts[0].offer.execution.id == "large-cheap"
            answers.add(canonical(p))
    assert len(answers) == 1


@pytest.mark.parametrize("field,value,reason", [
    ("expires_ms", NOW, "price_not_fresh"), ("observed_ms", NOW+1, "price_not_fresh"),
    ("ready", False, "not_ready"), ("hard_bound", False, "hard_budget_unavailable"),
    ("rate_microusd_per_second", 1000, "budget_exceeded")])
def test_fail_closed(field, value, reason):
    b = fixture_bundle(NOW)
    snapshot = b.snapshot.model_copy(update={"offers": (b.snapshot.offers[0].model_copy(update={field: value}),)})
    with pytest.raises(Rejected) as error:
        compile_plan(request(), b.profiles[0], snapshot, allow_fixture=True)
    assert reason in error.value.details["fixture"]


def test_latest_failure_blocks_older_success():
    b = fixture_bundle(NOW)
    failed = b.snapshot.evidence[0].model_copy(update={"passed": False})
    snapshot = b.snapshot.model_copy(update={"evidence": (*b.snapshot.evidence, failed)})
    with pytest.raises(Rejected):
        compile_plan(request(), b.profiles[0], snapshot, allow_fixture=True)


def test_unrelated_profile_does_not_invalidate_evidence():
    b = fixture_bundle(NOW)
    p = compile_plan(request(), b.profiles[0], b.snapshot, allow_fixture=True)
    assert p.profile_hash == digest(b.profiles[0])
    changed = b.profiles[0].model_copy(update={"revision": 2})
    with pytest.raises(Rejected):
        compile_plan(request(), changed, b.snapshot, allow_fixture=True)


def test_fixture_and_attestation_are_explicit():
    b = fixture_bundle(NOW)
    with pytest.raises(Rejected):
        compile_plan(request(), b.profiles[0], b.snapshot)
    with pytest.raises(Rejected):
        compile_plan(request(privacy="attested"), b.profiles[0], b.snapshot, allow_fixture=True)


@pytest.mark.parametrize("value", [True, -1, 1.2, "100"])
def test_money_is_strict_integer(value):
    with pytest.raises(ValidationError):
        request().model_validate({**request().model_dump(), "budget_microusd": value})


def test_output_validation_rejects_duplicate_keys_and_nonfinite():
    p = Profile(id="json", revision=1, task="infer", output_schema={"type": "object"})
    for text in ['{"x":1,"x":2}', '{"x":NaN}', 'not JSON']:
        assert validate_output(p, text) == "invalid_json"
    assert validate_output(p, '{"x":1}') is None


def test_schema_cannot_fetch_network():
    with pytest.raises(ValidationError):
        Profile(id="bad", revision=1, task="infer", output_schema={"$ref": "https://example.org/schema"})


def test_control_module_has_no_effectful_imports():
    import ast
    import inspect
    import gpucall.kernel
    tree = ast.parse(inspect.getsource(gpucall.kernel))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module)
    assert modules <= {"__future__", "json", "jsonschema", "models"}
