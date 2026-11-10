"""Offline compatibility regressions for legacy routes and account-pool wiring.

These tests intentionally never contact Google, Colab, CUDA, or OAuth.  The two
red tests document upgrade bugs found in the current production code; the
passing tests pin the legacy default-slot and Drive-directory safety boundary.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from app import compute_routes
from app import main as main_module
from app.common import APIError
from app.config import settings
from app.gpu.accounts import AccountService
from app.gpu.compute import GpuConfig
from app.gpu.drive import DriveStorage
from app.security import encrypt_secret

OWNER = "compat-owner"
SECONDARY = "account-secondary"
COLAB_SCOPE = "https://www.googleapis.com/auth/colaboratory"


class MemoryRepo:
    def __init__(self):
        self.rows = {
            "accounts": [],
            "credentials": [],
            "files": [],
            "jobs": [],
            "events": [],
            "operations": [],
        }

    def create(self, kind, owner_id, data, record_id=None, **_kwargs):
        row = {"id": record_id or f"{kind}-{len(self.rows[kind]) + 1}", "owner_id": owner_id, **data}
        self.rows.setdefault(kind, []).append(row)
        return dict(row)

    def get(self, kind, record_id, owner_id=None):
        return next(
            (
                dict(row)
                for row in self.rows.get(kind, [])
                if row.get("id") == record_id
                and (owner_id is None or row.get("owner_id") == owner_id)
            ),
            None,
        )

    def list(self, kind, owner_id=None, **_kwargs):
        return [
            dict(row)
            for row in self.rows.get(kind, [])
            if owner_id is None or row.get("owner_id") == owner_id
        ]

    def update(self, kind, record_id, patch, owner_id=None):
        for row in self.rows.get(kind, []):
            if row.get("id") == record_id and (owner_id is None or row.get("owner_id") == owner_id):
                row.update(patch)
                return dict(row)
        raise KeyError(record_id)


class SmokeStore:
    def __init__(self):
        self.rows = {"accounts": [], "projects": [], "jobs": [], "events": []}

    @contextmanager
    def dispatch_lock(self):
        yield True

    def get(self, kind, record_id, owner_id=None):
        return next(
            (
                dict(row)
                for row in self.rows.get(kind, [])
                if row.get("id") == record_id
                and (owner_id is None or row.get("owner_id") == owner_id)
            ),
            None,
        )

    def create(self, kind, owner_id, data, record_id=None, **_kwargs):
        row = {"id": record_id or f"{kind}-{len(self.rows[kind]) + 1}", "owner_id": owner_id, **data}
        self.rows.setdefault(kind, []).append(row)
        return dict(row)


class SmokeRepo:
    def __init__(self, store):
        self.store = store

    def list(self, kind, owner_id=None, **_kwargs):
        return [
            dict(row)
            for row in self.store.rows.get(kind, [])
            if owner_id is None or row.get("owner_id") == owner_id
        ]

    def get(self, kind, row_id, owner_id=None):
        return next((row for row in self.list(kind, owner_id) if row.get("id") == row_id), None)

    def create_job(self, owner_id, body, key):
        from app.gpu.manifest import digest

        row = {
            "id": "smoke-job-1",
            "owner_id": owner_id,
            "state": "queued",
            "idempotency_key": key,
            "body_hash": digest(body),
            **body,
        }
        self.store.rows["jobs"].append(row)
        return dict(row), True

    def update(self, kind, record_id, patch, owner_id=None):
        for row in self.store.rows.get(kind, []):
            if row.get("id") == record_id and (owner_id is None or row.get("owner_id") == owner_id):
                row.update(patch)
                return dict(row)
        raise KeyError(record_id)

    def event(self, job_id, owner_id, state, message):
        self.store.rows["events"].append(
            {"id": f"event-{len(self.store.rows['events']) + 1}", "job_id": job_id, "owner_id": owner_id, "state": state, "message": message}
        )


def request(request_id="compat"):
    return SimpleNamespace(
        headers={"Idempotency-Key": request_id},
        state=SimpleNamespace(request_id=request_id),
    )


def test_migrated_default_oauth_client_config_is_shared_with_new_account(monkeypatch):
    """A migrated owner app must still configure a newly-created account."""
    monkeypatch.delenv("GPU_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GPU_GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GPU_GOOGLE_SECRET", raising=False)
    repo = MemoryRepo()
    default_id = AccountService.default_account_id(OWNER)
    repo.create(
        "credentials",
        OWNER,
        {
            "provider": "google",
            "purpose": "colab",
            "encrypted_refresh_token": encrypt_secret("legacy-refresh"),
        },
        record_id="legacy-colab",
    )
    repo.create(
        "credentials",
        OWNER,
        {
            "provider": "google",
            "purpose": "oauth_client_config",
            "account_id": None,
            "client_id": "764086051850-cli.apps.googleusercontent.com",
            "encrypted_client_secret": encrypt_secret("legacy-client-secret"),
        },
        record_id="legacy-oauth-config",
    )

    # The real upgrade path binds all legacy credential rows to this account.
    AccountService(repo, OWNER).list()
    migrated = repo.get("credentials", "legacy-oauth-config", OWNER)
    assert migrated["account_id"] == default_id

    # Current code falls through to empty environment variables here instead of
    # reusing the same-owner migrated application configuration.
    from app.gpu.google import GoogleService

    client_id, client_secret, _redirect = GoogleService(repo, OWNER, account_id=SECONDARY)._config()
    assert client_id == "764086051850-cli.apps.googleusercontent.com"
    assert client_secret == "legacy-client-secret"


def test_legacy_settings_keeps_google_status_on_unscoped_default_slot(monkeypatch, tmp_path):
    """The old settings endpoint must not aggregate another account's token."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setenv("GPU_REAL_ENABLED", "1")
    repo = MemoryRepo()
    monkeypatch.setattr(main_module, "Repository", lambda _store: repo)
    repo.create(
        "credentials",
        OWNER,
        {
            "provider": "google",
            "purpose": "colab",
            "account_id": SECONDARY,
            "encrypted_access_token": encrypt_secret("secondary-access"),
            "encrypted_refresh_token": encrypt_secret("secondary-refresh"),
            "expires_at": 9_999_999_999,
            "scope": COLAB_SCOPE,
        },
    )
    response = main_module.get_settings(request(), {"id": OWNER, "role": "admin"}, repo)
    payload = json.loads(response.body)["data"]
    assert payload["google"]["colab"]["connected"] is False


