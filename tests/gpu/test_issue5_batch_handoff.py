"""Issue #5 RED test: SQL batch handoff must not strand the old instance."""

import pytest
from app.db import Store
from app.gpu.instances import InstanceService
from app.gpu.repository import Repository
from app.gpu.scheduler import Scheduler


def test_sql_batch_handoff_does_not_leave_old_instance_releasing():
    """A successful handoff must transfer the durable instance binding atomically.

    The current scheduler returns immediately after ``handoff_lease`` succeeds,
    so ``_complete_instance`` is skipped.  This leaves the old SQL instance in
    ``releasing`` while the lease has already moved to the next batch job.
    """
    repo = Repository(Store("sqlite:///:memory:"))
    body = {
        "project_id": "project-1",
        "snapshot": {"manifest": {"mock": True}},
        "execution_policy": {"provider": "mock", "lifecycle": "save"},
    }
    repo.create_batch("owner-1", [body, body], "batch-handoff", {"mode": "batch"})
    first, successor = sorted(repo.list("jobs", "owner-1"), key=lambda row: row["queue_order"])

    instance_id = "instance-old"
    lease_id = "lease-old"
    repo.create(
        "instances",
        "owner-1",
        {
            # Handoff is only legal for a reusable instance; releasing is a
            # manual-stop/cleanup state and must stay fenced.
            "state": "busy",
            "account_id": "account-1",
            "current_job_id": first["id"],
            "lease_id": lease_id,
            "budget_reserved": 1.0,
        },
        record_id=instance_id,
    )
    repo.create(
        "leases",
        "owner-1",
        {
            "state": "active",
            "owner_job_id": first["id"],
            "instance_id": instance_id,
            "account_id": "account-1",
            "deadline_epoch": 10_000.0,
            "handle": {"runtime_id": "runtime-1", "job_id": first["id"], "owner_job_id": first["id"]},
        },
        record_id=lease_id,
    )
    execution_id = "execution-old"
    repo.create(
        "executions",
        "owner-1",
        {
            "state": "running",
            "job_id": first["id"],
            "lease_id": lease_id,
            "instance_id": instance_id,
        },
        record_id=execution_id,
    )
    first = repo.update(
        "jobs",
        first["id"],
        {
            "state": "cleaning_up",
            "cleanup_final_state": "completed",
            "instance_id": instance_id,
            "lease_id": lease_id,
            "execution_id": execution_id,
        },
        "owner-1",
    )
    successor = repo.update(
        "jobs",
        successor["id"],
        {"state": "waiting_for_capacity", "instance_id": None},
        "owner-1",
    )

    scheduler = Scheduler(
        repo,
        instance_service=InstanceService(repo, clock=lambda: 1_000.0),
        clock=lambda: 1_000.0,
    )
    scheduler._cleanup(first)

    moved_job = repo.get("jobs", successor["id"], "owner-1")
    moved_lease = repo.get("leases", lease_id, "owner-1")
    moved_instance = repo.get("instances", instance_id, "owner-1")

    assert moved_lease["owner_job_id"] == successor["id"]
    assert moved_lease["instance_id"] == instance_id
    assert moved_job["instance_id"] == instance_id
    assert moved_instance["current_job_id"] == successor["id"]
    assert moved_instance["state"] != "releasing"


def test_handoff_does_not_mutate_foreign_execution():
    repo = Repository(Store("sqlite:///:memory:"))
    body = {"project_id": "p", "snapshot": {"manifest": {"mock": True}}, "execution_policy": {"provider": "mock"}}
    repo.create_batch("owner", [body, body], "x", {"mode": "batch"})
    first, following = sorted(repo.list("jobs", "owner"), key=lambda row: row["queue_order"])
    instance = repo.create("instances", "owner", {"account_id": "acct", "state": "busy", "current_job_id": first["id"]})
    lease = repo.create("leases", "owner", {"state": "active", "owner_job_id": first["id"], "instance_id": instance["id"], "account_id": "acct", "deadline_epoch": 9999, "handle": {"job_id": first["id"], "owner_job_id": first["id"]}})
    foreign = repo.create("executions", "other-owner", {"state": "running", "job_id": "unrelated"})
    repo.update("jobs", first["id"], {"state": "cleaning_up", "execution_id": foreign["id"], "instance_id": instance["id"], "lease_id": lease["id"], "compatibility_signature": "same"}, "owner")
    repo.update("jobs", following["id"], {"state": "waiting_for_capacity", "compatibility_signature": "same"}, "owner")
    assert not repo.handoff_lease("owner", first["id"], following["id"], lease["id"], 1)
    assert repo.get("executions", foreign["id"], "other-owner")["state"] == "running"
    assert repo.get("jobs", following["id"], "owner").get("instance_id") is None


