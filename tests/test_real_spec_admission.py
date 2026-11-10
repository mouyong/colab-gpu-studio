"""Regression from real /runtimespecs response; all providers are local fakes."""

import json
from types import SimpleNamespace

import pytest
from app import compute_routes, review_routes
from app.common import APIError
from app.db import Store
from app.gpu.compute import GpuConfig
from app.gpu.repository import Repository
from app.gpu.scheduler import Scheduler


def official_spec(accelerator="G4", **extra):
    return {"key": {"variant": "VARIANT_GPU", "accelerator": accelerator, "shape": "SHAPE_STANDARD"}, **extra}


def test_review_accepts_real_official_enum_but_not_missing_eligibility():
    spec = official_spec(eligible=True)
    assert review_routes._eligible_spec([spec], spec["key"]) == spec
    with pytest.raises(APIError):
        review_routes._eligible_spec([official_spec()], spec["key"])


def test_scheduler_detects_new_gpu_from_variant_not_marketing_name():
    scheduler = Scheduler(Repository(Store("sqlite:///:memory:")))
    assert scheduler._has_gpu(official_spec(eligible=True))
    assert not scheduler._has_gpu(
        {"key": {"variant": "VARIANT_CPU", "accelerator": "NONE", "shape": "SHAPE_STANDARD"}}
    )


def test_cuda_admission_official_spec_without_creating_runtime(monkeypatch):
    st = Store("sqlite:///:memory:")
    spec = official_spec(eligible=True)
    Repository(st).create("accounts", "owner", {"enabled": True, "allow_allocation": True}, record_id="smoke-account")
    monkeypatch.setattr(
        compute_routes.GpuConfig, "from_env", lambda: GpuConfig(real_enabled=True, unit_rate=0.01)
    )

    class ReadOnlyProvider:
        def __init__(self, *_, **__):
            pass

        def list_specs(self):
            return [official_spec("H100"), spec]

        def create(self, *_):
            pytest.fail("admission must never create a runtime")

    monkeypatch.setattr(compute_routes, "GoogleColabProvider", ReadOnlyProvider)
    monkeypatch.setattr(compute_routes.GoogleService, "status", lambda _: {"colab": {"connected": True}})
    request = SimpleNamespace(
        headers={"Idempotency-Key": "official-enum-test"}, state=SimpleNamespace(request_id="enum")
    )
    response = compute_routes.cuda_smoke(
        {"confirmed": True, "spec": spec, "account_id": "smoke-account"}, request, {"id": "owner", "role": "admin"}, st
    )
    assert response.status_code == 202
    job_id = json.loads(response.body)["data"]["id"]
    repo = Repository(st)
    assert repo.get("jobs", job_id, "owner")["requested_spec"] == spec
    assert repo.list("leases") == []
