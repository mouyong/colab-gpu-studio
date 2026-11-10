"""Regressions observed on a Windows coordinator with account-scoped OAuth."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from app import admission, review_routes
from app.common import APIError
from app.db import Store
from app.gpu import accounts as account_module
from app.gpu.colab import ColabAllocationError, ColabError, GoogleColabProvider
from app.gpu.compute import MockColabProvider, MockGPUExecutor
from app.gpu.repository import Repository
from app.gpu.scheduler import Scheduler


@pytest.mark.parametrize("operation,http_status,deleted,unknown", [
    ({"done": True, "error": {"code": 9}}, 404, True, False),
    ({"done": False}, 404, False, False),
    (None, 404, False, True),
    ({"done": True, "error": {"code": 9}}, 200, False, False),
])
def test_allocation_failure_cleanup_evidence(operation, http_status, deleted, unknown):
    provider = GoogleColabProvider.__new__(GoogleColabProvider)
    provider._owned = lambda handle: {"create_attempts": 1, "operation_done": False}
    provider._is_cli_tunnel = lambda lease: False
    provider._operation = lambda handle, lease: operation
    provider._request = lambda method, path: SimpleNamespace(status_code=http_status)
    status = provider.inspect({"runtime_name": "runtimes/owned"})
    assert status["deleted"] is deleted
    assert bool(status.get("allocation_unknown")) is unknown
    assert bool(status.get("allocation_failed")) is bool(operation and operation.get("error"))


def test_capacity_is_distinct_from_disappeared_runtime_and_raw_errors_stay_private():
    operation = {"done": True, "error": {"code": 9, "message": "PRIVATE_CREDENTIAL", "details": [{
        "@type": "type.googleapis.com/google.rpc.ErrorInfo", "domain": "colaboratory.googleapis.com", "reason": "NO_RUNTIMES",
    }]}}
    status = {"deleted": True, **GoogleColabProvider._operation_failure(operation)}
    scheduler = Scheduler.__new__(Scheduler)
    scheduler._lease = lambda job: {"handle": {}}
    scheduler._lease_handle = lambda lease: lease["handle"]
    scheduler._provider = lambda job: SimpleNamespace(inspect=lambda handle: status)
    scheduler._begin_failure = lambda job, code, message: {"code": code, "message": message}
    # _prepare requires a non-empty handle, independently of the provider response.
    scheduler._lease = lambda job: {"handle": {"runtime_name": "runtimes/owned"}}
    result = scheduler._prepare({"id": "owned"})
    assert result["code"] == "gpu_capacity_unavailable"
    assert "PRIVATE_CREDENTIAL" not in result["message"]
    with pytest.raises(ColabError, match="NO_RUNTIMES") as error:
        GoogleColabProvider._raise_operation_error(operation)
    assert "PRIVATE_CREDENTIAL" not in str(error.value)
    operation["error"]["details"][0]["reason"] = "OTHER https://private.example/PRIVATE_CREDENTIAL"
    with pytest.raises(ColabError) as error:
        GoogleColabProvider._raise_operation_error(operation)
    assert "PRIVATE_CREDENTIAL" not in str(error.value)


def test_registration_failure_releases_owned_runtime_without_starting_executor(monkeypatch):
    repo = Repository(Store("sqlite://"))
    job = repo.create("jobs", "owner", {"state": "loading", "provider": "mock", "max_runtime_minutes": 1})
    provider, executor = MockColabProvider(), MockGPUExecutor()
    handle = provider.create({}, "request", job["id"], "runtime")
    lease = repo.create("leases", "owner", {"state": "active", "owner_job_id": job["id"], "handle": handle})
    job = repo.update("jobs", job["id"], {"lease_id": lease["id"]}, "owner")
    def refuse(*args):
        raise ValueError("lease is not active")
    monkeypatch.setattr(repo, "prepare_execution", refuse)
    scheduler = Scheduler(repo, provider_factory=lambda job: provider, executor_factory=lambda job: executor)
    result = scheduler._load(job)
    assert result["state"] == "cleaning_up"
    assert result["error_code"] == "gpu_execution_prepare_failed"
    assert not executor.started
    result = scheduler._cleanup(result)
    assert result["state"] == "failed" and provider.deleted
    assert result["error_code"] == "gpu_execution_prepare_failed"


def test_immediate_allocation_failure_is_classified_and_absence_is_confirmed():
    repo = Repository(Store("sqlite://"))
    job = repo.create("jobs", "owner", {"state": "allocating", "provider": "mock", "max_runtime_minutes": 1})
    class Provider(MockColabProvider):
        def create(self, *args):
            raise ColabAllocationError(9, "NO_RUNTIMES")
        def inspect(self, handle):
            return {"deleted": True, "allocation_failed": True}
    scheduler = Scheduler(repo, provider_factory=lambda job: Provider())
    failed = scheduler._allocation(job)
    assert failed["state"] == "cleaning_up" and failed["error_code"] == "gpu_capacity_unavailable"
    assert scheduler._cleanup(failed)["state"] == "failed"
    assert repo.list("leases", "owner")[0]["state"] == "deleted"


@pytest.mark.parametrize("state", ["cleanup_pending", "releasing", "deleting", "quarantined", "deleted"])
def test_fenced_allocation_never_reuses_handle(state):
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.db_store = SimpleNamespace(dispatch_lock=lambda: nullcontext(True))
    scheduler._ensure_lease = lambda job: {"id": "lease", "state": state, "handle": {"runtime_name": "runtimes/owned"}}
    scheduler._begin_failure = lambda job, code, message: {"state": "cleaning_up", "error_code": code}
    result = scheduler._allocation({"id": "job", "owner_id": "owner", "lease_id": "lease"})
    assert result == {"state": "cleaning_up", "error_code": "gpu_allocation_aborted"}


@pytest.fixture
def scoped_accounts(monkeypatch):
    rows = [{"id": "owned", "account_id": "owned", "owner_id": "owner", "enabled": True, "allow_allocation": True}]
    connected = {"owned"}
    class Repo:
        def list(self, kind, owner):
            return [row for row in rows if row["owner_id"] == owner]
        def get(self, kind, key, owner):
            return next((row for row in rows if row["id"] == key and row["owner_id"] == owner), None)
    def status(repo, owner, account):
        assert account is not None
        return {"colab": {"connected": account in connected}}
    monkeypatch.setattr(admission, "_google_status", status)
    return Repo(), rows, connected


def test_auto_account_is_bound_before_scoped_oauth_and_foreign_accounts_are_excluded(scoped_accounts):
    repo, rows, connected = scoped_accounts
    policy = {"provider": "colab", "allocation": "auto", "account_id": None, "instance_id": None}
    rows.append({**rows[0], "id": "foreign", "account_id": "foreign", "owner_id": "other"})
    connected.add("foreign")
    assert admission._validate_allocation(repo, {"id": "owner"}, policy)["account_id"] == "owned"
    with pytest.raises(APIError):
        admission._validate_allocation(repo, {"id": "owner"}, {**policy, "account_id": "foreign"})
    rows.append({**rows[0], "id": "second", "account_id": "second"})
    connected.add("second")
    assert admission._validate_allocation(repo, {"id": "owner"}, policy)["account_id"] is None


@pytest.mark.parametrize("flag", ["enabled", "allow_allocation"])
def test_auto_account_requires_enabled_allocation(scoped_accounts, flag):
    repo, rows, _ = scoped_accounts
    rows[0][flag] = False
    with pytest.raises(APIError):
        admission._validate_allocation(repo, {"id": "owner"}, {"provider": "colab", "allocation": "auto", "account_id": None})


def test_prepare_preserves_explicit_compute_and_shared_drive_identity(monkeypatch):
    calls = []
    class Accounts:
        def __init__(self, repo, owner):
            pass
        def resolve_drive_account_id(self, account):
            return "drive"
    class Google:
        def __init__(self, repo, owner, *, account_id):
            calls.append(account_id)
            self.account = account_id
        def status(self):
            return {"colab": {"connected": self.account == "compute"}, "drive": {"connected": self.account == "drive"}}
    monkeypatch.setattr(account_module, "AccountService", Accounts)
    monkeypatch.setattr(review_routes, "GoogleService", Google)
    monkeypatch.setattr(review_routes, "_require_account", lambda repo, owner, account: None)
    assert review_routes._prepare_account_ids(None, "owner", "compute") == ("compute", "drive")
    assert calls == ["compute", "drive"]


def test_workflow_sync_uses_workflow_identity_and_deduplicates_shared_drive(monkeypatch):
    from app import main
    from app.gpu import drive, google

    row = {"id": "workflow-id", "workflow_key": "example", "workflow_version": 1}
    seen = []
    class Accounts:
        def __init__(self, repo, owner):
            pass
        def list(self):
            return [{"account_id": name, "authorization": {"drive": {"connected": True}}} for name in ("compute-a", "compute-b")]
        def resolve_drive_account_id(self, account):
            return "shared-drive"
    class Google:
        def __init__(self, *args):
            pass
        def status(self):
            return {"drive": {"connected": False}}
    class Storage:
        def __init__(self, job, repo):
            seen.append(job)
        def put(self, name, content, mime):
            return {"name": name}
        def verify(self, record, content):
            return True
    repo = SimpleNamespace(update=lambda kind, rid, patch, owner: {**row, **patch})
    monkeypatch.setenv("GPU_STORAGE", "drive")
    monkeypatch.setattr(main, "Repository", lambda store: repo)
    monkeypatch.setattr(account_module, "AccountService", Accounts)
    monkeypatch.setattr(google, "GoogleService", Google)
    monkeypatch.setattr(drive, "DriveStorage", Storage)
    result = main.cloud_sync_workflow(None, "owner", row)
    assert len(result["drive_files"]) == 4
    assert seen == [{"id": "workflow-id", "owner_id": "owner", "account_id": "shared-drive", "drive_account_id": "shared-drive"}]