def test_orphan_cleanup_refuses_unknown_account_live_lease():
    from app.gpu.scheduler import Scheduler

    owner = "owner"
    instance_id = "i"
    stale = {"id": "stale", "owner_id": owner, "owner_job_id": "old", "provider": "mock", "instance_id": instance_id, "account_id": "acct", "request_id": "req-stale", "runtime_id": "runtime-stale", "state": "cleanup_pending", "cleanup_next_epoch": 0, "handle": {"runtime_name": "old", "runtime_id": "runtime-stale", "request_id": "req-stale", "job_id": "old", "owner_job_id": "old"}}
    live = {"id": "live", "owner_id": owner, "owner_job_id": "new", "provider": "mock", "instance_id": instance_id, "account_id": None, "request_id": "req-live", "runtime_id": "runtime-live", "state": "active", "handle": {"runtime_name": "live", "runtime_id": "runtime-live", "request_id": "req-live", "job_id": "new", "owner_job_id": "new"}}

    repo = Repository(Store("sqlite:///:memory:"))
    for row in (stale, live):
        repo.create("leases", owner, {k: v for k, v in row.items() if k not in {"id", "owner_id"}}, record_id=row["id"])

    class Provider:
        def __init__(self):
            self.calls = []

        def inspect(self, handle):
            self.calls.append(("inspect", handle))
            return {"deleted": False}

        def delete(self, handle):
            self.calls.append(("delete", handle))
            return {"deleted": True}

    provider = Provider()
    scheduler = Scheduler(repo, provider_factory=lambda _: provider, clock=lambda: 100)
    scheduler._cleanup_orphan_lease(repo.get("leases", "stale", owner))
    assert provider.calls == []


def test_orphan_cleanup_rechecks_same_instance_before_provider_delete():
    """A lease becoming live after the first scan must fence remote deletion."""
    owner = "owner"
    instance_id = "i"
    stale = {
        "id": "stale",
        "owner_id": owner,
        "owner_job_id": "old",
        "provider": "mock",
        "instance_id": instance_id,
        "account_id": "acct",
        "request_id": "req-stale",
        "runtime_id": "runtime-stale",
        "state": "cleanup_pending",
        "cleanup_next_epoch": 0,
        "handle": {
            "runtime_name": "old",
            "runtime_id": "runtime-stale",
            "request_id": "req-stale",
            "job_id": "old",
            "owner_job_id": "old",
        },
    }
    live = {
        "id": "live",
        "owner_id": owner,
        "owner_job_id": "new",
        "provider": "mock",
        "instance_id": instance_id,
        "account_id": "acct",
        "request_id": "req-live",
        "runtime_id": "runtime-live",
        "state": "active",
        "handle": {"runtime_id": "runtime-live", "job_id": "new", "owner_job_id": "new"},
    }
    repo = Repository(Store("sqlite:///:memory:"))
    repo.create("leases", owner, {k: v for k, v in stale.items() if k not in {"id", "owner_id"}}, record_id="stale")

    class Provider:
        def __init__(self):
            self.calls = []

        def inspect(self, handle):
            self.calls.append(("inspect", handle))
            repo.create("leases", owner, {k: v for k, v in live.items() if k not in {"id", "owner_id"}}, record_id="live")
            return {"deleted": False}

        def delete(self, handle):
            self.calls.append(("delete", handle))
            return {"deleted": True}

    provider = Provider()
    scheduler = Scheduler(repo, provider_factory=lambda _: provider, clock=lambda: 100)

    assert scheduler._cleanup_orphan_lease(repo.get("leases", "stale", owner)) is False
    assert [name for name, _ in provider.calls] == ["inspect"]


