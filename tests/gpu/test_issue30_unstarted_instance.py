"""#30：运行时还没创建任务就失败时，实例不能一直停在「释放中」。"""
import sqlite3

from app.db import Store
from app.gpu.compute import MockColabProvider, MockGPUExecutor
from app.gpu.repository import Repository
from app.gpu.scheduler import Scheduler


def test_bind_failure_before_runtime_closes_instance(tmp_path):
    repo = Repository(Store('sqlite:///' + str(tmp_path / 'flow.db')))
    provider, executor = MockColabProvider(), MockGPUExecutor()
    scheduler = Scheduler(repo, provider_factory=lambda _: provider, executor_factory=lambda _: executor, clock=lambda: 1000.0)

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError('database is locked')

    scheduler.instances.bind_lease = locked
    job = repo.create_job('owner-1', {
        'provider': 'mock', 'project_id': 'project-1',
        'snapshot': {'manifest': {'mock': True}},
        'execution_policy': {'provider': 'mock', 'lifecycle': 'save'}, 'estimated_units': 0,
    }, 'k')[0]
    for _ in range(20):
        scheduler.tick()
        job = repo.get('jobs', job['id'], 'owner-1')
        if job['state'] in {'completed', 'failed', 'cancelled'}:
            break
    assert job['state'] == 'failed', job
    assert job['error_code'] == 'gpu_instance_conflict'
    assert repo.get('instances', job['instance_id'], 'owner-1')['state'] == 'deleted'


def test_file_sqlite_uses_wal(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'wal.db'))
    with store.engine.connect() as conn:
        assert conn.exec_driver_sql('PRAGMA journal_mode').scalar() == 'wal'


def test_two_processes_can_create_a_fresh_sqlite_schema_together(tmp_path):
    """API 和 worker 同时首次启动时不能因为重复建表而退出。"""
    import os
    import subprocess
    import sys

    url = 'sqlite:///' + str(tmp_path / 'fresh.db')
    code = 'import sys; from app.db import Store; Store(sys.argv[1])'
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(sys.path)}
    for _ in range(5):
        (tmp_path / 'fresh.db').unlink(missing_ok=True)
        procs = [subprocess.Popen([sys.executable, '-c', code, url], env=env, stderr=subprocess.PIPE) for _ in range(4)]
        for proc in procs:
            _, err = proc.communicate(timeout=60)
            assert proc.returncode == 0, err.decode()[-500:]
