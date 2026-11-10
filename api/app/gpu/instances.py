"""Persistent instance allocation and lifecycle domain service.

The service is deliberately repository-facing rather than provider-facing.  It
makes the short, durable admission decision (account + instance + budget),
then leaves provider I/O to the scheduler.  Every state change increments the
row version and uses a conditional SQL UPDATE, so a process restart or a
second scheduler cannot silently steal an instance.
"""

from __future__ import annotations

import copy
import math
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Mapping

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.db import utc_isoformat

from .manifest import digest


class InstanceServiceError(RuntimeError):
    """Base class for safe, domain-level allocation errors."""


class AllocationError(InstanceServiceError):
    """The requested account/instance cannot legally be allocated."""


class InstanceConflict(InstanceServiceError):
    """The resource changed state or is owned by another active operation."""


class BudgetError(InstanceServiceError):
    """Allocation or retention has no safe, configured budget."""


class InstanceService:
    """Persistent instance state machine used by both API and Scheduler.

    ``repo`` is the GPU ``Repository``.  The implementation only relies on
    its public store/session shape plus the model registry, which lets a
    migration register the v0.2 tables without changing this domain service.
    """

    _INSTANCE_STATES = {"requested", "allocating", "ready", "busy", "retained_idle", "releasing", "deleted", "unknown", "cleanup_pending", "quarantined"}
    _IDLE_STATES = {"ready", "retained_idle"}
    _SENSITIVE = {"token", "access_token", "refresh_token", "connectioninfo", "connection_info", "handle", "secret", "password", "authorization", "signed_url"}
    _PUBLIC = {
        "id", "owner_id", "project_id", "state", "version", "account_id", "account_alias_snapshot", "provider",
        "instance_label", "runtime_spec_requested", "runtime_spec_reported", "gpu_reported", "current_job_id",
        "lease_id", "hard_deadline_at", "idle_deadline_at", "retention_mode", "manual_stop_requested",
        "created_at", "updated_at", "last_seen_at", "generation", "selection_reason", "runtime_deleted",
        "allocation_key", "budget_reserved", "retention_reserved", "allocation_requested_at", "deletion_confirmed_at",
    }

    def __init__(self, repo, clock=time.time, config=None):
        self.repo = repo
        self.store = repo.store
        self.clock = clock
        self.config = config

    # ---- repository/model plumbing -------------------------------------------------
    def _model(self, kind):
        return self.repo._model(kind)

    @contextmanager
    def _tx(self):
        with self.store.db_lock(), self.store.Session.begin() as session:
            yield session

    @contextmanager
    def _admission(self):
        with self.store.admission_lock(timeout=float(self._cfg("admission_timeout_seconds", 1.0))) as acquired:
            if not acquired:
                raise InstanceConflict("资源分配正在进行，请稍后重试")
            yield

    @staticmethod
    def _data(row) -> dict:
        return copy.deepcopy(getattr(row, "data", None) or {})

    def _dict(self, row) -> dict:
        data = self._data(row)
        result = {"id": row.id, "owner_id": row.owner_id, "project_id": row.project_id}
        result.update(data)
        result.update(
            state=row.state,
            version=row.version,
            created_at=utc_isoformat(row.created_at),
            updated_at=utc_isoformat(row.updated_at),
        )
        return result

    def _cfg(self, name, default=None):
        cfg = self.config
        if cfg is None:
            return default
        if isinstance(cfg, Mapping):
            return cfg.get(name, default)
        return getattr(cfg, name, default)

    def _utcnow(self):
        return datetime.now(timezone.utc)

    def _set(self, row, payload: dict, *, state: str | None = None, expected_version: int | None = None, session=None):
        """CAS a row and update its JSON/state/version in one SQL statement."""
        expected = row.version if expected_version is None else expected_version
        values = {"data": copy.deepcopy(payload), "version": expected + 1, "updated_at": self._utcnow()}
        if state is not None:
            values["state"] = state
        for field in ("account_id", "instance_id", "current_job_id"):
            if hasattr(type(row), field) and field in payload:
                values[field] = payload[field]
        stmt = update(type(row)).where(type(row).id == row.id, type(row).version == expected).values(**values)
        result = session.execute(stmt)
        if result.rowcount != 1:
            raise InstanceConflict("实例状态已被其他调度器更新")
        row.data = copy.deepcopy(payload)
        row.version = expected + 1
        row.updated_at = values["updated_at"]
        if state is not None:
            row.state = state
        return row

    def _find(self, session, kind, record_id, owner_id=None):
        model = self._model(kind)
        stmt = select(model).where(model.id == record_id)
        if owner_id is not None:
            stmt = stmt.where(model.owner_id == owner_id)
        return session.scalar(stmt)

    def _rows(self, session, kind, owner_id=None):
        model = self._model(kind)
        stmt = select(model)
        if owner_id is not None:
            stmt = stmt.where(model.owner_id == owner_id)
        return list(session.scalars(stmt).all())

    @staticmethod
    def _value(data, *names, default=None):
        for name in names:
            if name in data:
                return data[name]
        return default

    def _job_policy(self, job):
        return job.get("execution_policy") if isinstance(job.get("execution_policy"), Mapping) else {}

    def _provider(self, job):
        return str(job.get("provider") or self._job_policy(job).get("provider") or "mock")

    def _requested(self, job, name):
        policy = self._job_policy(job)
        return job.get(f"requested_{name}") or job.get(name) or policy.get(f"requested_{name}") or policy.get(name)

    def _attempt_key(self, job):
        job_id = job.get("id")
        if not isinstance(job_id, str) or not job_id:
            raise AllocationError("job id is required for persistent allocation")
        attempt = job.get("attempt_id") or job.get("attempt_number") or job.get("attempt") or 1
        return f"{job_id}:{attempt}", str(uuid.uuid5(uuid.NAMESPACE_URL, f"colab-gpu-instance:{job_id}:{attempt}"))

    # ---- account, budget and selection --------------------------------------------
    def _account_id(self, row):
        return self._value(self._data(row), "account_id", default=row.id)

    def _account_legal(self, row, owner_id, project_id, provider):
        if row.owner_id != owner_id:
            return False
        data = self._data(row)
        status = self._value(data, "auth_status", "status", "connection_status", default="connected")
        connected = self._value(data, "connected", default=status == "connected")
        if connected is not True or data.get("enabled", True) is not True or data.get("allow_allocation", True) is not True:
            return False
        allowed = self._value(data, "allowed_projects", "allowed_project_ids", "project_ids", "projects")
        if allowed and project_id not in set(allowed):
            return False
        account_provider = data.get("provider")
        if provider != "mock" and account_provider not in {None, provider, "colab", "google"}:
            return False
        return True

    def _make_mock_account(self, session, owner_id):
        model = self._model("accounts")
        account_id = f"mock-{uuid.uuid5(uuid.NAMESPACE_URL, f'colab-gpu-mock-account:{owner_id}').hex}"
        existing = session.scalar(select(model).where(model.id == account_id))
        if existing:
            return existing
        now = self._utcnow()
        payload = {
            "account_id": account_id,
            "alias": "Mock account",
            "provider": "mock",
            "connected": True,
            "auth_status": "connected",
            "enabled": True,
            "allow_allocation": True,
            "is_mock": True,
        }
        row = model(id=account_id, owner_id=owner_id, project_id=None, state="connected", data=payload, version=0, created_at=now, updated_at=now)
        session.add(row)
        session.flush()
        return row

    def _legal_accounts(self, session, owner_id, project_id, provider, requested):
        rows = self._rows(session, "accounts", owner_id)
        if requested:
            row = next((r for r in rows if self._account_id(r) == requested or r.id == requested), None)
            if row is None:
                raise AllocationError("指定账号不存在")
            if not self._account_legal(row, owner_id, project_id, provider):
                raise AllocationError("指定账号未连接、已禁用或不允许该项目")
            return [row]
        legal = [r for r in rows if self._account_legal(r, owner_id, project_id, provider)]
        if not legal and provider == "mock":
            legal = [self._make_mock_account(session, owner_id)]
        if not legal:
            raise AllocationError("没有可分配的授权账号")
        return sorted(legal, key=lambda row: self._account_id(row))

    def _estimate_units(self, job, provider):
        if provider == "mock":
            return 0.0
        rate = self._cfg("unit_rate")
        if rate is None:
            raise BudgetError("real GPU allocation requires an explicit unit_rate")
        try:
            rate = float(rate)
        except (TypeError, ValueError) as exc:
            raise BudgetError("GPU unit_rate is invalid") from exc
        if not math.isfinite(rate) or rate <= 0:
            raise BudgetError("GPU unit_rate must be finite and positive")
        raw = job.get("budget_units", job.get("estimated_units"))
        if raw is None:
            minutes = job.get("max_runtime_minutes") or self._job_policy(job).get("max_runtime_minutes") or self._cfg("max_runtime_minutes", 0)
            raw = float(minutes) * rate
        try:
            units = float(raw)
        except (TypeError, ValueError) as exc:
            raise BudgetError("GPU budget estimate is invalid") from exc
        if not math.isfinite(units) or units < 0:
            raise BudgetError("GPU budget estimate is invalid")
        maximum = self._cfg("max_job_units")
        if maximum is not None and units > float(maximum):
            raise BudgetError("GPU budget exceeded")
        return units

    def _account_budget_limits(self, account):
        """Read the account-directory budget shape and fail closed when absent."""
        account_data = self._data(account)
        nested = account_data.get("budget")
        nested = nested if isinstance(nested, Mapping) else {}
        # The UI/API stores budget.daily/monthly.  The flat names remain a
        # narrow migration compatibility path, but global config is never used
        # as a substitute for an unconfigured real account.
        daily = self._value(nested, "daily", "daily_units", "daily_budget_units")
        monthly = self._value(nested, "monthly", "monthly_units", "monthly_budget_units")
        if daily is None:
            daily = self._value(account_data, "daily_units", "daily_budget_units", "budget_units")
        if monthly is None:
            monthly = self._value(account_data, "monthly_units", "monthly_budget_units")
        if daily is None:
            raise BudgetError("GPU account budget is not configured")
        try:
            daily = float(daily)
            monthly = None if monthly is None else float(monthly)
        except (TypeError, ValueError) as exc:
            raise BudgetError("GPU account budget is invalid") from exc
        if not math.isfinite(daily) or daily < 0 or (monthly is not None and (not math.isfinite(monthly) or monthly < 0)):
            raise BudgetError("GPU account budget is invalid")
        return daily, monthly

    def _budget_totals(self, session, owner_id, account_id=None):
        """Return billed usage plus every still-live instance reservation."""
        now = float(self.clock())
        daily = monthly = 0.0
        for row in self._rows(session, "instance_usage_segments", owner_id):
            data = self._data(row)
            if account_id is not None and data.get("account_id") not in {None, account_id}:
                continue
            value = data.get("actual_units") if data.get("actual_units") is not None else data.get("estimated_units")
            if value is None:
                continue
            value = float(value)
            stamp = data.get("created_epoch", data.get("epoch", data.get("started_at")))
            stamp = float(stamp) if stamp is not None else None
            if stamp is None or stamp >= now - 31 * 86400:
                monthly += value
            if stamp is None or stamp >= now - 86400:
                daily += value

        for row in self._rows(session, "instances", owner_id):
            data = self._data(row)
            if account_id is not None and data.get("account_id") != account_id:
                continue
            # retained_idle and releasing are billable until remote deletion is
            # confirmed.  Unknown/cleanup states are also fail-closed here.
            if row.state == "deleted":
                continue
            reserved = float(data.get("budget_reserved") or 0) + float(data.get("retention_reserved") or 0)
            daily += reserved
            monthly += reserved
        return daily, monthly

    def _check_budget(self, session, account, additional_units):
        account_daily, account_monthly = self._account_budget_limits(account)
        global_daily = self._cfg("daily_units")
        global_monthly = self._cfg("monthly_units")
        if global_daily is None:
            raise BudgetError("GPU global budget is not configured")

        account_id = self._account_id(account)
        account_daily_used, account_monthly_used = self._budget_totals(session, account.owner_id, account_id)
        global_daily_used, global_monthly_used = self._budget_totals(session, account.owner_id)
        if account_daily_used + additional_units > account_daily or (account_monthly is not None and account_monthly_used + additional_units > account_monthly):
            raise BudgetError("GPU account budget exceeded")
        if global_daily_used + additional_units > global_daily or (global_monthly is not None and global_monthly_used + additional_units > global_monthly):
            raise BudgetError("GPU global budget exceeded")

    def _reserve_budget(self, session, job, account, provider):
        units = self._estimate_units(job, provider)
        if provider == "mock":
            return 0.0
        self._check_budget(session, account, units)
        return units

    def _retention_units(self, provider, minutes):
        if provider == "mock":
            return 0.0
        rate = self._cfg("unit_rate")
        if not rate or rate <= 0:
            raise BudgetError("real GPU retention requires a positive unit_rate")
        return float(minutes) * rate

    def _cursor_id(self, owner_id, project_id):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"colab-gpu-allocation-cursor:{owner_id}:{project_id or ''}"))

    def _choose_account(self, session, accounts, owner_id, project_id):
        if len(accounts) == 1:
            return accounts[0]
        model = self._model("allocation_cursors")
        cursor_id = self._cursor_id(owner_id, project_id)
        cursor = session.scalar(select(model).where(model.id == cursor_id))
        if cursor is None:
            now = self._utcnow()
            cursor = model(id=cursor_id, owner_id=owner_id, project_id=project_id, state="active", version=0, data={"next_index": 0}, created_at=now, updated_at=now)
            session.add(cursor)
            session.flush()
        data = self._data(cursor)
        index = int(data.get("next_index", 0)) % len(accounts)
        selected = accounts[index]
        data.update(next_index=(index + 1) % len(accounts), last_account_id=self._account_id(selected))
        self._set(cursor, data, state="active", session=session)
        return selected

    def _compatible(self, instance, job, account_id):
        data = self._data(instance)
        if self._value(data, "account_id") != account_id:
            return False
        if data.get("current_job_id") is not None:
            return False
        if instance.state not in self._IDLE_STATES:
            return False
        signature = job.get("compatibility_signature")
        return not signature or data.get("compatibility_signature") == signature

    def _claim_existing(self, session, instance, job, account, key, stable_id, reason, units):
        data = self._data(instance)
        if data.get("current_job_id") not in {None, job.get("id")}:
            raise InstanceConflict("实例已被其他任务占用")
        data.update(
            current_job_id=job["id"], account_id=self._account_id(account), allocation_key=key,
            selection_reason=reason, budget_reserved=units, retention_reserved=0.0,
            allocation_requested_at=data.get("allocation_requested_at") or self.clock(), last_seen_at=self.clock(),
            manual_stop_requested=False, runtime_deleted=False, idle_deadline_at=None,
        )
        self._set(instance, data, state="allocating", expected_version=instance.version, session=session)
        self._bind_job_rows(session, job, instance, account, units)
        self._emit(session, job["owner_id"], instance.project_id, instance.id, job["id"], "allocating", "instance allocated")
        return self._dict(instance)

    def _create_instance(self, session, job, account, key, stable_id, units, reason):
        model = self._model("instances")
        now = self._utcnow()
        policy = self._job_policy(job)
        payload = {
            "account_id": self._account_id(account), "provider": self._provider(job), "current_job_id": job["id"],
            "allocation_key": key, "compatibility_signature": job.get("compatibility_signature"),
            "runtime_spec_requested": job.get("requested_spec") or policy.get("requested_spec"),
            "retention_mode": job.get("lifecycle") or policy.get("lifecycle") or "save",
            "selection_reason": reason, "budget_reserved": units, "retention_reserved": 0.0,
            "allocation_requested_at": self.clock(), "generation": 0,
            "manual_stop_requested": False, "runtime_deleted": False, "last_seen_at": self.clock(),
        }
        if job.get("hard_deadline_at") is not None:
            payload["hard_deadline_at"] = float(job["hard_deadline_at"])
        row = model(id=stable_id, owner_id=job["owner_id"], project_id=job.get("project_id"), state="allocating", data=payload, version=0, created_at=now, updated_at=now)
        session.add(row)
        try:
            session.flush()
        except IntegrityError as exc:
            raise InstanceConflict("实例分配发生竞争，请重试") from exc
        self._bind_job_rows(session, job, row, account, units)
        self._emit(session, job["owner_id"], row.project_id, row.id, job["id"], "allocating", "instance allocated")
        return self._dict(row)

    def _bind_job_rows(self, session, job, instance, account, units):
        account_id = self._account_id(account) if account is not None else self._value(self._data(instance), "account_id")
        account_data = self._data(account) if account is not None else {}
        account_alias = self._value(account_data, "alias", "label", default=None) if account is not None else None
        drive_account_id = self._value(account_data, "drive_account_id", default=None) or account_id
        try:
            job_row = self._find(session, "jobs", job["id"], job["owner_id"])
        except ValueError:
            job_row = None
        if job_row is not None:
            data = self._data(job_row)
            existing = data.get("instance_id")
            if existing not in {None, instance.id}:
                raise InstanceConflict("job already belongs to another instance")
            data.update(account_id=account_id, instance_id=instance.id, budget_reserved=units,
                        account_alias_snapshot=account_alias)
            if not data.get("drive_account_id") and drive_account_id:
                data["drive_account_id"] = drive_account_id
            if job.get("lease_id") is not None:
                data["lease_id"] = job["lease_id"]
            self._set(job_row, data, state=job_row.state, session=session)
        execution_id = job.get("execution_id")
        if execution_id:
            try:
                execution = self._find(session, "executions", execution_id, job["owner_id"])
            except ValueError:
                execution = None
            if execution is not None:
                data = self._data(execution)
                data.update(instance_id=instance.id, account_id=self._account_id(account))
                self._set(execution, data, state=execution.state, session=session)

    def allocate(self, job) -> dict:
        if not isinstance(job, Mapping):
            raise AllocationError("job must be an object")
        owner_id = job.get("owner_id")
        project_id = job.get("project_id")
        if not owner_id:
            raise AllocationError("job owner is required")
        key, stable_id = self._attempt_key(job)
        requested_instance = self._requested(job, "instance_id")
        requested_account = self._requested(job, "account_id")
        provider = self._provider(job)
        with self._admission(), self._tx() as session:
            model = self._model("instances")
            existing = session.scalar(select(model).where(model.owner_id == owner_id, model.id == stable_id))
            if existing is None:
                existing = session.scalar(select(model).where(model.owner_id == owner_id)) if False else None
            if existing is not None and self._data(existing).get("allocation_key") == key:
                return self._dict(existing)
            accounts = self._legal_accounts(session, owner_id, project_id, provider, requested_account)
            account_id = self._account_id(accounts[0]) if requested_account else None

            if requested_instance:
                instance = session.scalar(select(model).where(model.id == requested_instance, model.owner_id == owner_id))
                if instance is None:
                    raise AllocationError("指定实例不存在")
                data = self._data(instance)
                if instance.state in {"unknown", "cleanup_pending", "deleted", "releasing", "quarantined"}:
                    raise AllocationError("指定实例当前不可用")
                if requested_account and data.get("account_id") != account_id:
                    raise AllocationError("指定实例与账号冲突")
                account = next((a for a in accounts if self._account_id(a) == data.get("account_id")), None)
                if account is None or not self._compatible(instance, job, self._account_id(account)):
                    raise InstanceConflict("指定实例不兼容或已被占用")
                units = self._reserve_budget(session, job, account, provider)
                return self._claim_existing(session, instance, job, account, key, stable_id, "requested_instance", units)

            # Reuse is deliberately attempted before advancing the persistent RR cursor.
            idle = [r for r in self._rows(session, "instances", owner_id) if r.id != stable_id and r.state not in {"unknown", "cleanup_pending"}]
            if requested_account:
                account = accounts[0]
                candidates = [r for r in idle if self._compatible(r, job, self._account_id(account))]
            else:
                candidates = [r for r in idle if any(self._compatible(r, job, self._account_id(a)) for a in accounts)]
                account = None
                if candidates:
                    selected = sorted(candidates, key=lambda r: r.updated_at or r.created_at, reverse=True)[0]
                    account = next(a for a in accounts if self._account_id(a) == self._data(selected).get("account_id"))
                    candidates = [selected]
            if candidates:
                instance = candidates[0]
                units = self._reserve_budget(session, job, account, provider)
                return self._claim_existing(session, instance, job, account, key, stable_id, "reuse_idle", units)

            account = accounts[0] if requested_account else self._choose_account(session, accounts, owner_id, project_id)
            units = self._reserve_budget(session, job, account, provider)
            return self._create_instance(session, job, account, key, stable_id, units, "requested_account" if requested_account else "round_robin")

    # ---- scheduler lifecycle -------------------------------------------------------
    def _instance_for_job(self, session, job, owner_id=None):
        owner_id = owner_id or job.get("owner_id")
        instance_id = job.get("instance_id")
        if instance_id:
            return self._find(session, "instances", instance_id, owner_id)
        rows = self._rows(session, "instances", owner_id)
        return next((row for row in rows if self._data(row).get("current_job_id") == job.get("id") or self._data(row).get("allocation_key", "").startswith(f"{job.get('id')}:")), None)

    def _transition(self, job, state, message):
        with self._admission(), self._tx() as session:
            instance = self._instance_for_job(session, job)
            if instance is None:
                raise AllocationError("job has no instance")
            if instance.state == "unknown":
                raise InstanceConflict("unknown instance cannot be advanced")
            data = self._data(instance)
            if data.get("current_job_id") not in {None, job.get("id")}:
                raise InstanceConflict("instance is owned by another job")
            data["current_job_id"] = job.get("id")
            data["last_seen_at"] = self.clock()
            self._set(instance, data, state=state, session=session)
            self._emit(session, instance.owner_id, instance.project_id, instance.id, job.get("id"), state, message)
            return self._dict(instance)

    def mark_ready(self, job):
        return self._transition(job, "ready", "instance ready")

    def job_busy(self, job):
        return self._transition(job, "busy", "job started")

    def complete(self, job, success=True):
        with self._admission(), self._tx() as session:
            instance = self._instance_for_job(session, job)
            if instance is None:
                raise AllocationError("job has no instance")
            data = self._data(instance)
            if data.get("current_job_id") not in {None, job.get("id")}:
                raise InstanceConflict("instance is owned by another job")
            policy = self._job_policy(job)
            # The instance override is authoritative: a later job payload must
            # not turn an explicitly retained instance back into a release.
            mode = data.get("retention_mode") or job.get("lifecycle") or policy.get("lifecycle") or "save"
            hard = data.get("hard_deadline_at")
            retain = success and mode in {"retain", "smart"} and not data.get("manual_stop_requested") and (hard is None or self.clock() < float(hard))
            data.update(current_job_id=None, last_seen_at=self.clock(), retention_mode=mode)
            state = "retained_idle" if retain else "releasing"
            if retain:
                minutes = (
                    policy.get("retain_idle_minutes")
                    or data.get("retain_idle_minutes")
                    or self._cfg("default_idle_minutes", self._cfg("max_idle_minutes", 15))
                )
                minutes = min(30.0, max(1.0, float(minutes)))
                data["retain_idle_minutes"] = minutes
                data["idle_deadline_at"] = min(self.clock() + float(minutes) * 60, float(hard)) if hard is not None else self.clock() + float(minutes) * 60
            self._set(instance, data, state=state, session=session)
            self._emit(session, instance.owner_id, instance.project_id, instance.id, job.get("id"), state, "job completed" if success else "job failed")
            return self._dict(instance)

    def bind_lease(self, job, lease):
        lease_data = lease if isinstance(lease, Mapping) else {}
        lease_id = lease_data.get("id") or lease_data.get("lease_id")
        instance_id = job.get("instance_id") or lease_data.get("instance_id")
        if not lease_id:
            raise AllocationError("lease id is required")
        with self._admission(), self._tx() as session:
            instance = self._find(session, "instances", instance_id, job.get("owner_id")) if instance_id else self._instance_for_job(session, job)
            if instance is None:
                raise AllocationError("job has no instance")
            data = self._data(instance)
            if instance.state in {"deleted", "unknown", "releasing", "cleanup_pending", "quarantined"}:
                raise InstanceConflict("实例正在清理，不能接管租约")
            if data.get("cleanup_claim_token"):
                raise InstanceConflict("实例已被清理流程围栏")
            if data.get("current_job_id") not in {None, job.get("id")}:
                raise InstanceConflict("instance is owned by another job")
            deadline = lease_data.get("hard_deadline_at") or lease_data.get("deadline_epoch")
            current = data.get("hard_deadline_at")
            if deadline is not None:
                deadline = float(deadline)
                data["hard_deadline_at"] = min(float(current), deadline) if current is not None else deadline
            data.update(lease_id=lease_id, current_job_id=job.get("id"), last_seen_at=self.clock())
            self._set(instance, data, state=instance.state, session=session)
            account = self._account_row(session, data.get("account_id"), instance.owner_id)
            self._bind_job_rows(session, {**dict(job), "lease_id": lease_id}, instance, account, data.get("budget_reserved", 0.0))
            try:
                lease_row = self._find(session, "leases", lease_id, instance.owner_id)
            except ValueError:
                lease_row = None
            if lease_row is not None:
                lease_payload = self._data(lease_row)
                lease_payload.update(instance_id=instance.id, account_id=data.get("account_id"))
                self._set(lease_row, lease_payload, state=lease_row.state, session=session)
                if hasattr(lease_row, "instance_id"):
                    lease_row.instance_id = instance.id
                if hasattr(lease_row, "account_id"):
                    lease_row.account_id = data.get("account_id")
            return self._dict(instance)

    def _account_row(self, session, account_id, owner_id):
        rows = self._rows(session, "accounts", owner_id)
        return next((r for r in rows if self._account_id(r) == account_id or r.id == account_id), None)

    # ---- retention, stop and deletion ---------------------------------------------
    def _operation(self, session, owner_id, instance_id, kind, key, body):
        model = self._model("operations")
        op_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"colab-gpu-operation:{owner_id}:{instance_id}:{kind}:{key}"))
        body_hash = digest(body)
        row = session.scalar(select(model).where(model.id == op_id))
        if row is not None:
            data = self._data(row)
            if data.get("body_hash") != body_hash:
                raise InstanceConflict("idempotency key was used with a different body")
            return row, data.get("result"), True
        now = self._utcnow()
        row = model(id=op_id, owner_id=owner_id, project_id=None, state="started", version=0, data={"kind": kind, "idempotency_key": key, "body_hash": body_hash}, created_at=now, updated_at=now)
        session.add(row)
        session.flush()
        return row, None, False

    def _finish_operation(self, row, result, session):
        data = self._data(row)
        data.update(result=copy.deepcopy(result), state="completed")
        self._set(row, data, state="completed", session=session)

    def retention(self, owner, id, mode, idle_minutes, key, expected_version=None):
        if mode not in {"save", "smart", "retain"}:
            raise AllocationError("invalid retention mode")
        if not isinstance(key, str) or not key:
            raise AllocationError("idempotency key is required")
        try:
            minutes = float(idle_minutes)
        except (TypeError, ValueError) as exc:
            raise AllocationError("idle_minutes is invalid") from exc
        if minutes < 0 or minutes > float(self._cfg("max_idle_minutes", 30)):
            raise AllocationError("idle retention exceeds configured limit")
        body = {"mode": mode, "idle_minutes": minutes, "expected_version": expected_version}
        with self._admission(), self._tx() as session:
            operation, prior, done = self._operation(session, owner, id, "retention", key, body)
            if done:
                return prior
            instance = self._find(session, "instances", id, owner)
            if instance is None:
                raise KeyError(id)
            if expected_version is not None and int(instance.version) != int(expected_version):
                raise InstanceConflict("instance version conflict")
            if instance.state in {"releasing", "deleted", "unknown", "cleanup_pending", "quarantined"}:
                raise InstanceConflict("instance can no longer be retained")
            data = self._data(instance)
            provider = data.get("provider", "mock")
            hard = data.get("hard_deadline_at")
            if mode in {"retain", "smart"}:
                retention_units = self._retention_units(provider, minutes)
                if provider != "mock":
                    account = self._account_row(session, data.get("account_id"), owner)
                    if account is None:
                        raise BudgetError("GPU account is not configured")
                    previous = float(data.get("retention_reserved") or 0.0)
                    self._check_budget(session, account, max(0.0, retention_units - previous))
                data["retention_reserved"] = retention_units
                data["retain_idle_minutes"] = minutes
                deadline = self.clock() + minutes * 60
                data["idle_deadline_at"] = min(deadline, float(hard)) if hard is not None else deadline
                data["retention_mode"] = mode
                # Retention is an explicit lifecycle transition.  The
                # scheduler may still finish the current job afterwards, but
                # the durable instance policy is already retained_idle.
                state = "retained_idle"
            else:
                data.update(retention_mode="save", idle_deadline_at=None, retention_reserved=0.0)
                state = "releasing" if data.get("current_job_id") is None else instance.state
            self._set(instance, data, state=state, session=session)
            result = self.public(self._dict(instance))
            self._finish_operation(operation, result, session)
            self._emit(session, owner, instance.project_id, id, data.get("current_job_id"), state, "retention updated")
            return result

    def stop(self, owner, id, key):
        if not isinstance(key, str) or not key:
            raise AllocationError("idempotency key is required")
        body = {"action": "stop"}
        with self._admission(), self._tx() as session:
            operation, prior, done = self._operation(session, owner, id, "stop", key, body)
            if done:
                return prior
            instance = self._find(session, "instances", id, owner)
            if instance is None:
                raise KeyError(id)
            if instance.state == "deleted":
                result = self._dict(instance)
            elif instance.state == "unknown":
                raise InstanceConflict("unknown instance requires reconciliation before stop")
            else:
                data = self._data(instance)
                data.update(
                    manual_stop_requested=True, retention_mode="save", idle_deadline_at=None,
                    retention_reserved=0.0, last_seen_at=self.clock(),
                )
                self._set(instance, data, state="releasing", session=session)
                result = self._dict(instance)
                self._emit(session, owner, instance.project_id, id, data.get("current_job_id"), "releasing", "manual stop requested")
            self._finish_operation(operation, self.public(result), session)
            return result

    def mark_deleted(self, owner, id, expected_version=None, confirmed=False, remote_deleted=False):
        with self._admission(), self._tx() as session:
            instance = self._find(session, "instances", id, owner)
            if instance is None:
                raise KeyError(id)
            if instance.state == "deleted":
                return self._dict(instance)
            if expected_version is not None and instance.version != expected_version:
                raise InstanceConflict("instance version conflict")
            if confirmed is not True and remote_deleted is not True:
                raise InstanceConflict("remote deletion must be confirmed before marking deleted")
            data = self._data(instance)
            confirmed_at = self.clock()
            data.update(
                current_job_id=None, runtime_deleted=True, remote_deleted=True,
                retention_reserved=0.0, deleted_at=confirmed_at, deletion_confirmed_at=confirmed_at,
                last_seen_at=confirmed_at,
            )
            self._set(instance, data, state="deleted", session=session)
            self._emit(session, owner, instance.project_id, id, None, "deleted", "remote deletion confirmed")
            return self._dict(instance)

    # ---- accounting, events and safe reads ----------------------------------------
    def record_usage(self, owner, payload):
        if not isinstance(payload, Mapping):
            raise ValueError("usage payload must be an object")
        instance_id = payload.get("instance_id")
        if not instance_id:
            raise ValueError("instance_id is required")
        segment_key = payload.get("segment_key") or payload.get("idempotency_key")
        if not segment_key:
            segment_key = digest({k: payload.get(k) for k in ("job_id", "started_at", "ended_at", "kind")})
        segment_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"colab-gpu-usage:{owner}:{instance_id}:{segment_key}"))
        with self._tx() as session:
            instance = self._find(session, "instances", instance_id, owner)
            if instance is None:
                raise KeyError(instance_id)
            model = self._model("instance_usage_segments")
            existing = session.scalar(select(model).where(model.id == segment_id))
            if existing is not None:
                return self._public_usage(self._dict(existing))
            instance_data = self._data(instance)
            data = {k: copy.deepcopy(v) for k, v in dict(payload).items() if k not in self._SENSITIVE}
            data.update(instance_id=instance_id, account_id=instance_data.get("account_id"), segment_key=str(segment_key))
            requested_at = next(
                (payload.get(name) for name in ("allocation_requested_at", "request_started_at", "requested_at") if payload.get(name) is not None),
                instance_data.get("allocation_requested_at"),
            )
            confirmed_at = next(
                (payload.get(name) for name in ("deletion_confirmed_at", "confirmed_deleted_at", "remote_deleted_at") if payload.get(name) is not None),
                instance_data.get("deletion_confirmed_at") or instance_data.get("deleted_at"),
            )

            def timestamp(value):
                if value is None:
                    return None
                if isinstance(value, datetime):
                    return value.timestamp()
                try:
                    return float(value)
                except (TypeError, ValueError):
                    try:
                        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
                    except (TypeError, ValueError):
                        return None

            requested_epoch = timestamp(requested_at)
            confirmed_epoch = timestamp(confirmed_at)
            if requested_epoch is not None:
                data["allocation_requested_at"] = requested_at
            if confirmed_epoch is not None:
                data["deletion_confirmed_at"] = confirmed_at
            if requested_epoch is not None and confirmed_epoch is not None:
                total_seconds = max(0.0, confirmed_epoch - requested_epoch)
                data["total_duration_seconds"] = total_seconds
                data["total_duration_minutes"] = total_seconds / 60.0
            # None means not supplied by the platform; it is intentionally not
            # coerced to zero, which would present an actual bill as known.
            data.setdefault("actual_units", None)
            now = self._utcnow()
            row = model(id=segment_id, owner_id=owner, project_id=instance.project_id, state="closed" if data.get("ended_at") is not None else "open", data=data, version=0, created_at=now, updated_at=now)
            session.add(row)
            session.flush()
            return self._public_usage(self._dict(row))

    record_usage_segment = record_usage
    record_usage_once = record_usage

    def _emit(self, session, owner, project_id, instance_id, job_id, state, message):
        try:
            model = self._model("events")
        except ValueError:
            return
        data = {"instance_id": instance_id, "job_id": job_id, "state": state, "message": str(message)[:500]}
        now = self._utcnow()
        session.add(model(id=str(uuid.uuid4()), owner_id=owner, project_id=project_id, state=state, version=0, data=data, created_at=now, updated_at=now))

    @classmethod
    def _redact(cls, value):
        if isinstance(value, Mapping):
            return {k: cls._redact(v) for k, v in value.items() if str(k).lower() not in cls._SENSITIVE}
        if isinstance(value, list):
            return [cls._redact(v) for v in value]
        return value

    def _public_usage(self, row):
        allowed = {
            "id", "owner_id", "project_id", "instance_id", "account_id", "job_id", "segment_key",
            "started_at", "ended_at", "allocation_requested_at", "deletion_confirmed_at",
            "total_duration_seconds", "total_duration_minutes", "estimated_units", "actual_units",
            "state", "version", "created_at", "updated_at",
        }
        return {k: self._redact(v) for k, v in row.items() if k in allowed}

    def public(self, instance):
        source = dict(instance) if isinstance(instance, Mapping) else self._dict(instance)
        return {k: self._redact(copy.deepcopy(source[k])) for k in self._PUBLIC if k in source}

    def get(self, owner, id):
        with self._tx() as session:
            row = self._find(session, "instances", id, owner)
            return None if row is None else self.public(self._dict(row))

    def list(self, owner, state=None):
        with self._tx() as session:
            rows = self._rows(session, "instances", owner)
            return [self.public(self._dict(row)) for row in rows if state is None or row.state == state]

    def events(self, owner, id):
        with self._tx() as session:
            rows = self._rows(session, "events", owner)
            return [self._redact(self._dict(row)) for row in rows if self._data(row).get("instance_id") == id]

    def usage(self, owner, id=None):
        with self._tx() as session:
            rows = self._rows(session, "instance_usage_segments", owner)
            return [self._public_usage(self._dict(row)) for row in rows if id is None or self._data(row).get("instance_id") == id]

    # Friendly aliases used by API adapters.
    instance = get
    instances = list
    usage_segments = usage