def test_handoff_requires_account_match_without_instance_row():
    repo = Repository(Store("sqlite:///:memory:"))
    body = {"project_id": "p", "snapshot": {"manifest": {"mock": True}}, "execution_policy": {"provider": "mock"}}
    repo.create_batch("owner", [body, body], "account-handoff", {"mode": "batch"})
    first, following = sorted(repo.list("jobs", "owner"), key=lambda row: row["queue_order"])
    lease = repo.create("leases", "owner", {
        "state": "active", "owner_job_id": first["id"], "account_id": "acct-a",
        "deadline_epoch": 9999, "handle": {"runtime_id": "r", "job_id": first["id"], "owner_job_id": first["id"]},
    })
    repo.update("jobs", first["id"], {"state": "cleaning_up", "lease_id": lease["id"], "account_id": "acct-a", "compatibility_signature": "same"}, "owner")
    repo.update("jobs", following["id"], {"state": "waiting_for_capacity", "account_id": "acct-b", "compatibility_signature": "same"}, "owner")

    assert not repo.handoff_lease("owner", first["id"], following["id"], lease["id"], 1)
    assert repo.get("jobs", following["id"], "owner").get("lease_id") is None


def test_prepare_execution_rejects_same_owner_wrong_job_account_lease():
    repo = Repository(Store("sqlite:///:memory:"))
    job = repo.create("jobs", "owner", {
        "state": "loading", "project_id": "p", "provider": "mock", "account_id": "acct-a",
        "lease_id": "lease-b", "idempotency_key": "prepare-job",
    }, record_id="job-a")
    repo.create("leases", "owner", {
        "state": "active", "owner_job_id": "job-b", "account_id": "acct-b", "instance_id": "instance-b",
        "deadline_epoch": 9999, "handle": {"runtime_id": "r", "job_id": "job-b", "owner_job_id": "job-b"},
    }, record_id="lease-b")

    import pytest
    with pytest.raises(ValueError, match="lease"):
        repo.prepare_execution(job["id"], "owner", "lease-b", 1)


def test_sql_cleanup_claim_ttl_replaces_crashed_claim_and_fences_old_token():
    repo = Repository(Store("sqlite:///:memory:"))
    instance = repo.create("instances", "owner", {"state": "releasing", "account_id": "acct"}, record_id="instance")
    lease = repo.create("leases", "owner", {"state": "cleanup_pending", "owner_job_id": "old", "instance_id": instance["id"], "account_id": "acct", "request_id": "req", "runtime_id": "runtime", "handle": {"runtime_id": "runtime", "job_id": "old", "owner_job_id": "old"}}, record_id="lease")
    first = repo.claim_orphan_lease("owner", lease["id"], instance["id"], "token-a", 100.0)
    assert first is not None
    assert repo.claim_orphan_lease("owner", lease["id"], instance["id"], "token-b", 101.0) is None
    second = repo.claim_orphan_lease("owner", lease["id"], instance["id"], "token-b", 401.0)
    assert second is not None
    assert not repo.finish_orphan_lease("owner", lease["id"], instance["id"], "token-a", 402.0, confirmed=True)
    assert repo.finish_orphan_lease("owner", lease["id"], instance["id"], "token-b", 402.0, confirmed=True)
    assert repo.get("leases", lease["id"], "owner")["state"] == "deleted"


def test_sql_cleanup_claim_fences_allocation_and_bind_until_finished():
    repo = Repository(Store("sqlite:///:memory:"))
    instance = repo.create("instances", "owner", {"state": "releasing", "account_id": "acct"}, record_id="instance")
    lease = repo.create("leases", "owner", {"state": "cleanup_pending", "owner_job_id": "old", "instance_id": instance["id"], "account_id": "acct", "request_id": "req", "runtime_id": "runtime", "handle": {"runtime_id": "runtime", "job_id": "old", "owner_job_id": "old"}}, record_id="lease")
    assert repo.claim_orphan_lease("owner", lease["id"], instance["id"], "token", 100.0)
    service = InstanceService(repo, clock=lambda: 100.0)
    body = {"id": "new-job", "owner_id": "owner", "project_id": "p", "provider": "mock", "requested_instance_id": instance["id"]}
    with pytest.raises(Exception):
        service.allocate(body)
    with pytest.raises(Exception):
        service.bind_lease({"id": "new-job", "owner_id": "owner", "instance_id": instance["id"]}, lease)


