from __future__ import annotations

import uuid

from app.db import Store
from app.gpu.compute import MockColabProvider, MockGPUExecutor
from app.gpu.repository import Repository
from app.gpu.scheduler import Scheduler


class LedgerInstanceService:
    """Small offline stand-in for the InstanceService accounting boundary."""

    def __init__(self, clock=lambda: 1_000.0):
        self.clock = clock
        self.instances = {}
        self.segments = {}
        self._next = 0

    def list(self, owner):
        return [dict(row) for row in self.instances.values()]

    def get(self, owner, instance_id):
        row = self.instances.get(instance_id)
        return dict(row) if row else None

    def allocate(self, job):
        self._next += 1
        instance = {
            "instance_id": f"instance-{self._next}",
            "account_id": "account-1",
            "state": "allocating",
            "retention_mode": job["execution_policy"].get("lifecycle", "save"),
            "allocation_requested_at": self.clock(),
            "hard_deadline_at": self.clock() + 600,
        }
        self.instances[instance["instance_id"]] = instance
        return dict(instance)

    def bind_lease(self, job, lease):
        instance = self.instances[job["instance_id"]]
        instance["lease_id"] = lease["id"]
        return dict(instance)

    def mark_ready(self, job):
        instance = self.instances[job["instance_id"]]
        instance["state"] = "ready"
        return dict(instance)

    def job_busy(self, job):
        instance = self.instances[job["instance_id"]]
        instance["state"] = "busy"
        instance["current_job_id"] = job["id"]
        return dict(instance)

    def complete(self, job, success=True):
        instance = self.instances[job["instance_id"]]
        instance["current_job_id"] = None
        instance["state"] = "retained_idle" if success and instance["retention_mode"] == "retain" else "releasing"
        if instance["state"] == "retained_idle":
            instance["idle_deadline_at"] = self.clock() + 120
        return dict(instance)

    def mark_deleted(self, owner, instance_id, expected_version=None, remote_deleted=False):
        assert remote_deleted is True
        instance = self.instances[instance_id]
        instance["state"] = "deleted"
        instance["deletion_confirmed_at"] = self.clock()
        return dict(instance)

    def record_usage(self, owner, payload):
        assert payload["instance_id"] in self.instances
        assert payload["actual_units"] is None
        key = (owner, payload["instance_id"], payload["segment_key"])
        if key not in self.segments:
            self.segments[key] = {"owner_id": owner, **payload}
        return dict(self.segments[key])


def make_job(repo, *, job_id="job-1", lifecycle="save"):
    return repo.create_job(
        "owner-1",
        {
            "provider": "mock",
            "project_id": "project-1",
            "snapshot": {"manifest": {"mock": True}},
            "execution_policy": {"provider": "mock", "lifecycle": lifecycle},
            "estimated_units": 0,
        },
        job_id,
    )[0]


def run_to_terminal(scheduler, repo, job_id, limit=40):
    for _ in range(limit):
        scheduler.tick()
        job = repo.get("jobs", job_id, "owner-1")
        if job["state"] in {"completed", "failed", "cancelled"}:
            return job
    return repo.get("jobs", job_id, "owner-1")


def test_normal_confirmed_delete_records_one_idempotent_instance_segment():
    repo = Repository(Store("sqlite:///:memory:"))
    service = LedgerInstanceService()
    job = make_job(repo)
    provider, executor = MockColabProvider(), MockGPUExecutor()
    scheduler = Scheduler(
        repo,
        provider_factory=lambda _: provider,
        executor_factory=lambda _: executor,
        instance_service=service,
        clock=service.clock,
    )

    result = run_to_terminal(scheduler, repo, job["id"])
    assert result["state"] == "completed"
    assert provider.delete_calls == 1
    assert len(service.segments) == 1
    segment = next(iter(service.segments.values()))
    assert segment["instance_id"] == result["instance_id"]
    assert segment["actual_units"] is None
    assert segment["estimated_units"] == 0.0
    assert segment["deletion_confirmed_at"] == 1_000.0

    # A repeated confirmation must use the same segment key and remain one row.
    scheduler._mark_instance_deleted(result)
    assert len(service.segments) == 1


def test_retained_instance_records_usage_only_after_idle_runtime_is_deleted():
    now = [1_000.0]
    repo = Repository(Store("sqlite:///:memory:"))
    service = LedgerInstanceService(clock=lambda: now[0])
    job = make_job(repo, lifecycle="retain")
    provider, executor = MockColabProvider(), MockGPUExecutor()
    scheduler = Scheduler(
        repo,
        provider_factory=lambda _: provider,
        executor_factory=lambda _: executor,
        instance_service=service,
        clock=lambda: now[0],
    )

    result = run_to_terminal(scheduler, repo, job["id"])
    assert result["state"] == "completed"
    assert len(service.segments) == 0
    assert provider.delete_calls == 0

    now[0] = service.instances[result["instance_id"]]["idle_deadline_at"] + 1
    scheduler.watchdog()
    assert provider.delete_calls == 1
    assert len(service.segments) == 1
    segment = next(iter(service.segments.values()))
    assert segment["actual_units"] is None


def test_orphan_confirmed_delete_records_instance_segment_without_actual_cu():
    repo = Repository(Store("sqlite:///:memory:"))
    service = LedgerInstanceService()
    instance_id = "orphan-instance"
    service.instances[instance_id] = {
        "instance_id": instance_id,
        "allocation_requested_at": 900.0,
        "state": "releasing",
    }
    provider = MockColabProvider()
    request_id = str(uuid.uuid4())
    runtime_id = "studio-orphan"
    provider.create({"provider": "mock"}, request_id, "orphan-job", runtime_id)
    lease = repo.create(
        "leases",
        "owner-1",
        {
            "owner_job_id": "orphan-job",
            "provider": "mock",
            "state": "active",
            "request_id": request_id,
            "runtime_id": runtime_id,
            "instance_id": instance_id,
            "created_epoch": 900.0,
            "deadline_epoch": 900.0,
        },
    )
    scheduler = Scheduler(
        repo,
        provider_factory=lambda _: provider,
        executor_factory=lambda _: MockGPUExecutor(),
        instance_service=service,
        clock=lambda: 1_000.0,
    )

    assert scheduler._cleanup_orphan_lease(lease) is True
    assert repo.get("leases", lease["id"], "owner-1")["state"] == "deleted"
    assert len(service.segments) == 1
    segment = next(iter(service.segments.values()))
    assert segment["instance_id"] == instance_id
    assert segment["allocation_requested_at"] == 900.0
    assert segment["deletion_confirmed_at"] == 1_000.0
    assert segment["actual_units"] is None
    assert segment["estimated_units"] == 0.0
