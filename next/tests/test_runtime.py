from concurrent.futures import ThreadPoolExecutor

import pytest

from gpucall.fixtures import FixtureProvider, fixture_bundle
from gpucall.kernel import Rejected
from gpucall.models import Input, Request
from gpucall.runtime import Runtime
from gpucall.store import Store

NOW = 1_800_000_000_000


def setup(tmp_path, **changes):
    bundle = fixture_bundle(NOW)
    store = Store(tmp_path / "state", bundle.account_limit_microusd)
    provider = FixtureProvider(tmp_path / "remote")
    runtime = Runtime(store, bundle, {"fixture": provider})
    request = Request(key="one", profile="extract", instruction="hello", deadline_ms=NOW+60000,
                      budget_microusd=100, **changes)
    return store, provider, runtime, request


def finish(runtime):
    for _ in range(30):
        runtime.tick(NOW)
        if not runtime.store.jobs(active_only=True):
            return
    raise AssertionError("jobs did not finish")


def test_full_job_artifact_cleanup_account_and_replay(tmp_path):
    store, provider, runtime, request = setup(tmp_path)
    job = store.submit(request, runtime.bundle, NOW)
    assert store.account()["reserved_microusd"] == 20
    finish(runtime)
    result = store.get(job["id"])
    assert result["status"] == "succeeded"
    assert b'"text": "hello"' in store.artifacts.get(result["output"])
    assert result["receipts"][0]["proof"] == "fixture_absent"
    assert store.account()["reserved_microusd"] == 0
    assert store.replay(job["id"])["matches"]
    assert store.audit()["valid"]


def test_atomic_budget_under_concurrent_requests(tmp_path):
    store, _, runtime, request = setup(tmp_path)
    store = Store(store.root, 20)
    def submit(index):
        try:
            store.submit(request.model_copy(update={"key": f"job-{index}"}), runtime.bundle, NOW)
            return "accepted"
        except Rejected as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(16)))
    assert results.count("accepted") == 1
    assert results.count("account_budget_exceeded") == 15
    assert store.account()["reserved_microusd"] == 20


def test_idempotent_concurrent_submission_and_conflict(tmp_path):
    store, _, runtime, request = setup(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: store.submit(request, runtime.bundle, NOW)["id"], range(16)))
    assert len(set(ids)) == 1 and len(store.jobs()) == 1
    with pytest.raises(Rejected, match="idempotency_conflict"):
        store.submit(request.model_copy(update={"instruction": "changed"}), runtime.bundle, NOW)


def test_lost_start_ack_recovers_without_second_creation(tmp_path):
    store, provider, runtime, request = setup(tmp_path)
    provider.crash_after_start = True
    job = store.submit(request, runtime.bundle, NOW)
    runtime.tick(NOW)
    runtime.tick(NOW)
    assert store.get(job["id"])["status"] == "starting"
    provider2 = FixtureProvider(tmp_path / "remote")
    recovered = Runtime(Store(store.root, 1000), runtime.bundle, {"fixture": provider2})
    finish(recovered)
    assert provider.starts == 1 and provider2.starts == 0
    assert store.get(job["id"])["status"] == "succeeded"


def test_cleanup_failure_holds_reservation_after_result(tmp_path):
    store, provider, runtime, request = setup(tmp_path)
    provider.cleanup_failures = 3
    job = store.submit(request, runtime.bundle, NOW)
    for _ in range(5):
        runtime.tick(NOW)
    assert store.get(job["id"])["status"] == "cleaning"
    assert store.get(job["id"])["output"]
    assert store.account()["reserved_microusd"] == 20
    finish(runtime)
    assert store.account()["reserved_microusd"] == 0


@pytest.mark.parametrize("ticks", [0, 1, 2])
def test_cancel_before_during_and_after_start(tmp_path, ticks):
    store, provider, runtime, request = setup(tmp_path)
    job = store.submit(request, runtime.bundle, NOW)
    for _ in range(ticks):
        runtime.tick(NOW)
    store.cancel(job["id"], NOW)
    finish(runtime)
    assert store.get(job["id"])["status"] == "cancelled"
    assert store.account()["reserved_microusd"] == 0


