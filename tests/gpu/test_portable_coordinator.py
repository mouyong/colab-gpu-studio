import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.db import Store, utc_isoformat
from app.filelocks import file_lock
from app.gpu.repository import Repository


def test_process_lock_exclusion_and_release(tmp_path):
    path = tmp_path / "shared.lock"
    code = (
        "import sys; from app.filelocks import file_lock; "
        "h=open(sys.argv[1], 'a+b'); "
        "ctx=file_lock(h,blocking=False); acquired=ctx.__enter__(); "
        "print(int(acquired)); ctx.__exit__(None,None,None)"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "api")}
    def attempt():
        return subprocess.run([sys.executable, "-c", code, str(path)], env=env, capture_output=True, text=True, check=True, timeout=20).stdout.strip()
    with path.open("a+b") as handle:
        with file_lock(handle) as acquired:
            assert acquired and attempt() == "0"
        assert attempt() == "1"


def test_sqlite_rows_keep_explicit_utc_after_round_trip(tmp_path):
    store = Store("sqlite:///" + str(tmp_path / "studio.db"))
    project = store.create("projects", "owner", {"title": "example"})
    repo = Repository(store)
    job = repo.create("jobs", "owner", {"state": "queued"})
    for row in [store.get("projects", project["id"], "owner"), repo.get("jobs", job["id"], "owner")]:
        for field in ("created_at", "updated_at"):
            assert datetime.fromisoformat(row[field]).utcoffset().total_seconds() == 0
    assert utc_isoformat(datetime(2026, 11, 10, 3, 2)) == "2026-11-10T03:02:00+00:00"
    assert utc_isoformat(datetime(2026, 11, 10, tzinfo=timezone.utc)).endswith("+00:00")
