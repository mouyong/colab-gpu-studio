"""Focused regressions for the final independent review edges."""

from app.db import Store
from app.gpu.compute import GpuConfig
from app.gpu.repository import Repository
from app.gpu.scheduler import Scheduler


def _orphan_repo():
    repo = Repository(Store("sqlite:///:memory:"))
    instance = repo.create(
        "instances",
        "owner",
        {"state": "releasing", "account_id": "acct"},
        record_id="instance",
    )
    lease = repo.create(
        "leases",
        "owner",
        {
            "state": "cleanup_pending",
            "owner_job_id": "missing-job",
            "instance_id": instance["id"],
            "account_id": "acct",
            "request_id": "request",
            "runtime_id": "runtime",
            "handle": {"runtime_id": "runtime", "job_id": "missing-job", "owner_job_id": "missing-job"},
        },
        record_id="lease",
    )
    return repo, instance, lease


def test_failed_orphan_finish_releases_instance_cleanup_fence():
    repo, instance, lease = _orphan_repo()
    assert repo.claim_orphan_lease("owner", lease["id"], instance["id"], "token", 100.0)

    assert repo.finish_orphan_lease(
        "owner",
        lease["id"],
        instance["id"],
        "token",
        101.0,
        confirmed=False,
        error="provider unavailable",
        attempts=1,
    ) is True

    saved_instance = repo.get("instances", instance["id"], "owner")
    saved_lease = repo.get("leases", lease["id"], "owner")
    assert saved_instance.get("cleanup_claim_token") is None
    assert saved_instance.get("cleanup_claimed_at") is None
    assert saved_lease.get("cleanup_claim_token") is None


def test_failed_orphan_cleanup_uses_exponential_backoff_for_string_errors():
    repo, instance, lease = _orphan_repo()

    class Provider:
        def inspect(self, handle):
            raise RuntimeError("temporary provider outage")

        def delete(self, handle):
            raise AssertionError("inspect failure should prevent delete")

    scheduler = Scheduler(
        repo,
        provider_factory=lambda _: Provider(),
        config=GpuConfig(cleanup_retry_base_seconds=10.0, cleanup_retry_max_seconds=100.0),
        clock=lambda: 100.0,
        instance_service=None,
    )
    assert scheduler._cleanup_orphan_lease(lease) is False

    saved = repo.get("leases", lease["id"], "owner")
    assert saved["delete_attempts"] == 1
    assert saved["cleanup_next_epoch"] == 110.0
    assert saved["state"] == "cleanup_pending"


def test_deleted_reconciliation_requires_lease_instance_identity_match():
    class Service:
        instances = [
            {
                "id": "instance-current",
                "owner_id": "owner",
                "state": "releasing",
                "lease_id": "lease-deleted",
                "account_id": "acct",
            }
        ]

        def mark_deleted(self, *args, **kwargs):
            raise AssertionError("must not use evidence from a different instance")

    lease = {
        "id": "lease-deleted",
        "owner_id": "owner",
        "instance_id": "instance-other",
        "account_id": "acct",
        "state": "deleted",
        "remote_deleted": True,
        "remote_delete_source": "cleanup_provider_confirmation",
    }
    repo = Repository(Store("sqlite:///:memory:"))
    repo.create("jobs", "owner", {"state": "completed"}, record_id="job")
    service = Service()
    service.list = lambda owner: Service.instances
    scheduler = Scheduler(repo, instance_service=service)

    assert scheduler._reconcile_deleted_lease_instances([lease]) == 0


def test_admission_lock_remains_distinct_inside_dispatch_lock():
    store = Store("sqlite:///:memory:")
    with store.dispatch_lock() as dispatch_acquired:
        assert dispatch_acquired
        with store.admission_lock(timeout=0.05) as admission_acquired:
            assert admission_acquired


def test_handoff_is_blocked_by_admission_lock():
    repo = Repository(Store("sqlite:///:memory:"))
    body = {
        "project_id": "p",
        "snapshot": {"manifest": {"mock": True}},
        "execution_policy": {"provider": "mock"},
    }
    repo.create_batch("owner", [body, body], "handoff-lock", {"mode": "batch"})
    first, following = sorted(repo.list("jobs", "owner"), key=lambda row: row["queue_order"])
    lease = repo.create(
        "leases",
        "owner",
        {
            "state": "active",
            "owner_job_id": first["id"],
            "deadline_epoch": 9999.0,
            "compatibility_signature": first["compatibility_signature"],
            "handle": {"runtime_id": "runtime", "job_id": first["id"], "owner_job_id": first["id"]},
        },
        record_id="handoff-lease",
    )
    repo.update("jobs", first["id"], {"state": "cleaning_up", "lease_id": lease["id"]}, "owner")
    repo.update("jobs", following["id"], {"state": "waiting_for_capacity"}, "owner")

    store = repo.store
    with store.admission_lock() as acquired:
        assert acquired
        assert repo.handoff_lease("owner", first["id"], following["id"], lease["id"], 1.0) is False

    assert repo.get("leases", lease["id"], "owner")["owner_job_id"] == first["id"]
