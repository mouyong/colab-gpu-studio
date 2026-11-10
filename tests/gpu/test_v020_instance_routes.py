from __future__ import annotations

from dataclasses import dataclass, field

from app.common import APIError, current_user, envelope
from app.instance_routes import router
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


@dataclass
class FakeInstanceService:
    instances: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    def public(self, instance):
        return {**instance, "handle": "do-not-leak", "connection_token": "secret"}

    def get(self, owner, instance_id):
        row = self.instances.get(instance_id)
        return row if row and row.get("owner_id") == owner else None

    def list(self, owner, state=None):
        return [row for row in self.instances.values() if row.get("owner_id") == owner
                and (state is None or row.get("state") == state)]

    def events(self, owner, instance_id):
        return []

    def retention(self, owner, instance_id, mode, idle_minutes, key, expected_version=None):
        self.calls.append(("retention", owner, instance_id, mode, idle_minutes, key, expected_version))
        row = self.instances[instance_id]
        row.update(retention_mode=mode, idle_minutes=idle_minutes, version=row.get("version", 0) + 1)
        return {**row, "operation": "retention-started"}

    def stop(self, owner, instance_id, key):
        self.calls.append(("stop", owner, instance_id, key))
        row = self.instances[instance_id]
        row["state"] = "releasing"
        return {**row, "operation": "stop-requested"}


class FakeRepo:
    def __init__(self, instances, jobs=None, events=None):
        self.instances = instances
        self.jobs = jobs or []
        self.events = events or []

    def list(self, kind, owner_id=None, project_id=None):
        rows = {"instances": self.instances, "jobs": self.jobs, "events": self.events}.get(kind, [])
        return [r for r in rows if owner_id is None or r.get("owner_id") == owner_id]

    def get(self, kind, record_id, owner_id=None):
        for row in self.list(kind, owner_id):
            if row["id"] == record_id:
                return row
        return None


def _client(service, repo):
    app = FastAPI()
    @app.exception_handler(APIError)
    async def api_error(_request: Request, exc: APIError):
        return JSONResponse(envelope(error={"code": exc.code, "message": exc.message}), status_code=exc.status)

    app.state.instance_service = service
    app.state.repo = repo
    app.dependency_overrides[current_user] = lambda: {"id": "owner", "role": "user"}
    app.include_router(router)
    return TestClient(app)


def test_instance_routes_are_owner_scoped_and_public_redacts_runtime_secrets():
    rows = [
        {"id": "i1", "owner_id": "owner", "state": "retained_idle", "version": 3, "label": "GPU-01"},
        {"id": "i2", "owner_id": "other", "state": "ready", "label": "other"},
    ]
    service = FakeInstanceService({"i1": rows[0], "i2": rows[1]})
    with _client(service, FakeRepo(rows)) as client:
        response = client.get("/api/gpu/instances")
    assert response.status_code == 200
    data = response.json()["data"]
    assert [row["id"] for row in data] == ["i1"]
    assert "handle" not in data[0]
    assert "connection_token" not in data[0]


def test_retention_and_stop_are_accepted_as_operations_not_fake_deletion():
    row = {"id": "i1", "owner_id": "owner", "state": "ready", "version": 4}
    service = FakeInstanceService({"i1": row})
    with _client(service, FakeRepo([row])) as client:
        retained = client.patch(
            "/api/gpu/instances/i1/retention",
            json={"mode": "retain", "idle_minutes": 10, "key": "r-1", "expected_version": 4},
        )
        stopped = client.post("/api/gpu/instances/i1/stop", json={"key": "s-1", "confirmed": True})
    assert retained.status_code == 202
    assert retained.json()["data"]["operation"] == "retention-started"
    assert stopped.status_code == 202
    assert stopped.json()["data"]["state"] == "releasing"
    assert stopped.json()["data"]["state"] != "deleted"
    assert service.calls[0][-1] == 4


def test_stop_requires_explicit_confirmation_and_key():
    row = {"id": "i1", "owner_id": "owner", "state": "ready", "version": 1}
    service = FakeInstanceService({"i1": row})
    with _client(service, FakeRepo([row])) as client:
        response = client.post("/api/gpu/instances/i1/stop", json={"confirmed": False})
    assert response.status_code == 422
    assert service.calls == []
