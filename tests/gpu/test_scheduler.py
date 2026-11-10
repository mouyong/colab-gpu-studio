import uuid
from dataclasses import dataclass

from app.db import Store
from app.gpu.compute import GpuConfig, MockColabProvider, MockGPUExecutor
from app.gpu.repository import Repository
from app.gpu.scheduler import Scheduler


def MemoryStore():
    """In-memory SQLite Repository: the scheduler always runs against the real store."""
    return Repository(Store("sqlite:///:memory:"))


@dataclass
class Clock:
    now: float = 1_000.0

    def time(self):
        return self.now


def make_job(store, *, owner="u1", provider="mock", job_id=None, **extra):
    body = {
        "project_id": "p1",
        "provider": provider,
        "workflow_key": "demo",
        "workflow_version": 1,
        "model_id": "demo",
        "params": {},
        "max_runtime_minutes": 1,
        "estimated_units": 1,
        "snapshot": {"manifest": {"mock": True}},
        "request_body": {},
        **extra,
    }
    return store.create("jobs", owner, {**body, "state": "queued"}, record_id=job_id)


def drive(scheduler, job_id, limit=40):
    for _ in range(limit):
        scheduler.tick()
        job = scheduler.store.get("jobs", job_id)
        if job["state"] in {"completed", "failed", "cancelled", "cleanup_pending"}:
            return job
    return scheduler.store.get("jobs", job_id)


def test_three_jobs_are_globally_single_gpu_and_complete():
    store = MemoryStore()
    jobs = [make_job(store, job_id=f"j{i}") for i in range(3)]
    provider, executor = MockColabProvider(), MockGPUExecutor()
    scheduler = Scheduler(store, provider_factory=lambda j: provider, executor_factory=lambda j: executor)
    for _ in range(100):
        scheduler.tick()
        if all(store.get("jobs", j["id"])["state"] == "completed" for j in jobs):
            break
    assert [store.get("jobs", j["id"])["state"] for j in jobs] == ["completed"] * 3
    assert len(executor.started) == 3
    assert all(output["metadata"]["label"] == "MOCK" for output in executor.outputs)


def test_cancel_running_job_always_deletes_runtime():
    store = MemoryStore()
    job = make_job(store, job_id="cancel-me")
    provider, executor = MockColabProvider(), MockGPUExecutor(polls_before_complete=10)
    scheduler = Scheduler(store, provider_factory=lambda j: provider, executor_factory=lambda j: executor)
    for _ in range(6):
        scheduler.tick()
    assert store.get("jobs", job["id"])["state"] == "running"
    scheduler.cancel("u1", job["id"])
    assert drive(scheduler, job["id"])["state"] == "cancelled"
    assert provider.deleted


def test_failure_uses_finally_cleanup_and_never_completed():
    store = MemoryStore()
    job = make_job(store, job_id="fails")
    provider, executor = MockColabProvider(), MockGPUExecutor(fail_on_poll=True)
    scheduler = Scheduler(store, provider_factory=lambda j: provider, executor_factory=lambda j: executor)
    result = drive(scheduler, job["id"])
    assert result["state"] == "failed" and provider.deleted


def test_cleanup_pending_blocks_next_job_until_watchdog():
    store = MemoryStore()
    first = make_job(store, job_id="first")
    second = make_job(store, job_id="second")
    provider, executor = MockColabProvider(fail_delete_times=1), MockGPUExecutor()
    clock = Clock()
    scheduler = Scheduler(
        store, provider_factory=lambda j: provider, executor_factory=lambda j: executor, clock=clock.time
    )
    for _ in range(30):
        scheduler.tick()
        if store.get("jobs", first["id"])["state"] == "cleanup_pending":
            break
    assert store.get("jobs", first["id"])["state"] == "cleanup_pending"
    assert store.get("jobs", second["id"])["state"] != "completed"
    assert provider.delete_calls == 1
    scheduler.watchdog()
    assert provider.delete_calls == 1
    assert store.get("jobs", first["id"])["state"] == "cleanup_pending"
    assert store.get("jobs", second["id"])["state"] != "completed"
    clock.now = store.list("leases", "u1")[0]["cleanup_next_epoch"]
    scheduler.watchdog()
    assert provider.delete_calls == 2
    assert drive(scheduler, second["id"])["state"] == "completed"


