import json
from types import SimpleNamespace

import pytest
from app.db import Store
from app.gpu import runtime_worker
from app.gpu.compute import MockColabProvider
from app.gpu.diagnostics import gpu_diagnostics
from app.gpu.mcp import MCPError, MCPGPUExecutor, build_poll_code
from app.gpu.repository import Repository
from app.gpu.scheduler import Scheduler
from app.presenters import public_job

EVIDENCE = {"source": "runtime_nvidia_smi", "captured_at": 1000.0,
            "devices": [{"name": "NVIDIA L4", "memory_mib": 23034}]}
EXECUTION = "11111111-1111-4111-8111-111111111111"


def test_worker_reports_actual_hardware_and_preserves_it_across_stage_and_terminal_updates(tmp_path, monkeypatch):
    commands = []
    def probe(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout="NVIDIA L4, 23034\n")
    monkeypatch.setattr(runtime_worker.subprocess, "run", probe)
    result = runtime_worker._probe_gpu()
    assert result["devices"] == EVIDENCE["devices"]
    assert commands == [["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]]
    runtime_worker._status(tmp_path, "running", gpu_diagnostics=result)
    stages = runtime_worker._Stages(tmp_path)
    stages.start("sampling")
    runtime_worker._status(tmp_path, "completed")
    assert json.loads((tmp_path / "status.json").read_text())["gpu_diagnostics"] == result


def test_missing_gpu_probe_is_nonfatal_and_never_invents_device(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError
    monkeypatch.setattr(runtime_worker.subprocess, "run", missing)
    assert runtime_worker._probe_gpu() is None
    assert gpu_diagnostics({"source": "requested_spec", "devices": [{"name": "A100"}]}) is None
    assert gpu_diagnostics({**EVIDENCE, "devices": []}) is None
    clean = gpu_diagnostics({**EVIDENCE, "proxy_token": "PRIVATE", "devices": [{**EVIDENCE["devices"][0], "serial": "PRIVATE"}]})
    assert clean == EVIDENCE


@pytest.mark.parametrize("state", ["running", "failed", "completed"])
def test_mcp_preserves_diagnostics_for_every_poll_outcome(state):
    executor = MCPGPUExecutor.__new__(MCPGPUExecutor)
    executor._session = lambda handle, job, create=False: "session"
    executor._exec_code_on_handle = lambda *args: {"state": state, "execution_id": EXECUTION, "outputs": [], "gpu_diagnostics": EVIDENCE}
    result = executor.poll({}, {"id": "job", "execution_id": EXECUTION})
    assert result["execution_id"] == EXECUTION and result["gpu_diagnostics"] == EVIDENCE
    assert "gpu_diagnostics" in build_poll_code(EXECUTION)
    executor._exec_code_on_handle = lambda *args: {"state": state, "execution_id": "foreign", "gpu_diagnostics": EVIDENCE}
    with pytest.raises(MCPError):
        executor.poll({}, {"id": "job", "execution_id": EXECUTION})


def running_job():
    repo = Repository(Store("sqlite://"))
    job = repo.create("jobs", "owner", {"state": "running", "provider": "mock", "account_id": "a", "execution_id": EXECUTION,
        "requested_spec": {"variant": "VARIANT_GPU", "accelerator": "A100", "shape": "SHAPE_HIGHMEM"}})
    instance = repo.create("instances", "owner", {"state": "busy", "current_job_id": job["id"], "account_id": "a"})
    lease = repo.create("leases", "owner", {"state": "active", "owner_job_id": job["id"], "account_id": "a", "instance_id": instance["id"]})
    repo.update("instances", instance["id"], {"lease_id": lease["id"]}, "owner")
    job = repo.update("jobs", job["id"], {"lease_id": lease["id"], "instance_id": instance["id"]}, "owner")
    return repo, job, instance, lease


def test_scheduler_records_actual_device_on_owned_job_lease_and_instance_and_keeps_history():
    repo, job, instance, lease = running_job()
    class Executor:
        def poll(self, handle, job):
            return {"state": "running", "execution_id": EXECUTION, "gpu_diagnostics": EVIDENCE}
    result = Scheduler(repo, executor_factory=lambda job: Executor())._run(job)
    assert result["gpu_reported"] == "NVIDIA L4"  # requested A100 is not evidence
    for kind, rid in [("jobs", job["id"]), ("instances", instance["id"]), ("leases", lease["id"])]:
        assert repo.get(kind, rid, "owner")["gpu_reported"] == "NVIDIA L4"
    repo.update("instances", instance["id"], {"state": "deleted", "runtime_deleted": True, "current_job_id": None}, "owner")
    saved = repo.update("jobs", job["id"], {"state": "completed", "runtime_deleted": True}, "owner")
    assert public_job(saved)["gpu_reported"] == "NVIDIA L4"
    assert Scheduler(repo)._result_metadata(saved)["gpu_diagnostics"] == EVIDENCE


def test_device_report_only_writes_the_owners_running_job():
    repo, job, instance, lease = running_job()
    assert repo.record_gpu_diagnostics({**job, "owner_id": "foreign"}, EVIDENCE) is None
    repo.update("jobs", job["id"], {"state": "completed"}, "owner")
    assert repo.record_gpu_diagnostics(job, EVIDENCE) is None
    assert not repo.get("jobs", job["id"], "owner").get("gpu_reported")
    assert not repo.get("instances", instance["id"], "owner").get("gpu_reported")


def test_device_report_skips_a_released_instance():
    repo, job, instance, lease = running_job()
    repo.update("instances", instance["id"], {"state": "deleted"}, "owner")
    assert repo.record_gpu_diagnostics(job, EVIDENCE)["gpu_reported"] == "NVIDIA L4"
    assert not repo.get("instances", instance["id"], "owner").get("gpu_reported")


@pytest.mark.parametrize("nested", [False, True])
def test_public_requested_spec_is_allowlisted_without_fabricating_gpu(nested):
    spec = {"accelerator": "L4", "proxy_token": "PRIVATE"}
    result = public_job({"id": "old", "requested_spec": {"key": spec} if nested else spec})
    assert result["runtime_spec_requested"] == {"accelerator": "L4"}
    assert not result.get("gpu_reported") and "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("failure", [False, True])
def test_cleanup_retry_clears_temporary_warning_and_preserves_generation_failure(failure):
    repo = Repository(Store("sqlite://"))
    provider = MockColabProvider(fail_delete_times=1)
    job = repo.create("jobs", "owner", {"state": "cleaning_up", "provider": "mock", "cleanup_final_state": "failed" if failure else "completed",
        **({"error_code": "gpu_execution_failed", "error_message": "generation failed"} if failure else {})})
    handle = provider.create({}, "request", job["id"], "runtime")
    repo.create("leases", "owner", {"state": "active", "owner_job_id": job["id"], "handle": handle})
    now = [1000.0]
    scheduler = Scheduler(repo, provider_factory=lambda job: provider, clock=lambda: now[0])
    pending = scheduler._cleanup(job)
    assert pending["state"] == "cleanup_pending"
    now[0] = pending["cleanup_next_epoch"] + 1
    result = scheduler._cleanup(pending)
    assert result["state"] == ("failed" if failure else "completed")
    assert result.get("error_code") == ("gpu_execution_failed" if failure else None)
    assert repo.list("leases", "owner")[0]["state"] == "deleted"
