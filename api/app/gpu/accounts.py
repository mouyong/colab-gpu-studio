"""Multi-account Google identity directory and safe public projections."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any

from app.common import APIError

# Keep this algorithm byte-for-byte compatible with migration 0003.
DEFAULT_ACCOUNT_NAMESPACE = uuid.NAMESPACE_URL
DEFAULT_ACCOUNT_PREFIX = "colab-gpu-studio:v0.2.0:legacy-account:"


class AccountService:
    """Owner-scoped account directory.

    Repository writes are deliberately small and contain no network calls. OAuth
    and remote runtime operations happen after the directory checks, outside any
    repository transaction.
    """

    PUBLIC_FIELDS = frozenset(
        {
            "account_id",
            "alias",
            "enabled",
            "allow_allocation",
            "drive_account_id",
            "drive_account_alias",
            "projects",
            "workflows",
            "budget",
            "strategy",
            "status",
            "authorization",
            "instance_count",
            "active_instance_count",
            "created_at",
            "updated_at",
            "last_error_code",
            "last_checked_at",
        }
    )
    MUTABLE_FIELDS = frozenset({"alias", "enabled", "allow_allocation", "drive_account_id", "projects", "workflows", "budget", "strategy"})
    ACTIVE_INSTANCE_STATES = frozenset(
        {"requested", "allocating", "ready", "busy", "retained_idle", "releasing", "unknown", "cleanup_pending", "quarantined"}
    )

    def __init__(self, repo, owner_id: str):
        if repo is None or not isinstance(owner_id, str) or not owner_id:
            raise ValueError("AccountService需要Repository和owner_id")
        self.repo = repo
        self.owner_id = owner_id

    @staticmethod
    def default_account_id(owner_id: str) -> str:
        return str(uuid.uuid5(DEFAULT_ACCOUNT_NAMESPACE, DEFAULT_ACCOUNT_PREFIX + owner_id))

    @staticmethod
    def _hash(value: Any) -> str:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hashlib.sha256(raw).hexdigest()

    def _rows(self, kind: str) -> list[dict]:
        return self.repo.list(kind, self.owner_id)

    def _get(self, account_id: str) -> dict | None:
        # Account records use the account id as their row id.
        return self.repo.get("accounts", account_id, self.owner_id)

    def _operation(self, key: str | None, body: Any) -> dict | None:
        if not key:
            return None
        digest = self._hash(body)
        for row in self._rows("operations"):
            if row.get("idempotency_key") != key:
                continue
            if row.get("body_hash") != digest:
                raise APIError("IDEMPOTENCY_CONFLICT", "幂等键已用于其他参数", 409)
            result = row.get("result")
            if isinstance(result, dict):
                return result
            return {"operation_id": row.get("id"), "state": row.get("state", "completed")}
        return None

    def _save_operation(self, key: str | None, body: Any, result: dict, *, operation: str) -> dict:
        if not key:
            return result
        data = {
            "kind": "account_operation",
            "operation": operation,
            "idempotency_key": key,
            "body_hash": self._hash(body),
            "state": "completed",
            "result": result,
        }
        try:
            self.repo.create("operations", self.owner_id, data, record_id=hashlib.sha256(f"{self.owner_id}:{key}".encode()).hexdigest())
        except ValueError:
            # A concurrent request with the same key won the insert; return its result.
            existing = self._operation(key, body)
            if existing is not None:
                return existing
            raise
        return result

    def _ensure_default(self) -> None:
        # Existing explicit accounts must remain untouched, but they do not
        # prove that unscoped pre-v0.2 rows were repaired.  Materialize the
        # deterministic default whenever a legacy row still lacks identity.
        legacy = [
            *[row for row in self._rows("credentials") if row.get("purpose") in {"colab", "drive"}],
            *self._rows("jobs"),
            *self._rows("leases"),
            *self._rows("instances"),
        ]
        if not any(row.get("account_id") in (None, "") for row in legacy):
            return
        account_id = self.default_account_id(self.owner_id)
        if self._get(account_id) is None:
            data = {
                "account_id": account_id,
                "alias": "Google账号",
                "enabled": True,
                "allow_allocation": False,
                "projects": [],
                "workflows": [],
                "budget": {},
                "strategy": "manual",
                "legacy_default": True,
            }
            try:
                self.repo.create("accounts", self.owner_id, data, record_id=account_id)
            except ValueError:
                pass  # created by a concurrent request
        # Migration is exact and local: it never exchanges or invents tokens.
        for kind in ("credentials", "jobs", "leases", "instances", "executions", "files"):
            for row in self._rows(kind):
                if row.get("account_id") in (None, ""):
                    self.repo.update(kind, row["id"], {"account_id": account_id}, self.owner_id)

    @staticmethod
    def _clean_text(value: Any, limit: int, default: str = "") -> str:
        return str(value if value is not None else default).strip()[:limit]

    def _validate_drive_account(self, drive_account_id: Any, *, self_account_id: str | None = None) -> tuple[str | None, dict | None]:
        if drive_account_id in (None, ""):
            return None, None
        if not isinstance(drive_account_id, str):
            raise APIError("VALIDATION_ERROR", "drive_account_id必须为字符串或null", 422)
        target_id = drive_account_id.strip()
        if not target_id or (self_account_id and target_id == self_account_id):
            return None, None
        self._ensure_default()
        target = self._get(target_id)
        if not target:
            raise APIError("NOT_FOUND", "绑定的Drive账号不存在或不属于当前用户", 404)
        resolved_id = str(target.get("account_id") or target.get("id"))
        if target.get("drive_account_id"):
            raise APIError("VALIDATION_ERROR", "不支持链式绑定Drive账号，请直接绑定拥有Drive授权的账号", 422)
        from .google import GoogleService

        drive_status = GoogleService(self.repo, self.owner_id, account_id=resolved_id).status().get("drive") or {}
        if not drive_status.get("connected"):
            raise APIError("DRIVE_ACCOUNT_UNAVAILABLE", "目标账号尚未连接Google Drive，无法作为共享存储盘", 409)
        return resolved_id, target

    def resolve_drive_account_id(self, account_id: str | None) -> str | None:
        if not account_id:
            return account_id
        row = self._get(account_id)
        if not row:
            return account_id
        delegated = row.get("drive_account_id")
        if isinstance(delegated, str) and delegated.strip() and delegated.strip() != account_id:
            target = self._get(delegated.strip())
            if target:
                return str(target.get("account_id") or target.get("id"))
        return account_id

    def _status(self, account_id: str, enabled: bool, drive_account_id: str | None = None) -> tuple[str, dict[str, dict], str | None]:
        from .google import GoogleService

        google = GoogleService(self.repo, self.owner_id, account_id=account_id)
        authorization = google.status()
        drive_alias = None
        if isinstance(drive_account_id, str) and drive_account_id.strip() and drive_account_id.strip() != account_id:
            target = self._get(drive_account_id.strip())
            if target:
                target_id = str(target.get("account_id") or target.get("id"))
                drive_alias = self._clean_text(target.get("alias"), 80, "主账号")
                target_drive = GoogleService(self.repo, self.owner_id, account_id=target_id).status().get("drive") or {}
                authorization["drive"] = {
                    **target_drive,
                    "delegated_to": target_id,
                    "delegated_alias": drive_alias,
                }
            else:
                authorization["drive"] = {
                    "connected": False,
                    "configured": authorization.get("drive", {}).get("configured", False),
                    "expires_at": None,
                    "delegated_to": drive_account_id.strip(),
                    "message": "绑定的Drive账号不存在，请重新选择",
                }
        if not enabled:
            return "disabled", authorization, drive_alias
        if any(value.get("message") for value in authorization.values()):
            return "needs_reauth", authorization, drive_alias
        if any(value.get("connected") for value in authorization.values()):
            return "connected", authorization, drive_alias
        return "not_connected", authorization, drive_alias

    def _public(self, row: dict) -> dict:
        account_id = str(row.get("account_id") or row.get("id"))
        raw_drive_id = row.get("drive_account_id")
        drive_account_id = raw_drive_id.strip() if isinstance(raw_drive_id, str) and raw_drive_id.strip() and raw_drive_id.strip() != account_id else None
        status, authorization, drive_alias = self._status(account_id, row.get("enabled", True) is not False, drive_account_id)
        instances = [item for item in self._rows("instances") if item.get("account_id") == account_id]
        result = {
            "account_id": account_id,
            "alias": self._clean_text(row.get("alias"), 80, "未命名账号"),
            "enabled": row.get("enabled", True) is not False,
            "allow_allocation": bool(row.get("allow_allocation", False)),
            "drive_account_id": drive_account_id,
            "drive_account_alias": drive_alias,
            "projects": list(row.get("projects") or []),
            "workflows": list(row.get("workflows") or []),
            "budget": dict(row.get("budget") or {}) if isinstance(row.get("budget") or {}, dict) else row.get("budget"),
            "strategy": self._clean_text(row.get("strategy"), 32, "manual"),
            "status": status,
            "authorization": authorization,
            "instance_count": len(instances),
            "active_instance_count": sum(item.get("state") in self.ACTIVE_INSTANCE_STATES for item in instances),
        }
        for key in ("created_at", "updated_at", "last_error_code", "last_checked_at"):
            if row.get(key) is not None:
                result[key] = row[key]
        return {key: value for key, value in result.items() if key in self.PUBLIC_FIELDS}

    def list(self) -> list[dict]:
        self._ensure_default()
        return [self._public(row) for row in self._rows("accounts")]

    def require(self, account_id: str) -> dict:
        self._ensure_default()
        row = self._get(account_id)
        if not row:
            raise APIError("NOT_FOUND", "Google账号不存在或不属于当前用户", 404)
        return row

    def create(self, body: dict, idempotency_key: str | None = None) -> dict:
        if not isinstance(body, dict):
            raise APIError("VALIDATION_ERROR", "账号参数必须是对象", 422)
        existing = self._operation(idempotency_key, body)
        if existing is not None:
            return existing
        unknown = set(body) - set(self.MUTABLE_FIELDS)
        if unknown:
            raise APIError("VALIDATION_ERROR", "账号参数包含不支持的字段", 422)
        alias = self._clean_text(body.get("alias"), 80)
        if not alias:
            raise APIError("VALIDATION_ERROR", "账号别名不能为空", 422)
        account_id = str(uuid.uuid4())
        drive_account_id, _ = self._validate_drive_account(body.get("drive_account_id"), self_account_id=account_id)
        data = {
            "account_id": account_id,
            "alias": alias,
            "enabled": body.get("enabled", True) is not False,
            "allow_allocation": bool(body.get("allow_allocation", False)),
            "drive_account_id": drive_account_id,
            "projects": [self._clean_text(item, 128) for item in body.get("projects", [])] if isinstance(body.get("projects", []), list) else [],
            "workflows": [self._clean_text(item, 128) for item in body.get("workflows", [])] if isinstance(body.get("workflows", []), list) else [],
            "budget": body.get("budget") if isinstance(body.get("budget"), dict) else {},
            "strategy": self._clean_text(body.get("strategy"), 32, "manual"),
        }
        self.repo.create("accounts", self.owner_id, data, record_id=account_id)
        return self._save_operation(idempotency_key, body, self._public(data), operation="create")

    def update(self, account_id: str, body: dict, idempotency_key: str | None = None) -> dict:
        self.require(account_id)
        existing = self._operation(idempotency_key, {"account_id": account_id, **(body or {})})
        if existing is not None:
            return existing
        if not isinstance(body, dict) or not body or set(body) - set(self.MUTABLE_FIELDS):
            raise APIError("VALIDATION_ERROR", "账号修改参数无效", 422)
        patch = dict(body)
        if "alias" in patch:
            patch["alias"] = self._clean_text(patch["alias"], 80)
            if not patch["alias"]:
                raise APIError("VALIDATION_ERROR", "账号别名不能为空", 422)
        if "enabled" in patch and not isinstance(patch["enabled"], bool):
            raise APIError("VALIDATION_ERROR", "enabled必须为布尔值", 422)
        if "allow_allocation" in patch and not isinstance(patch["allow_allocation"], bool):
            raise APIError("VALIDATION_ERROR", "allow_allocation必须为布尔值", 422)
        if "projects" in patch and not isinstance(patch["projects"], list):
            raise APIError("VALIDATION_ERROR", "projects必须为数组", 422)
        if "workflows" in patch and not isinstance(patch["workflows"], list):
            raise APIError("VALIDATION_ERROR", "workflows必须为数组", 422)
        if "drive_account_id" in patch:
            patch["drive_account_id"], _ = self._validate_drive_account(patch["drive_account_id"], self_account_id=account_id)
        updated = self.repo.update("accounts", account_id, patch, self.owner_id)
        return self._save_operation(idempotency_key, {"account_id": account_id, **body}, self._public(updated), operation="update")

    def disconnect(self, account_id: str, idempotency_key: str | None = None, body: dict | None = None) -> dict:
        self.require(account_id)
        body = {"account_id": account_id, **(body or {})}
        existing = self._operation(idempotency_key, body)
        if existing is not None:
            return existing
        active = [
            item
            for item in self._rows("instances")
            if item.get("account_id") == account_id and item.get("state") in self.ACTIVE_INSTANCE_STATES
        ]
        active += [
            item
            for item in self._rows("leases")
            if item.get("account_id") == account_id and item.get("state") not in {None, "deleted", "released"}
        ]
        if active:
            raise APIError("ACCOUNT_HAS_ACTIVE_RESOURCES", "账号仍有活动资源，停止并确认收尾后再断开", 409)
        credential_rows = [row for row in self._rows("credentials") if row.get("account_id") == account_id]
        if os.getenv("STUDIO_OFFLINE_TEST") == "1":
            for credential in credential_rows:
                self.repo.update("credentials", credential["id"], {"revoked": True, "encrypted_access_token": None, "encrypted_refresh_token": None, "disconnect_status": "offline_unconfirmed"}, self.owner_id)
            result = {"account_id": account_id, "disconnected": False, "remote_confirmed": False, "status": "remote_unconfirmed"}
            return self._save_operation(idempotency_key, body, result, operation="disconnect")
        from .google import GoogleService

        service = GoogleService(self.repo, self.owner_id, account_id=account_id)
        remote_confirmed = True
        for purpose in ("colab", "drive"):
            had_credential = any(item.get("purpose") == purpose and not item.get("revoked") for item in credential_rows)
            outcome = service.disconnect(purpose)
            if had_credential and not outcome:
                remote_confirmed = False
        result = {"account_id": account_id, "disconnected": remote_confirmed, "remote_confirmed": remote_confirmed, "status": "confirmed" if remote_confirmed else "remote_unconfirmed"}
        return self._save_operation(idempotency_key, body, result, operation="disconnect")

    def runtimes(self, account_id: str) -> list[dict]:
        self.require(account_id)
        local = [
            {
                key: item.get(key)
                for key in ("instance_id", "id", "state", "current_job_id", "gpu_reported", "runtime_spec_reported", "last_seen_at", "retention_mode")
                if item.get(key) is not None
            }
            for item in self._rows("instances")
            if item.get("account_id") == account_id
        ]
        if os.getenv("STUDIO_OFFLINE_TEST") == "1":
            return local
        from .colab import GoogleColabProvider

        provider = GoogleColabProvider(self.repo, self.owner_id, account_id=account_id)
        return [{key: row.get(key) for key in ("name", "runtimeSpec", "version") if row.get(key) is not None} for row in provider.list_runtimes()]


__all__ = ["AccountService", "DEFAULT_ACCOUNT_NAMESPACE"]