def test_claim_orphan_requires_matching_nonempty_account_evidence():
    repo = Repository(Store("sqlite:///:memory:"))
    repo.create("instances", "owner", {"state": "releasing", "account_id": "acct-b"}, record_id="instance")
    lease = repo.create("leases", "owner", {"state": "cleanup_pending", "instance_id": "instance", "account_id": "acct-a"}, record_id="lease")
    assert repo.claim_orphan_lease("owner", lease["id"], "instance", "token", 100.0) is None


def test_claim_orphan_rejects_allocating_busy_and_current_job_instance():
    for state in ("allocating", "ready", "busy", "retained_idle"):
        repo = Repository(Store("sqlite:///:memory:"))
        repo.create("instances", "owner", {"state": state, "account_id": "acct", "current_job_id": "new-job"}, record_id="instance")
        lease = repo.create("leases", "owner", {"state": "cleanup_pending", "instance_id": "instance", "account_id": "acct"}, record_id="lease")
        assert repo.claim_orphan_lease("owner", lease["id"], "instance", "token", 100.0) is None


def test_handoff_rejects_existing_successor_execution_association_conflict():
    repo = Repository(Store("sqlite:///:memory:"))
    body = {"project_id": "p", "snapshot": {"manifest": {"mock": True}}, "execution_policy": {"provider": "mock"}}
    repo.create_batch("owner", [body, body], "execution-conflict", {"mode": "batch"})
    first, following = sorted(repo.list("jobs", "owner"), key=lambda row: row["queue_order"])
    instance = repo.create("instances", "owner", {"account_id": "acct", "state": "busy", "current_job_id": first["id"]})
    lease = repo.create("leases", "owner", {"state": "active", "owner_job_id": first["id"], "instance_id": instance["id"], "account_id": "acct", "deadline_epoch": 9999, "handle": {"job_id": first["id"], "owner_job_id": first["id"]}})
    execution = repo.create("executions", "owner", {"state": "starting", "job_id": following["id"], "lease_id": "other-lease"})
    repo.update("jobs", first["id"], {"state": "cleaning_up", "lease_id": lease["id"], "instance_id": instance["id"], "compatibility_signature": "same"}, "owner")
    repo.update("jobs", following["id"], {"state": "waiting_for_capacity", "execution_id": execution["id"], "compatibility_signature": "same"}, "owner")
    assert repo.handoff_lease("owner", first["id"], following["id"], lease["id"], 1) is False


def test_prepare_execution_replay_rejects_wrong_lease_before_idempotent_return():
    repo = Repository(Store("sqlite:///:memory:"))
    job = repo.create("jobs", "owner", {"state": "loading", "project_id": "p", "provider": "mock", "lease_id": "lease-good", "idempotency_key": "replay"}, record_id="job")
    repo.create("leases", "owner", {"state": "active", "owner_job_id": "job", "deadline_epoch": 9999}, record_id="lease-good")
    repo.create("leases", "owner", {"state": "active", "owner_job_id": "job", "deadline_epoch": 9999}, record_id="lease-wrong")
    repo.update("jobs", "job", {"execution_id": "execution"}, "owner")
    repo.create("executions", "owner", {"state": "starting", "job_id": "job", "lease_id": "lease-good"}, record_id="execution")
    with pytest.raises(ValueError, match="lease"):
        repo.prepare_execution(job["id"], "owner", "lease-wrong", 1)


def test_reconcile_deleted_lease_requires_positive_matching_account_evidence():
    class Service:
        instances = [{"id": "instance", "owner_id": "owner", "state": "releasing", "lease_id": "lease", "account_id": None}]
        def mark_deleted(self, *args, **kwargs):
            raise AssertionError("must not mark an accountless historical orphan deleted")
    lease = {"id": "lease", "owner_id": "owner", "owner_job_id": "job", "state": "deleted", "account_id": None, "remote_deleted": True, "remote_delete_source": "cleanup_provider_confirmation"}
    repo = Repository(Store("sqlite:///:memory:"))
    repo.create("jobs", "owner", {"state": "completed"}, record_id="job")
    service = Service()
    service.list = lambda owner: Service.instances
    scheduler = Scheduler(repo, instance_service=service)
    assert scheduler._reconcile_deleted_lease_instances([lease]) == 0