def test_budget_rejection_precedes_provider_create(monkeypatch):
    monkeypatch.setenv("GPU_DAILY_UNITS", "1")
    store = MemoryStore()
    job = make_job(store, job_id="over-budget", provider="colab", estimated_units=2)
    provider = MockColabProvider()
    scheduler = Scheduler(
        store,
        provider_factory=lambda j: provider,
        executor_factory=lambda j: MockGPUExecutor(),
        config=GpuConfig.from_env(),
    )
    for _ in range(5):
        scheduler.tick()
    result = store.get("jobs", job["id"])
    assert result["state"] == "failed" and "budget" in result["error_code"] and provider.create_calls == 0


def test_timeout_cleans_up_without_polling_forever():
    clock, store = Clock(), MemoryStore()
    job = make_job(store, job_id="times-out")
    provider, executor = MockColabProvider(), MockGPUExecutor(polls_before_complete=100)
    scheduler = Scheduler(
        store, provider_factory=lambda j: provider, executor_factory=lambda j: executor, clock=clock.time
    )
    for _ in range(8):
        scheduler.tick()
    clock.now += 61
    result = drive(scheduler, job["id"])
    assert result["state"] == "failed" and result["error_code"] == "gpu_timeout" and provider.deleted


def test_restart_with_execution_id_does_not_start_again():
    store = MemoryStore()
    job = make_job(store, job_id="recover")
    provider, first = MockColabProvider(), MockGPUExecutor(polls_before_complete=3)
    scheduler = Scheduler(store, provider_factory=lambda j: provider, executor_factory=lambda j: first)
    for _ in range(6):
        scheduler.tick()
    stored = store.get("jobs", job["id"])
    assert stored["state"] == "running"
    second = MockGPUExecutor(polls_before_complete=0)
    recovered = Scheduler(store, provider_factory=lambda j: provider, executor_factory=lambda j: second)
    result = drive(recovered, job["id"])
    assert result["state"] == "completed" and second.start_calls == 0
    assert result["execution_id"] == stored["execution_id"]


def test_unknown_real_provider_fails_closed(monkeypatch):
    monkeypatch.setenv("GPU_REAL_ENABLED", "0")
    store = MemoryStore()
    job = make_job(store, job_id="real-disabled", provider="colab")
    result = drive(Scheduler(store, config=GpuConfig.from_env()), job["id"])
    assert result["state"] == "failed" and not store.list("leases", "u1")


class SQLiteOutputStorage:
    """Test storage adapter: durable GPU files, with verify-before-complete."""

    def __init__(self, repo):
        self.repo = repo

    def put(self, key, content, mime):
        return self.repo.create(
            "files",
            "u1",
            {
                "project_id": "p1",
                "storage_key": key,
                "filename": key.rsplit("/", 1)[-1],
                "mime": mime,
                "content_hex": content.hex(),
                "mock": True,
            },
        )

    def verify(self, record, content):
        return bytes.fromhex(record["content_hex"]) == content


def test_real_sqlite_three_jobs_outputs_and_restart_recovery(tmp_path):
    base = Store(f"sqlite:///{tmp_path / 'scheduler.db'}")
    repo = Repository(base)
    jobs = [
        repo.create_job("u1", {"provider": "mock", "project_id": "p1", "estimated_units": 1}, f"k{i}")[0]
        for i in range(3)
    ]
    provider = MockColabProvider()
    first_executor = MockGPUExecutor(polls_before_complete=2)
    scheduler = Scheduler(
        base,
        provider_factory=lambda job, store: provider,
        executor_factory=lambda job, store: first_executor,
        storage_factory=lambda job, store: SQLiteOutputStorage(repo),
    )
    # Simulate a worker restart while the first execution is already running.
    for _ in range(6):
        scheduler.tick()
    running = repo.get("jobs", jobs[0]["id"])
    assert running["state"] == "running"
    first_execution_id = running["execution_id"]
    restarted_executor = MockGPUExecutor(polls_before_complete=0)
    recovered = Scheduler(
        base,
        provider_factory=lambda job, store: provider,
        executor_factory=lambda job, store: restarted_executor,
        storage_factory=lambda job, store: SQLiteOutputStorage(repo),
    )
    for _ in range(120):
        recovered.tick()
        if all(repo.get("jobs", job["id"])["state"] == "completed" for job in jobs):
            break
    assert [repo.get("jobs", job["id"])["state"] for job in jobs] == ["completed"] * 3
    assert first_execution_id not in restarted_executor.started
    outputs = repo.list("files", "u1", "p1")
    assert len(outputs) == 6
    assert all(item["mock"] is True for item in outputs)
    assert all(lease["state"] == "deleted" for lease in repo.list("leases", "u1"))