def test_retry_is_bounded_and_cleans_previous_attempt(tmp_path):
    store, provider, runtime, request = setup(tmp_path, max_attempts=2)
    provider.error = "unavailable"
    job = store.submit(request, runtime.bundle, NOW)
    for _ in range(4):
        runtime.tick(NOW)
    assert store.get(job["id"])["attempt"] == 1
    provider.error = None
    finish(runtime)
    assert provider.starts == 2
    assert len(store.get(job["id"])["receipts"]) == 2


def test_output_failure_does_not_spend_on_retries(tmp_path):
    store, provider, runtime, request = setup(tmp_path, max_attempts=3)
    provider.output_override = "not json"
    job = store.submit(request, runtime.bundle, NOW)
    finish(runtime)
    assert store.get(job["id"])["error"] == "invalid_json"
    assert provider.starts == 1


def test_unknown_actual_cost_is_accounted_conservatively(tmp_path):
    store, provider, runtime, request = setup(tmp_path)
    provider.actual_microusd = None
    job = store.submit(request, runtime.bundle, NOW)
    finish(runtime)
    assert store.get(job["id"])["charged"] == 20
    assert store.get(job["id"])["cost_basis"] == "reserved_upper_bound"


def test_vision_input_is_verified_and_passed(tmp_path):
    store, provider, runtime, request = setup(tmp_path)
    image = b"test-image-fixture"
    sha = store.artifacts.put(image)
    request = request.model_copy(update={"profile": "vision", "inputs": (Input(sha256=sha, media_type="image/png", size=len(image)),)})
    job = store.submit(request, runtime.bundle, NOW)
    finish(runtime)
    assert b'"images": 1' in store.artifacts.get(store.get(job["id"])["output"])


def test_tampered_input_is_rejected(tmp_path):
    store, _, runtime, request = setup(tmp_path)
    sha = store.artifacts.put(b"abc")
    store.artifacts.path(sha).write_bytes(b"def")
    with pytest.raises(Rejected, match="artifact_integrity_failed"):
        store.submit(request.model_copy(update={"inputs": (Input(sha256=sha, media_type="text/plain", size=3),)}), runtime.bundle, NOW)


def test_expired_queue_never_starts(tmp_path):
    store, provider, runtime, request = setup(tmp_path)
    job = store.submit(request, runtime.bundle, NOW)
    runtime.tick(request.deadline_ms)
    assert store.get(job["id"])["status"] == "failed"
    assert provider.starts == 0


def test_state_and_artifact_permissions(tmp_path):
    store, _, _, _ = setup(tmp_path)
    sha = store.artifacts.put(b"secret")
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.artifacts.path(sha).stat().st_mode & 0o777 == 0o600


def test_explicit_validation_earns_evidence_without_bypassing_production(tmp_path):
    store, _, runtime, request = setup(tmp_path)
    bundle = runtime.bundle.model_copy(update={"snapshot": runtime.bundle.snapshot.model_copy(update={"evidence": ()})})
    runtime.bundle = bundle
    with pytest.raises(Rejected, match="no_eligible_execution"):
        store.submit(request, bundle, NOW)
    calibration = request.model_copy(update={"key": "calibration", "privacy": "public"})
    job = store.submit(calibration, bundle, NOW, validation_execution="fixture")
    with pytest.raises(Rejected, match="idempotency_conflict"):
        store.submit(calibration, bundle, NOW)
    finish(runtime)
    evidence = store.certify(job["id"], 60000)
    assert evidence.passed
    assert store.replay(job["id"])["matches"]
    normal = store.submit(request, bundle, NOW)
    assert normal["plan"]["validation_execution"] is None


def test_latest_expired_failure_never_revives_old_success(tmp_path):
    from gpucall.kernel import compile_plan
    store, _, runtime, request = setup(tmp_path)
    bundle = runtime.bundle
    failed = bundle.snapshot.evidence[0].model_copy(update={"passed": False, "expires_ms": NOW})
    snapshot = bundle.snapshot.model_copy(update={"evidence": (*bundle.snapshot.evidence, failed)})
    with pytest.raises(Rejected):
        compile_plan(request, bundle.profiles[0], snapshot, allow_fixture=True)
