"""Owner-scoped, read-only persisted instance history.

This router reads jobs, events and output assets from the local repository only.
It never contacts a runtime, allocates a lease, or submits a GPU job.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request

from .common import APIError, current_user, get_store, ok
from .gpu.repository import Repository
from .job_history import job_timing_details, job_timings, safe_history_value, safe_log
from .presenters import public_job

router = APIRouter(prefix="/api/gpu")


def _repo(request: Request, st):
    return getattr(request.app.state, "repo", None) or Repository(st)


def _get(repo, kind: str, record_id: str, owner: str):
    try:
        return repo.get(kind, record_id, owner)
    except (AttributeError, KeyError, ValueError):
        return next((row for row in repo.list(kind, owner) if row.get("id") == record_id), None)


def _events(repo, owner: str, job_id: str):
    rows = [row for row in repo.list("events", owner) if row.get("job_id") == job_id]
    return sorted(rows, key=lambda row: str(row.get("created_at") or ""))


def _public_event(event: dict) -> dict:
    result = {}
    for key in ("id", "state", "event_type", "progress", "created_at"):
        if event.get(key) is not None:
            result[key] = event[key]
    if event.get("message") is not None:
        result["message"] = safe_log(event["message"])
    return result


def _public_asset(asset: dict) -> dict:
    result = {}
    for key in ("id", "name", "filename", "mime", "status", "created_at"):
        if asset.get(key) is not None:
            result[key] = safe_history_value(asset[key])
    for key in ("thumbnail_url", "download_url"):
        value = asset.get(key)
        if isinstance(value, str) and value.startswith("/") and not value.startswith("//"):
            result[key] = value.split("?", 1)[0].split("#", 1)[0]
    return result


def _history_job(repo, owner: str, job: dict, assets: dict[str, dict], *, now: float | None = None) -> dict:
    events = _events(repo, owner, job["id"])
    public = safe_history_value(public_job(job))
    error = None
    if job.get("error_code") or job.get("error_message"):
        error = {
            "code": safe_history_value(job.get("error_code")),
            "message": safe_log(job.get("error_message", "")),
        }
    output_assets = [_public_asset(assets[asset_id]) for asset_id in job.get("output_asset_ids", []) if asset_id in assets]
    logs = [_public_event(event) for event in events]
    if error and error.get("message"):
        logs.append({"source": "job", "state": job.get("state"), "message": error["message"]})
    return {
        **public,
        "job_id": job["id"],
        "timings": job_timings(job, events, now=now),
        "timing_details": job_timing_details(job, events, now=now),
        "events": logs,
        "logs": logs,
        "error": error,
        "artifacts": output_assets,
    }


@router.get("/instances/{instance_id}/history")
def instance_history(instance_id: str, request: Request, user=Depends(current_user), st=Depends(get_store)):
    repo = _repo(request, st)
    owner = user["id"]
    instance = _get(repo, "instances", instance_id, owner)
    if not isinstance(instance, dict):
        raise APIError("NOT_FOUND", "实例不存在或不属于当前用户", 404)
    jobs = [row for row in repo.list("jobs", owner) if row.get("instance_id") == instance_id]
    jobs.sort(key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)
    asset_ids = {asset_id for job in jobs for asset_id in (job.get("output_asset_ids") or []) if isinstance(asset_id, str)}
    # Assets belong to Store, not the GPU-only Repository; retain owner scoping.
    assets = {asset["id"]: asset for asset in st.list("assets", owner) if asset.get("id") in asset_ids} if asset_ids else {}
    now = time.time()
    return ok({
        "instance_id": instance_id,
        "offline": True,
        "source": "persisted_job_history",
        "live_runtime": False,
        "remote_actions": False,
        "jobs": [_history_job(repo, owner, job, assets, now=now) for job in jobs],
    }, request)


__all__ = ["router"]
