from __future__ import annotations

import copy

import pytest
from app.gpu.workbench import WorkbenchError, WorkbenchService


class MemoryRepo:
    def __init__(self):
        self.rows = {"workflows": [], "jobs": [], "operations": [], "instances": [], "events": []}
        self.fail_update = None
        self.store = self  # assets live in the same in-memory table set

    def list(self, kind, owner_id=None, project_id=None):
        rows = self.rows.get(kind, [])
        return [
            copy.deepcopy(row)
            for row in rows
            if owner_id is None or row.get("owner_id") == owner_id
        ]

    def get(self, kind, record_id, owner_id=None):
        for row in self.rows.get(kind, []):
            if row.get("id") == record_id and (owner_id is None or row.get("owner_id") == owner_id):
                return copy.deepcopy(row)
        return None

    def create(self, kind, owner_id, data, project_id=None, record_id=None):
        row = {
            "id": record_id or f"{kind}-{len(self.rows.get(kind, [])) + 1}",
            "owner_id": owner_id,
            **copy.deepcopy(dict(data)),
        }
        if project_id is not None:
            row["project_id"] = project_id
        if self.get(kind, row["id"], owner_id) is not None:
            raise ValueError("already exists")
        self.rows.setdefault(kind, []).append(row)
        return copy.deepcopy(row)

    def update(self, kind, record_id, patch, owner_id=None):
        if self.fail_update is not None:
            raise self.fail_update
        row = self.get(kind, record_id, owner_id)
        if row is None:
            raise KeyError(record_id)
        for stored in self.rows[kind]:
            if stored["id"] == record_id and (owner_id is None or stored.get("owner_id") == owner_id):
                stored.update(copy.deepcopy(dict(patch)))
                return copy.deepcopy(stored)
        raise KeyError(record_id)


def _workflow():
    return {
        "id": "wf-1",
        "owner_id": "owner",
        "workflow_key": "demo-mock",
        "workflow_version": 1,
        "state": "published",
        "model_id": "demo-mock",
        "manifest": {"parameters": {"prompt": {"type": "string", "default": "hi"}}},
        "api_graph": {"1": {"class_type": "Prompt", "inputs": {}}},
    }


def _service(repo, admit_job=None):
    repo.rows["workflows"] = [_workflow()]
    instance = {"id": "instance-1", "owner_id": "owner", "project_id": "project-1", "state": "ready", "mock": True}
    return WorkbenchService(repo=repo, admit_job=admit_job), instance


def _queue_payload(job_key="job-1"):
    return {
        "workflow_key": "demo-mock",
        "workflow_version": 1,
        "job_input": {"execution_policy": {"provider": "mock"}, "params": {"marker": job_key}},
    }


def test_mock_queue_requires_admit_adapter_and_never_creates_private_job():
    repo = MemoryRepo()
    service, instance = _service(repo)

    with pytest.raises(WorkbenchError) as exc:
        service.execute("owner", instance, "queue_reviewed_workflow", _queue_payload(), key="op-1")

    assert exc.value.status == 503
    assert exc.value.code == "QUEUE_UNAVAILABLE"
    assert repo.rows["jobs"] == []


def test_mock_queue_persists_job_through_adapter_and_operation_survives_restart():
    repo = MemoryRepo()
    calls = []

    def admit_job(owner, body, key, *, instance):
        calls.append((owner, copy.deepcopy(body), key, instance["id"]))
        assert body["project_id"] == "project-1"
        assert body["model_id"] == "demo-mock"
        return repo.create(
            "jobs",
            owner,
            {
                "idempotency_key": key,
                "request_body": body,
                "instance_id": instance["id"],
                "provider": "mock",
                "state": "queued",
            },
        )

    service, instance = _service(repo, admit_job)
    payload = _queue_payload()
    first = service.execute("owner", instance, "queue_reviewed_workflow", payload, key="op-1")

    restarted, same_instance = _service(repo, admit_job=lambda *_args, **_kwargs: pytest.fail("re-admitted"))
    second = restarted.execute("owner", same_instance, "queue_reviewed_workflow", payload, key="op-1")

    assert first == second
    assert len(calls) == 1
    assert calls[0][2] == "op-1"
    assert len(repo.rows["jobs"]) == 1
    assert len(repo.rows["operations"]) == 1
    assert repo.rows["jobs"][0]["state"] == "queued"
    assert "output_asset_ids" not in repo.rows["jobs"][0]


def test_operation_key_conflict_is_persistent():
    repo = MemoryRepo()

    def admit_job(owner, body, key, *, instance):
        return repo.create("jobs", owner, {"idempotency_key": key, "instance_id": instance["id"], "state": "queued"})

    service, instance = _service(repo, admit_job)
    service.execute("owner", instance, "queue_reviewed_workflow", _queue_payload(), key="op-1")

    with pytest.raises(WorkbenchError) as exc:
        service.execute("owner", instance, "queue_reviewed_workflow", _queue_payload("different"), key="op-1")

    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    assert exc.value.status == 409


def test_mock_health_is_explicitly_simulated_not_verified_comfy_process():
    repo = MemoryRepo()
    service, instance = _service(repo)

    result = service.execute("owner", instance, "health")

    assert result["simulated"] is True
    assert result["verified"] is False
    assert result["status"] == "simulated"


def test_draft_persistence_error_is_not_swallowed(monkeypatch):
    repo = MemoryRepo()
    service, instance = _service(repo)
    repo.rows["instances"] = [instance]
    repo.fail_update = RuntimeError("draft store unavailable")
    monkeypatch.setattr("app.gpu.workbench.resolve_parameters", lambda _manifest, params: dict(params))
    monkeypatch.setattr("app.gpu.workbench.bind_parameters", lambda _manifest, graph, params: {"bound": params})

    with pytest.raises(RuntimeError, match="draft store unavailable"):
        service.execute(
            "owner",
            instance,
            "set_reviewed_params",
            {"workflow_key": "demo-mock", "workflow_version": 1, "params": {"prompt": "hello"}},
            key="draft-op-1",
        )


