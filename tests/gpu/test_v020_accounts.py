from __future__ import annotations

import hashlib
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from app.common import APIError
from app.gpu.accounts import DEFAULT_ACCOUNT_NAMESPACE, AccountService
from app.gpu.compute import GpuConfig, default_executor_factory, default_provider_factory
from app.gpu.drive import DriveStorage
from app.gpu.google import GoogleService
from app.security import decrypt_secret, encrypt_secret
from fastapi import FastAPI
from fastapi.testclient import TestClient


class MemoryRepo:
    def __init__(self):
        self.rows = {"credentials": [], "accounts": [], "instances": [], "leases": [], "operations": [], "files": []}

    def create(self, kind, owner_id, data, record_id=None, **_):
        row = {"id": record_id or f"{kind}-{len(self.rows.setdefault(kind, [])) + 1}", "owner_id": owner_id, **data}
        self.rows.setdefault(kind, []).append(row)
        return dict(row)

    def get(self, kind, record_id, owner_id=None):
        return next((dict(row) for row in self.rows.get(kind, []) if row["id"] == record_id and (owner_id is None or row["owner_id"] == owner_id)), None)

    def list(self, kind, owner_id=None, **_):
        return [dict(row) for row in self.rows.get(kind, []) if owner_id is None or row["owner_id"] == owner_id]

    def update(self, kind, record_id, patch, owner_id=None):
        for row in self.rows.get(kind, []):
            if row["id"] == record_id and (owner_id is None or row["owner_id"] == owner_id):
                row.update(patch)
                return dict(row)
        raise KeyError(record_id)

    def delete(self, kind, record_id, owner_id=None):
        before = len(self.rows.get(kind, []))
        self.rows[kind] = [row for row in self.rows.get(kind, []) if not (row["id"] == record_id and (owner_id is None or row["owner_id"] == owner_id))]
        return len(self.rows[kind]) != before


def configured(monkeypatch):
    monkeypatch.setenv("GPU_GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GPU_GOOGLE_CLIENT_SECRET", "client-secret")


def test_explicit_account_oauth_isolated_and_callback_reports_account(monkeypatch):
    configured(monkeypatch)
    repo = MemoryRepo()
    service = GoogleService(repo, "owner-1", account_id="account-a")
    started = service.start("drive")
    row = repo.rows["credentials"][0]
    assert row["account_id"] == "account-a"
    assert parse_qs(urlparse(started["authorization_url"]).query)["scope"] == ["https://www.googleapis.com/auth/drive.file"]

    def handler(request):
        return httpx.Response(200, json={"access_token": "access-a", "refresh_token": "refresh-a", "expires_in": 3600})

    result = GoogleService(repo, "owner-1", account_id=None, client=httpx.Client(transport=httpx.MockTransport(handler))).callback("code", started["state"])
    assert result["account_id"] == "account-a"
    credential = next(row for row in repo.rows["credentials"] if row.get("purpose") == "drive")
    assert credential["account_id"] == "account-a"
    assert decrypt_secret(credential["encrypted_access_token"]) == "access-a"
    assert GoogleService(repo, "owner-1", account_id="account-b").status()["drive"]["connected"] is False


def test_explicit_account_refresh_does_not_use_other_account(monkeypatch):
    configured(monkeypatch)
    repo = MemoryRepo()
    for account, token in (("account-a", "a"), ("account-b", "b")):
        repo.create("credentials", "owner-1", {"provider": "google", "purpose": "drive", "account_id": account, "encrypted_access_token": encrypt_secret(token), "encrypted_refresh_token": encrypt_secret("refresh-" + account), "expires_at": time.time() - 1})
    seen = []

    def handler(request):
        seen.append(parse_qs(request.content.decode())["refresh_token"][0])
        return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})

    assert GoogleService(repo, "owner-1", account_id="account-b", client=httpx.Client(transport=httpx.MockTransport(handler))).access_token("drive") == "fresh"
    assert seen == ["refresh-account-b"]


