from app.common import APIError, current_user
from app.db import Store
from app.gpu.repository import Repository
from app.history_routes import router
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


def api_error_handler(request, exc):
    return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)


class Repo:
    def __init__(self):
        self.rows = {
            "instances": [{"id": "instance-1", "owner_id": "owner", "state": "deleted"}],
            "jobs": [
                {
                    "id": "job-1",
                    "owner_id": "owner",
                    "instance_id": "instance-1",
                    "state": "failed",
                    "provider": "colab",
                    "error_code": "gpu_failed",
                    "error_message": "failed https://private.example/token?x=secret",
                    "worker_timings": {"sampling": 4.2},
                    "output_asset_ids": ["asset-1"],
                },
                {"id": "job-other", "owner_id": "other", "instance_id": "instance-1", "state": "completed"},
            ],
            "events": [
                {"id": "event-1", "owner_id": "owner", "job_id": "job-1", "state": "failed", "created_at": "2026-11-08T00:00:02Z", "message": "failed https://private.example/run"},
                {"id": "event-other", "owner_id": "other", "job_id": "job-1", "state": "failed", "message": "cross-owner"},
            ],
            "assets": [{"id": "asset-1", "owner_id": "owner", "filename": "result.png", "mime": "image/png", "storage_key": "private/key"}],
        }

    def get(self, kind, record_id, owner_id=None):
        return next((row for row in self.rows.get(kind, []) if row.get("id") == record_id and (owner_id is None or row.get("owner_id") == owner_id)), None)

    def list(self, kind, owner_id=None, project_id=None):
        if kind == "assets":
            raise ValueError("assets belong to Store, not the GPU Repository")
        return [row for row in self.rows.get(kind, []) if owner_id is None or row.get("owner_id") == owner_id]


class AssetStore:
    def list(self, kind, owner_id=None):
        assert kind == "assets"
        return Repo().rows["assets"] if owner_id == "owner" else []


def test_history_is_owner_scoped_persisted_only_and_redacts_error_logs():
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)
    app.state.repo = Repo()
    app.state.store = AssetStore()
    app.dependency_overrides[current_user] = lambda: {"id": "owner", "role": "user"}
    app.include_router(router)

    with TestClient(app) as client:
        response = client.get("/api/gpu/instances/instance-1/history")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["offline"] is True
    assert [job["job_id"] for job in data["jobs"]] == ["job-1"]
    job = data["jobs"][0]
    assert job["error"]["code"] == "gpu_failed"
    assert job["timing_details"]["sampling"] == {
        "seconds": 4.2,
        "source": "worker",
        "status": "completed",
    }
    assert job["logs"] == job["events"]
    assert "https://" not in str(job)
    assert "token" not in str(job).lower()
    assert job["artifacts"][0]["filename"] == "result.png"
    assert "storage_key" not in str(job)


def test_history_requires_the_instance_to_belong_to_the_current_owner():
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)
    app.state.repo = Repo()
    app.state.store = AssetStore()
    app.dependency_overrides[current_user] = lambda: {"id": "other", "role": "user"}
    app.include_router(router)

    with TestClient(app) as client:
        response = client.get("/api/gpu/instances/instance-1/history")

    assert response.status_code == 404


def test_history_with_real_repository_reads_assets_from_store(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'history.sqlite'}")
    repo = Repository(store)
    repo.create("instances", "owner", {"state": "deleted"}, record_id="instance-real")
    repo.create("jobs", "owner", {
        "state": "completed", "instance_id": "instance-real",
        "output_asset_ids": ["owned-asset", "foreign-asset"],
    }, record_id="job-real")
    store.create("assets", "owner", {
        "filename": "owned.png", "mime": "image/png", "storage_key": "private/owned",
    }, record_id="owned-asset")
    store.create("assets", "other", {"filename": "foreign.png"}, record_id="foreign-asset")
    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)
    app.state.store = store
    app.state.repo = repo
    app.dependency_overrides[current_user] = lambda: {"id": "owner", "role": "user"}
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/api/gpu/instances/instance-real/history")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["offline"] and not data["remote_actions"]
    assert [a["id"] for a in data["jobs"][0]["artifacts"]] == ["owned-asset"]
    assert "storage_key" not in response.text
    assert "foreign.png" not in response.text