def test_legacy_specs_and_subscription_without_account_id_do_not_select_secondary(monkeypatch):
    """The old unscoped routes remain bound to the legacy/default identity."""
    monkeypatch.setenv("GPU_REAL_ENABLED", "1")
    captured = []

    class Provider:
        def __init__(self, _repo, _owner, *, account_id=None):
            captured.append(account_id)

        def list_specs(self):
            return []

        def subscription(self):
            return {"name": "subscription"}

    monkeypatch.setattr("app.gpu.colab.GoogleColabProvider", Provider)
    monkeypatch.setattr(main_module, "Repository", lambda _store: MemoryRepo())
    store = object()
    main_module.compute_specs(request(), provider="colab", account_id=None, user={"id": OWNER}, st=store)
    main_module.subscription(request(), account_id=None, user={"id": OWNER}, st=store)
    assert captured == [None, None]


def test_cuda_smoke_routes_explicit_account_to_google_provider_and_job(monkeypatch):
    """Account-pool CUDA smoke must not silently use the legacy OAuth slot."""
    monkeypatch.setattr(
        compute_routes.GpuConfig,
        "from_env",
        lambda: GpuConfig(real_enabled=True, unit_rate=1.0, max_runtime_minutes=5),
    )
    seen = {}

    class Google:
        def __init__(self, _repo, _owner, *, account_id=None):
            seen["google_account_id"] = account_id

        def status(self):
            return {"colab": {"connected": True}}

    class Provider:
        def __init__(self, _repo, _owner, *, account_id=None):
            seen["provider_account_id"] = account_id

        def list_specs(self):
            return [
                {
                    "key": {"variant": "VARIANT_GPU", "accelerator": "L4", "shape": "SHAPE_STANDARD"},
                    "eligible": True,
                }
            ]

    monkeypatch.setattr(compute_routes, "GoogleService", Google)
    monkeypatch.setattr(compute_routes, "GoogleColabProvider", Provider)
    store = SmokeStore()
    monkeypatch.setattr(compute_routes, "Repository", SmokeRepo)
    store.rows["accounts"].append(
        {
            "id": SECONDARY,
            "account_id": SECONDARY,
            "owner_id": OWNER,
            "enabled": True,
            "allow_allocation": True,
        }
    )
    body = {
        "confirmed": True,
        "account_id": SECONDARY,
        "spec": {"variant": "VARIANT_GPU", "accelerator": "L4", "shape": "SHAPE_STANDARD"},
    }

    response = compute_routes.cuda_smoke(body, request("smoke-account"), {"id": OWNER, "role": "admin"}, store)
    payload = json.loads(response.body)["data"]
    job = store.rows["jobs"][0]

    assert response.status_code == 202
    assert payload["job_id"] == job["id"]
    assert seen["google_account_id"] == SECONDARY
    assert seen["provider_account_id"] == SECONDARY
    assert job["account_id"] == SECONDARY


def test_selected_account_must_be_owned_and_enabled():
    """Explicit smoke identities cannot cross owners or use disabled accounts."""
    repo = MemoryRepo()
    repo.create("accounts", OWNER, {"account_id": SECONDARY, "enabled": False}, record_id=SECONDARY)

    with pytest.raises(APIError) as disabled:
        compute_routes._require_account(repo, OWNER, SECONDARY)
    assert disabled.value.code == "ACCOUNT_DISABLED"

    with pytest.raises(APIError) as foreign:
        compute_routes._require_account(repo, "other-owner", SECONDARY)
    assert foreign.value.code == "ACCOUNT_NOT_FOUND"


def test_legacy_drive_root_and_file_rows_are_reused_without_account_id():
    """Pre-account Drive metadata remains visible to the legacy storage path."""
    content = b"legacy output"
    key = "outputs/legacy/result.bin"
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    sha256 = hashlib.sha256(content).hexdigest()
    repo = MemoryRepo()
    repo.create("files", OWNER, {"kind": "drive_root", "remote_id": "old-root"}, record_id="old-root-row")
    repo.create(
        "files",
        OWNER,
        {
            "kind": "drive_file",
            "key_hash": key_hash,
            "sha256": sha256,
            "remote_id": "old-file",
            "name": "result.bin",
            "size": len(content),
            "mime": "application/octet-stream",
            "state": "verified",
        },
        record_id="old-file-row",
    )
    storage = DriveStorage(
        {"id": "legacy-job", "owner_id": OWNER},
        repo,
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: pytest.fail("Drive network used"))),
        google=SimpleNamespace(),
    )
    assert storage._root_folder() == "old-root"
    reused = storage.put(key, content, "application/octet-stream")
    assert reused["id"] == "old-file"
    assert reused["verified"] is True
