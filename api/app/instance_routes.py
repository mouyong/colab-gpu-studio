"""HTTP boundary for owner-scoped GPU instances and the controlled workbench."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import APIError, current_user, ok
from .gpu.instances import AllocationError, BudgetError, InstanceConflict, InstanceServiceError
from .gpu.workbench import WorkbenchError
from .presenters import public_job

router = APIRouter(prefix="/api/gpu")


class RetentionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # ``retention_mode`` is the v0.2 client spelling; ``mode`` remains a
    # compatibility alias used by the first offline API clients.
    mode: Literal["save", "smart", "retain"] | None = None
    retention_mode: Literal["save", "smart", "retain"] | None = None
    idle_minutes: int = Field(default=0, ge=0, le=30)
    key: str | None = Field(default=None, min_length=1, max_length=200)
    expected_version: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def has_mode(self):
        if not self.mode and not self.retention_mode:
            raise ValueError("retention_mode不能为空")
        if self.mode and self.retention_mode and self.mode != self.retention_mode:
            raise ValueError("mode与retention_mode不能冲突")
        return self


class StopInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str | None = Field(default=None, min_length=1, max_length=200)
    confirmed: bool


class WorkbenchActionInput(BaseModel):
    model_config = ConfigDict(extra="allow")
    action: str = Field(min_length=1, max_length=64)
    key: str | None = Field(default=None, max_length=200)


_PUBLIC_SECRET_NAMES = {
    "handle",
    "runtime_handle",
    "connectioninfo",
    "connection_info",
    "connection_token",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "password",
    "cookie",
    "authorization",
    "signed_url",
    "runtime_name",
}


def _safe_public(value: Any):
    """Remove credential-bearing fields even if a service.public implementation errs."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in _PUBLIC_SECRET_NAMES or any(
                marker in normalized for marker in ("token", "secret", "password", "authorization", "cookie")
            ):
                continue
            result[key] = _safe_public(item)
        return result
    if isinstance(value, list):
        return [_safe_public(item) for item in value]
    return value


def _service(request: Request):
    return request.app.state.instance_service


def _repo(request: Request):
    return request.app.state.repo


def _domain_error(exc):
    if isinstance(exc, InstanceConflict):
        return APIError("INSTANCE_CONFLICT", str(exc), 409, True)
    if isinstance(exc, BudgetError):
        return APIError("BUDGET_REJECTED", str(exc), 409)
    if isinstance(exc, AllocationError):
        return APIError("INSTANCE_OPERATION_REJECTED", str(exc), 422)
    if isinstance(exc, KeyError):
        return APIError("NOT_FOUND", "实例不存在或不属于当前用户", 404)
    if isinstance(exc, InstanceServiceError):
        return APIError("INSTANCE_OPERATION_FAILED", str(exc), 503, True)
    return None


def _instance(service, instance_id: str, owner: str):
    try:
        row = service.get(owner, instance_id)
    except (InstanceServiceError, KeyError) as exc:
        raise _domain_error(exc) from exc
    if not row:
        raise APIError("NOT_FOUND", "实例不存在或不属于当前用户", 404)
    return row


def _public(service, row):
    return _safe_public(service.public(row))


def _related(request: Request, instance_id: str, owner: str):
    jobs = [row for row in _repo(request).list("jobs", owner) if row.get("instance_id") == instance_id]
    try:
        events = _service(request).events(owner, instance_id)
    except (InstanceServiceError, KeyError) as exc:
        raise _domain_error(exc) from exc
    return jobs, events


def _public_job_item(job: dict):
    return _safe_public(public_job(job))


@router.get("/instances")
def instances(
    request: Request,
    state: str | None = None,
    detail: bool = False,
    user=Depends(current_user),
):
    service = _service(request)
    rows = service.list(user["id"], state=state)
    result = []
    for row in rows:
        item = _public(service, row)
        if detail:
            jobs, events = _related(request, row["id"], user["id"])
            item["jobs"] = [_public_job_item(job) for job in jobs]
            item["events"] = [_safe_public(event) for event in events]
        result.append(item)
    return ok(result, request)


@router.get("/instances/{instance_id}")
def instance_detail(instance_id: str, request: Request, user=Depends(current_user)):
    service = _service(request)
    row = _instance(service, instance_id, user["id"])
    jobs, events = _related(request, instance_id, user["id"])
    item = _public(service, row)
    item["jobs"] = [_public_job_item(job) for job in jobs]
    item["events"] = [_safe_public(event) for event in events]
    return ok(item, request)