def test_provider_and_executor_factories_route_account_id(monkeypatch):
    captured = {}

    class FakeProvider:
        def __init__(self, store, owner_id, *, account_id):
            captured["provider"] = {"store": store, "owner_id": owner_id, "account_id": account_id}

    class FakeExecutor:
        @classmethod
        def from_environment(cls, **kwargs):
            captured["executor"] = kwargs
            return cls()

    monkeypatch.setattr("app.gpu.colab.GoogleColabProvider", FakeProvider)
    monkeypatch.setattr("app.gpu.mcp.MCPGPUExecutor", FakeExecutor)
    monkeypatch.setenv("STUDIO_OFFLINE_TEST", "0")
    config = GpuConfig(real_enabled=True)
    job = {"provider": "colab", "owner_id": "owner-1", "account_id": "account-a", "execution_policy": {"strategy": "existing"}}
    default_provider_factory(job, config, store="repo")
    default_executor_factory(job, config, store="repo")
    assert captured["provider"] == {"store": "repo", "owner_id": "owner-1", "account_id": "account-a"}
    assert captured["executor"] == {"store": "repo", "owner_id": "owner-1", "account_id": "account-a"}


def test_drive_file_cache_isolated_by_account():
    repo = MemoryRepo()
    for account, token in (("account-a", "token-a"), ("account-b", "token-b")):
        repo.create("credentials", "owner-1", {"provider": "google", "purpose": "drive", "account_id": account, "encrypted_access_token": encrypt_secret(token), "encrypted_refresh_token": encrypt_secret("refresh"), "expires_at": 9999999999})
    content = b"same bytes"
    digest = hashlib.sha256(content).hexdigest()
    repo.create("files", "owner-1", {"kind": "drive_file", "account_id": "account-a", "key_hash": hashlib.sha256(b"outputs/x.bin").hexdigest(), "sha256": digest, "remote_id": "remote-a", "name": "x.bin", "size": len(content), "mime": "application/octet-stream", "state": "verified"})
    storage = DriveStorage({"id": "job", "owner_id": "owner-1", "account_id": "account-b"}, repo, account_id="account-b", client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))))
    with pytest.raises(Exception):
        storage.put("outputs/x.bin", content, "application/octet-stream")
    assert storage.google.access_token("drive") == "token-b"


def test_account_service_whitelist_default_migration_and_disconnect_guard():
    repo = MemoryRepo()
    repo.create("credentials", "owner-1", {"provider": "google", "purpose": "drive", "encrypted_access_token": "x", "encrypted_refresh_token": "y"})
    service = AccountService(repo, "owner-1")
    rows = service.list()
    assert len(rows) == 1
    assert rows[0]["account_id"] == str(__import__("uuid").uuid5(DEFAULT_ACCOUNT_NAMESPACE, "colab-gpu-studio:v0.2.0:legacy-account:owner-1"))
    assert "encrypted_access_token" not in str(rows)
    account = service.create({"alias": "实验", "projects": ["p1"], "workflows": ["wf"], "budget": {"daily": 2}, "allow_allocation": True})
    assert set(account) <= set(AccountService.PUBLIC_FIELDS)
    repo.create("instances", "owner-1", {"account_id": account["account_id"], "state": "ready", "current_job_id": None})
    with pytest.raises(APIError, match="资源"):
        service.disconnect(account["account_id"])


def test_account_route_requires_idempotency_and_is_idempotent():
    from app.account_routes import router

    repo = MemoryRepo()
    app = FastAPI()
    app.state.store = repo
    app.include_router(router)

    from app.account_routes import get_current_user_for_tests
    app.dependency_overrides[get_current_user_for_tests] = lambda: {"id": "owner-1", "role": "admin"}
    client = TestClient(app)
    missing = client.post("/api/gpu/accounts", json={"alias": "one"})
    assert missing.status_code == 422
    headers = {"Idempotency-Key": "create-one"}
    first = client.post("/api/gpu/accounts", headers=headers, json={"alias": "one"})
    second = client.post("/api/gpu/accounts", headers=headers, json={"alias": "one"})
    assert first.status_code == second.status_code == 201
    assert first.json()["data"]["account_id"] == second.json()["data"]["account_id"]
    conflict = client.post("/api/gpu/accounts", headers=headers, json={"alias": "two"})
    assert conflict.status_code == 409