class SpecProvider(MockColabProvider):
    def __init__(self, specs, **kwargs):
        super().__init__(**kwargs)
        self.specs = specs
        self.specs_calls = 0
        self.created_specs = []

    def list_specs(self):
        self.specs_calls += 1
        return list(self.specs)

    def create(self, spec, request_id, job_id, runtime_id):
        self.created_specs.append(spec)
        return super().create(spec, request_id, job_id, runtime_id)


def sqlite_job(tmp_path, **extra):
    base = Store(f"sqlite:///{tmp_path / 'faults.db'}")
    repo = Repository(base)
    job, _ = repo.create_job(
        "u1", {"provider": "mock", "project_id": "p1", "estimated_units": 0, **extra}, str(uuid.uuid4())
    )
    return base, repo, job


def test_real_sqlite_rejects_unverified_runtime_spec_before_create(tmp_path):
    base, repo, job = sqlite_job(
        tmp_path,
        execution_policy={"provider": "colab"},
        provider="colab",
        estimated_units=1,
        requested_spec="unknown",
        snapshot={"manifest": {"runtime": "r1"}},
    )
    provider = SpecProvider([{"name": "t4", "eligible": True, "gpu": True, "vram_gb": 16, "runtime": "r1"}])
    scheduler = Scheduler(
        base,
        provider_factory=lambda j, s: provider,
        executor_factory=lambda j, s: MockGPUExecutor(),
        config=GpuConfig(real_enabled=True, unit_rate=1, max_runtime_minutes=1),
    )
    result = drive(scheduler, job["id"])
    assert result["state"] == "failed"
    assert result["error_code"] == "gpu_spec_invalid"
    assert provider.specs_calls == 1 and provider.create_calls == 0


def test_real_sqlite_unknown_create_is_inspected_and_deleted(tmp_path):
    class TimeoutProvider(SpecProvider):
        def create(self, spec, request_id, job_id, runtime_id):
            self._runtimes[f"mock-runtime-{runtime_id}"] = {
                "runtime_name": f"mock-runtime-{runtime_id}",
                "runtime_id": runtime_id,
                "request_id": request_id,
                "job_id": job_id,
                "mock": True,
            }
            raise TimeoutError("provider timed out after allocation")

    base, repo, job = sqlite_job(tmp_path)
    provider = TimeoutProvider([{"name": "mock-colab", "eligible": True, "gpu": True, "vram_gb": 16}])
    scheduler = Scheduler(
        base,
        provider_factory=lambda j, s: provider,
        executor_factory=lambda j, s: MockGPUExecutor(),
        config=GpuConfig(create_retries=0),
    )
    result = drive(scheduler, job["id"])
    lease = repo.list("leases", "u1")[0]
    assert result["state"] == "failed"
    assert provider.inspect_calls >= 1 and provider.delete_calls >= 1
    assert lease["state"] == "deleted"


def test_real_sqlite_orphan_watchdog_only_cleans_expired_owned_lease(tmp_path):
    base = Store(f"sqlite:///{tmp_path / 'orphan.db'}")
    repo = Repository(base)
    provider = MockColabProvider()
    request_id = str(uuid.uuid4())
    runtime_id = "studio-deadbeef"
    repo.create(
        "leases",
        "u1",
        {
            "owner_job_id": "missing",
            "provider": "mock",
            "state": "active",
            "request_id": request_id,
            "runtime_id": runtime_id,
            "created_epoch": 1000,
            "deadline_epoch": 1000,
        },
    )
    provider.create({"provider": "mock"}, request_id, "missing", runtime_id)
    repo.create(
        "leases",
        "u1",
        {
            "provider": "mock",
            "state": "active",
            "request_id": str(uuid.uuid4()),
            "runtime_id": "studio-unowned",
            "created_epoch": 1000,
            "deadline_epoch": 1000,
        },
    )
    scheduler = Scheduler(
        base,
        provider_factory=lambda j, s: provider,
        executor_factory=lambda j, s: MockGPUExecutor(),
        clock=lambda: 1001,
    )
    result = scheduler.watchdog()
    assert result["recovered"] == 1
    leases = {lease.get("owner_job_id"): lease for lease in repo.list("leases", "u1")}
    assert leases["missing"]["state"] == "deleted"
    assert leases[None]["state"] == "active"
    assert provider.deleted