def test_describe_returns_frontend_capabilities_manifest_draft_jobs_artifacts_and_logs():
    repo = MemoryRepo()
    service, instance = _service(repo)
    repo.rows["instances"] = [{
        **instance,
        "workbench_drafts": {
            "demo-mock@1": {
                "params": {"prompt": "saved"},
                "bound_graph": {"secret": "must not be returned"},
                "draft": True,
            }
        },
    }]
    repo.rows["jobs"] = [
        {"id": "job-1", "owner_id": "owner", "instance_id": instance["id"], "project_id": "project-1", "output_asset_ids": ["asset-1", "asset-cross"]},
        {"id": "job-other-instance", "owner_id": "owner", "instance_id": "other-instance", "output_asset_ids": ["asset-other"]},
        {"id": "job-other-owner", "owner_id": "other-owner", "instance_id": instance["id"], "output_asset_ids": ["asset-other-owner"]},
    ]
    repo.rows["assets"] = [
        {"id": "asset-1", "owner_id": "owner", "name": "result.png", "mime": "image/png", "thumbnail_url": "/api/gpu/assets/asset-1"},
        {"id": "asset-cross", "owner_id": "owner", "name": "cross.png", "thumbnail_url": "https://signed.example/secret?token=leak"},
        {"id": "asset-other", "owner_id": "owner", "name": "other.png"},
        {"id": "asset-other-owner", "owner_id": "other-owner", "name": "private.png"},
    ]
    repo.rows["events"] = [
        {"id": "event-1", "owner_id": "owner", "job_id": "job-1", "created_at": "now", "message": "done https://logs.example/run/123"},
        {"id": "event-other", "owner_id": "owner", "job_id": "job-other-instance", "message": "must not cross instance"},
        {"id": "event-other-owner", "owner_id": "other-owner", "job_id": "job-1", "message": "must not cross owner"},
    ]

    described = service.describe("owner", instance)

    assert described["capabilities"] == described["actions"]
    assert described["capabilities"]
    workflow = described["allowed_workflows"][0]
    assert workflow["manifest"]["parameters"] == _workflow()["manifest"]["parameters"]
    assert described["parameters"][0]["key"] == "prompt"
    assert described["values"] == {"prompt": "saved"}
    assert described["drafts"]["demo-mock@1"] == {"prompt": "saved"}
    assert [item["id"] for item in described["artifacts"]] == ["asset-1", "asset-cross"]
    assert all("https://" not in str(item) and "token" not in str(item) for item in described["artifacts"])
    assert [item["job_id"] for item in described["logs"]] == ["job-1"]
    assert "https://" not in described["logs"][0]["message"]
    assert "token" not in described["logs"][0]["message"]


def test_describe_does_not_claim_writes_for_history_or_unverified_real_instance():
    repo = MemoryRepo()
    service, _ = _service(repo)

    for instance in (
        {"id": "history", "owner_id": "owner", "state": "deleted", "provider": "mock"},
        {"id": "real", "owner_id": "owner", "state": "ready", "provider": "colab"},
    ):
        described = service.describe("owner", instance)
        assert described["read_only"] is True
        assert "set_reviewed_params" not in described["capabilities"]
        assert "queue_reviewed_workflow" not in described["capabilities"]
        assert "stop_current_job" not in described["capabilities"]
        assert described["actions"] == described["capabilities"]


def test_unverified_real_stop_is_rejected_before_trusted_mcp_call():
    class FakeTrustedMcp:
        def __init__(self):
            self.calls = []

        def stop_current_job(self, *args):
            self.calls.append(args)
            return {"accepted": True}

    repo = MemoryRepo()
    trusted_mcp = FakeTrustedMcp()
    service = WorkbenchService(repo=repo, trusted_mcp=trusted_mcp)
    instance = {
        "id": "instance-real",
        "owner_id": "owner",
        "state": "ready",
        "provider": "colab",
        "verified": False,
        "current_job_id": "job-1",
    }
    repo.rows["jobs"] = [{
        "id": "job-1",
        "owner_id": "owner",
        "instance_id": "instance-real",
        "state": "running",
    }]

    with pytest.raises(WorkbenchError) as exc:
        service.execute("owner", instance, "stop_current_job", {}, key="stop-op")

    assert exc.value.code == "COMFY_UNVERIFIED"
    assert trusted_mcp.calls == []


def test_preview_artifacts_recursively_sanitize_nested_metadata():
    repo = MemoryRepo()
    service, instance = _service(repo)
    repo.rows["jobs"] = [{
        "id": "job-1",
        "owner_id": "owner",
        "instance_id": instance["id"],
        "state": "completed",
        "output_asset_ids": ["asset-1"],
    }]
    repo.rows["assets"] = [{
        "id": "asset-1",
        "owner_id": "owner",
        "metadata": {
            "label": "safe",
            "api_key": "nested-api-secret",
            "items": [{"private_key": "nested-private-secret", "value": 1}],
        },
        "thumbnail_url": "https://signed.example.invalid/file?token=leak",
    }]

    result = service.execute(
        "owner",
        instance,
        "preview_artifacts",
        {"job_id": "job-1"},
    )

    assert result["artifacts"] == [{
        "id": "asset-1",
        "owner_id": "owner",
        "metadata": {"label": "safe", "items": [{"value": 1}]},
    }]
    assert "nested-api-secret" not in repr(result)
    assert "signed.example.invalid" not in repr(result)
