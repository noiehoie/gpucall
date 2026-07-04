from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from gpucall.sovereignty import (
    PROVIDER_RESIDUE_MODEL,
    object_inventory,
    reap_objects,
    sovereignty_report,
)


class FakePaginator:
    def __init__(self, objects):
        self._objects = objects

    def paginate(self, **kwargs):
        prefix = kwargs.get("Prefix")
        contents = [o for o in self._objects if not prefix or o["Key"].startswith(prefix)]
        yield {"Contents": contents}


class FakeS3Client:
    def __init__(self, objects):
        self.objects = {o["Key"]: o for o in objects}
        self.deleted: list[str] = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return FakePaginator(list(self.objects.values()))

    def delete_object(self, *, Bucket, Key):
        self.objects.pop(Key, None)
        self.deleted.append(Key)

    def head_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"ContentLength": self.objects[Key]["Size"]}


def _store(objects):
    return SimpleNamespace(client=FakeS3Client(objects), config=SimpleNamespace(bucket="gpucall-test", presign_ttl_seconds=900))


def _obj(key: str, *, days_old: float, size: int = 100):
    return {"Key": key, "Size": size, "LastModified": datetime.now(timezone.utc) - timedelta(days=days_old)}


@pytest.fixture(autouse=True)
def isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("GPUCALL_STATE_DIR", raising=False)


def test_inventory_lists_keys_sizes_and_ages() -> None:
    store = _store([_obj("gpucall/tenants/a/x.txt", days_old=2), _obj("gpucall/tenants/b/y.txt", days_old=0.1)])

    items = object_inventory(store)

    assert [item["key"] for item in items] == ["gpucall/tenants/a/x.txt", "gpucall/tenants/b/y.txt"]
    assert items[0]["age_seconds"] > 86400
    assert items[1]["bytes"] == 100


def test_reap_dry_run_deletes_nothing() -> None:
    store = _store([_obj("old.txt", days_old=10), _obj("new.txt", days_old=1)])

    report = reap_objects(store, older_than_days=7)

    assert report["apply"] is False
    assert report["object_count_eligible"] == 1
    assert report["receipts"][0] == {
        "key": "old.txt",
        "bytes": 100,
        "age_seconds": report["receipts"][0]["age_seconds"],
        "action": "would_delete",
    }
    assert store.client.deleted == []
    assert "receipt_path" not in report


def test_reap_apply_deletes_verifies_and_writes_receipt() -> None:
    store = _store([_obj("old.txt", days_old=10), _obj("new.txt", days_old=1)])

    report = reap_objects(store, older_than_days=7, apply=True)

    assert store.client.deleted == ["old.txt"]
    receipt = report["receipts"][0]
    assert receipt["action"] == "delete"
    assert receipt["verified_absent"] is True
    assert report["all_verified_absent"] is True
    saved = json.loads(open(report["receipt_path"], encoding="utf-8").read())
    assert saved["phase"] == "object-reclamation"
    assert saved["receipts"][0]["key"] == "old.txt"
    # "new.txt" stayed
    assert "new.txt" in store.client.objects


def test_reap_prefix_scopes_deletion() -> None:
    store = _store([_obj("gpucall/tenants/a/old.txt", days_old=10), _obj("gpucall/tenants/b/old.txt", days_old=10)])

    report = reap_objects(store, older_than_days=7, prefix="gpucall/tenants/a/", apply=True)

    assert store.client.deleted == ["gpucall/tenants/a/old.txt"]
    assert report["object_count_eligible"] == 1


def test_sovereignty_report_with_and_without_store() -> None:
    without = sovereignty_report(None)
    assert without["object_store"]["configured"] is False
    assert "modal-function-runtime" in without["provider_residue_model"]

    store = _store([_obj("k1", days_old=3)])
    reap_objects(store, older_than_days=1, apply=True)
    with_store = sovereignty_report(_store([_obj("k2", days_old=0.5)]))

    assert with_store["object_store"]["configured"] is True
    assert with_store["object_store"]["object_count"] == 1
    assert with_store["object_store"]["presign_ttl_seconds"] == 900
    assert with_store["reclamation_receipts"], "receipt history must surface"
    assert with_store["reclamation_receipts"][0]["all_verified_absent"] is True
    assert PROVIDER_RESIDUE_MODEL["modal-function-runtime"]["payload_persistence"].startswith("ephemeral")


def test_reap_rejects_negative_age() -> None:
    with pytest.raises(ValueError):
        reap_objects(_store([]), older_than_days=-1)