@router.get("/instances/{instance_id}/jobs")
def instance_jobs(instance_id: str, request: Request, user=Depends(current_user)):
    service = _service(request)
    _instance(service, instance_id, user["id"])
    jobs, _ = _related(request, instance_id, user["id"])
    return ok([_public_job_item(job) for job in jobs], request)


@router.get("/instances/{instance_id}/events")
def instance_events(instance_id: str, request: Request, user=Depends(current_user)):
    service = _service(request)
    _instance(service, instance_id, user["id"])
    _, events = _related(request, instance_id, user["id"])
    return ok([_safe_public(event) for event in events], request)


@router.patch("/instances/{instance_id}/retention", status_code=202)
def retention(instance_id: str, body: RetentionInput, request: Request, user=Depends(current_user)):
    service = _service(request)
    row = _instance(service, instance_id, user["id"])
    if row.get("state") in {"deleted", "unknown", "cleanup_pending", "quarantined"}:
        raise APIError("INSTANCE_READ_ONLY", "实例已失效，不能修改保留策略", 409)
    mode = body.mode or body.retention_mode
    key = body.key or request.headers.get("Idempotency-Key")
    if not key:
        raise APIError("IDEMPOTENCY_REQUIRED", "保留策略更新必须提供固定幂等键")

    try:
        value = service.retention(
            user["id"], instance_id, mode, body.idle_minutes, key, expected_version=body.expected_version
        )
    except (InstanceServiceError, KeyError) as exc:
        raise _domain_error(exc) from exc
    return ok(_public(service, value), request, 202)


@router.post("/instances/{instance_id}/stop", status_code=202)
def stop_instance(instance_id: str, body: StopInput, request: Request, user=Depends(current_user)):
    service = _service(request)
    row = _instance(service, instance_id, user["id"])
    if body.confirmed is not True:
        raise APIError("CONFIRMATION_REQUIRED", "停止实例必须明确 confirmed=true", 422)
    key = body.key or request.headers.get("Idempotency-Key")
    if not key:
        raise APIError("IDEMPOTENCY_REQUIRED", "停止实例必须提供固定幂等键")
    if row.get("state") == "deleted":
        return ok({"id": instance_id, "state": "deleted", "operation": "already_deleted"}, request, 202)
    try:
        value = service.stop(user["id"], instance_id, key)
    except (InstanceServiceError, KeyError) as exc:
        raise _domain_error(exc) from exc
    return ok(_public(service, value), request, 202)


def _workbench(request: Request):
    return request.app.state.workbench_service


@router.get("/instances/{instance_id}/workbench")
def workbench_info(instance_id: str, request: Request, user=Depends(current_user)):
    service = _service(request)
    row = _instance(service, instance_id, user["id"])
    return ok(_workbench(request).describe(user["id"], row), request)


@router.post("/instances/{instance_id}/workbench/actions")
def workbench_action(instance_id: str, body: WorkbenchActionInput, request: Request, user=Depends(current_user)):
    service = _service(request)
    row = _instance(service, instance_id, user["id"])
    payload = body.model_dump(exclude={"action", "key"})
    key = request.headers.get("Idempotency-Key") or body.key
    if body.key and request.headers.get("Idempotency-Key") and body.key != request.headers["Idempotency-Key"]:
        raise APIError("IDEMPOTENCY_CONFLICT", "请求头与正文幂等键不一致", 409)
    try:
        result = _workbench(request).execute(user["id"], row, body.action, payload, key)
    except WorkbenchError as exc:
        raise APIError(exc.code, str(exc), exc.status, exc.retryable) from exc
    return ok(_safe_public(result), request, 202 if body.action in {"set_reviewed_params", "queue_reviewed_workflow", "stop_current_job"} else 200)


@router.get("/workflows/{workflow_key}/preview")
def workflow_graph_preview(
    workflow_key: str,
    request: Request,
    version: int,
    draft: bool = False,
    user=Depends(current_user),
):
    """Owner-scoped offline graph preview; drafts are read-only and never executable."""
    wb = _workbench(request)
    try:
        result = wb.get_reviewed_graph(
            user["id"], {"workflow_key": workflow_key, "workflow_version": version, "allow_draft": draft}
        )
    except WorkbenchError as exc:
        raise APIError(exc.code, str(exc), exc.status, exc.retryable) from exc
    return ok(_safe_public(result), request)


__all__ = ["router", "RetentionInput", "StopInput", "WorkbenchActionInput"]