def test_real_sqlite_usage_ledger_is_actual_and_idempotent_after_delete(tmp_path):
    base, repo, job = sqlite_job(tmp_path, estimated_units=1)
    provider, executor = MockColabProvider(), MockGPUExecutor()
    clock = Clock(1000)
    scheduler = Scheduler(
        base, provider_factory=lambda j, s: provider, executor_factory=lambda j, s: executor, clock=clock.time
    )
    for _ in range(12):
        scheduler.tick()
        if repo.get("jobs", job["id"])["state"] == "completed":
            break
    assert repo.get("jobs", job["id"])["state"] == "completed"
    clock.now += 60
    scheduler.watchdog()
    usage = repo.list("usage", "u1")
    assert len(usage) == 1 and usage[0]["units"] == 0
    assert usage[0]["units_actual"] == 0 and usage[0]["simulated"] is True
    assert repo.get("jobs", job["id"])["actual_units"] == 0
    assert usage[0]["stopped_at"] is not None
    scheduler.watchdog()
    assert len(repo.list("usage", "u1")) == 1


def test_real_usage_is_administrator_estimate_until_provider_billing(tmp_path):
    base, repo, job = sqlite_job(tmp_path, provider="colab", estimated_units=2)
    lease = repo.create(
        "leases",
        "u1",
        {
            "owner_job_id": job["id"],
            "provider": "colab",
            "state": "deleted",
            "request_id": str(uuid.uuid4()),
            "runtime_id": "studio-billing",
            "created_epoch": 1000,
        },
    )
    scheduler = Scheduler(base, config=GpuConfig(real_enabled=True, unit_rate=2))
    scheduler._record_usage({**job, "provider": "colab", "started_epoch": 1000}, lease, 1120)
    usage = repo.list("usage", "u1")[0]
    assert usage["units_estimate"] == 4
    assert usage["units"] == 4
    assert usage["units_actual"] is None
    assert usage["source"] == "administrator_estimate"
    assert repo.get("jobs", job["id"])["actual_units"] is None


def test_watchdog_cleans_terminal_lease_only_when_owner_matches(tmp_path):
    base = Store(f"sqlite:///{tmp_path / 'terminal-lease.db'}")
    repo = Repository(base)
    provider = MockColabProvider()
    terminal_id = str(uuid.uuid4())
    repo.create("jobs", "u1", {"state": "completed"}, record_id=terminal_id)
    repo.create(
        "leases",
        "u1",
        {
            "owner_job_id": terminal_id,
            "provider": "mock",
            "state": "active",
            "request_id": "owned-request",
            "runtime_id": "studio-terminal-owned",
            "created_epoch": 1000,
            "deadline_epoch": 1000,
        },
    )
    provider.create({"provider": "mock"}, "owned-request", terminal_id, "studio-terminal-owned")
    repo.create(
        "leases",
        "u2",
        {
            "owner_job_id": terminal_id,
            "provider": "mock",
            "state": "active",
            "request_id": "wrong-owner-request",
            "runtime_id": "studio-terminal-wrong",
            "created_epoch": 1000,
            "deadline_epoch": 1000,
        },
    )
    provider.create({"provider": "mock"}, "wrong-owner-request", terminal_id, "studio-terminal-wrong")
    scheduler = Scheduler(
        base,
        provider_factory=lambda j, s: provider,
        executor_factory=lambda j, s: MockGPUExecutor(),
        clock=lambda: 1001,
    )
    result = scheduler.watchdog()
    assert result["recovered"] == 1
    assert repo.list("leases", "u1")[0]["state"] == "deleted"
    assert repo.list("leases", "u2")[0]["state"] == "active"


def test_real_sqlite_completed_without_outputs_is_failure_and_cleaned(tmp_path):
    base, repo, job = sqlite_job(tmp_path)

    class EmptyExecutor(MockGPUExecutor):
        def poll(self, handle, job):
            return {"state": "completed", "outputs": []}

    provider = MockColabProvider()
    scheduler = Scheduler(
        base, provider_factory=lambda j, s: provider, executor_factory=lambda j, s: EmptyExecutor()
    )
    result = drive(scheduler, job["id"])
    assert result["state"] == "failed" and result["error_code"] == "gpu_empty_outputs"
    assert provider.deleted
