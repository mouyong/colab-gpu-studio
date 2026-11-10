from __future__ import annotations

from app.db import Store
from app.gpu.compute import MockColabProvider, MockGPUExecutor
from app.gpu.repository import Repository
from app.gpu.scheduler import Scheduler


class FakeInstanceService:
    """Offline v0.2 instance boundary used to prove scheduler coordination."""

    def __init__(self, *, lifecycle="retain", clock=lambda: 1_000.0):
        self.clock = clock
        self.lifecycle = lifecycle
        self.instances = {}
        self.calls = []
        self._next = 0

    def list(self, owner):
        return [dict(row) for row in self.instances.values()]

    def get(self, owner, instance_id):
        row = self.instances.get(instance_id)
        return dict(row) if row else None

    def allocate(self, job):
        self.calls.append(("allocate", job["id"]))
        self._next += 1
        instance = {
            "instance_id": f"instance-{self._next}",
            "account_id": "account-1",
            "state": "allocating",
            "retention_mode": self.lifecycle,
            "hard_deadline_at": self.clock() + 600,
            "idle_deadline_at": None,
            "compatibility_signature": job.get("compatibility_signature"),
        }
        self.instances[instance["instance_id"]] = instance
        return dict(instance)

    def bind_lease(self, job, lease):
        self.calls.append(("bind_lease", job["id"], lease["id"]))
        instance = self.instances[job["instance_id"]]
        instance["lease_id"] = lease["id"]
        instance["state"] = "allocating"
        return dict(instance)

    def mark_ready(self, job):
        self.calls.append(("mark_ready", job["id"]))
        instance = self.instances[job["instance_id"]]
        instance["state"] = "ready"
        return dict(instance)

    def job_busy(self, job):
        self.calls.append(("job_busy", job["id"]))
        instance = self.instances[job["instance_id"]]
        instance["state"] = "busy"
        instance["current_job_id"] = job["id"]
        return dict(instance)

    def complete(self, job, success=True):
        self.calls.append(("complete", job["id"], success))
        instance = self.instances[job["instance_id"]]
        instance["current_job_id"] = None
        if success and self.lifecycle == "retain":
            instance["state"] = "retained_idle"
            instance["idle_deadline_at"] = self.clock() + 120
        else:
            instance["state"] = "releasing"
        return dict(instance)

    def mark_deleted(self, owner, instance_id, expected_version=None, remote_deleted=False):
        assert owner == "owner-1"
        assert remote_deleted is True
        self.calls.append(("mark_deleted", instance_id, remote_deleted))
        self.instances[instance_id]["state"] = "deleted"
        return dict(self.instances[instance_id])

    def record_usage(self, owner, payload):
        self.calls.append(("record_usage", payload["instance_id"]))

    def public(self, instance):
        return {k: v for k, v in instance.items() if k != "runtime_name"}

    def retention(self, instance, retain=True, **kwargs):
        self.calls.append(("retention", instance["instance_id"], retain))
        return dict(instance)

    def stop(self, instance):
        self.calls.append(("stop", instance["instance_id"]))
        return dict(instance)


def make_job(repo, *, key="job", lifecycle="retain"):
    return repo.create_job(
        "owner-1",
        {
            "provider": "mock",
            "project_id": "project-1",
            "snapshot": {"manifest": {"mock": True}},
            "execution_policy": {"provider": "mock", "lifecycle": lifecycle},
            "estimated_units": 0,
        },
        key,
    )[0]


def run_to_terminal(scheduler, repo, job_id, limit=40):
    for _ in range(limit):
        scheduler.tick()
        job = repo.get("jobs", job_id, "owner-1")
        if job["state"] in {"completed", "failed", "cancelled"}:
            return job
    return repo.get("jobs", job_id, "owner-1")


def test_v020_allocates_and_binds_instance_before_execution():
    repo = Repository(Store("sqlite:///:memory:"))
    job = make_job(repo)
    service = FakeInstanceService()
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
    assert result["instance_id"] == "instance-1"
    assert result["account_id"] == "account-1"
    execution = repo.get("executions", result["execution_id"], "owner-1")
    assert execution["instance_id"] == "instance-1"
    lease = repo.get("leases", result["lease_id"], "owner-1")
    assert lease["instance_id"] == "instance-1"
    assert [call[0] for call in service.calls[:3]] == ["allocate", "bind_lease", "mark_ready"]
    assert ("job_busy", job["id"]) in service.calls


def test_v020_retained_instance_survives_completed_job_and_watchdog_until_deadline():
    now = [1_000.0]
    repo = Repository(Store("sqlite:///:memory:"))
    job = make_job(repo, lifecycle="retain")
    service = FakeInstanceService(clock=lambda: now[0])
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
    instance = service.instances["instance-1"]
    assert instance["state"] == "retained_idle"
    assert provider.delete_calls == 0

    scheduler.watchdog()
    assert instance["state"] == "retained_idle"
    assert provider.delete_calls == 0

    now[0] = instance["idle_deadline_at"] + 1
    scheduler.watchdog()
    assert instance["state"] == "deleted"
    assert provider.delete_calls == 1
    assert repo.get("leases", result["lease_id"], "owner-1")["state"] == "deleted"


def test_v020_save_requires_remote_delete_confirmation_before_marking_deleted():
    class AcceptedButNotGoneProvider(MockColabProvider):
        def delete(self, handle):
            self.delete_calls += 1
            return {"status": 202, "accepted": True}

    repo = Repository(Store("sqlite:///:memory:"))
    job = make_job(repo, lifecycle="save")
    service = FakeInstanceService(lifecycle="save")
    provider, executor = AcceptedButNotGoneProvider(), MockGPUExecutor()
    scheduler = Scheduler(
        repo,
        provider_factory=lambda _: provider,
        executor_factory=lambda _: executor,
        instance_service=service,
        clock=service.clock,
    )

    for _ in range(20):
        scheduler.tick()
        if repo.get("jobs", job["id"], "owner-1")["state"] == "cleanup_pending":
            break

    lease = repo.get("leases", repo.get("jobs", job["id"], "owner-1")["lease_id"], "owner-1")
    assert repo.get("jobs", job["id"], "owner-1")["state"] == "cleanup_pending"
    assert lease["state"] == "cleanup_pending"
    assert service.instances["instance-1"]["state"] != "deleted"
    assert not any(call[0] == "mark_deleted" for call in service.calls)
