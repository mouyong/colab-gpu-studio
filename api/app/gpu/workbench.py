"""A narrow, offline-safe Comfy workbench domain service.

The workbench is deliberately not a proxy.  It accepts a fixed set of domain
operations and can only operate on an instance-owned, published workflow
snapshot.  A real runtime needs a separately verified MCP adapter; without one
all real operations fail closed with ``COMFY_UNVERIFIED``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from collections.abc import Mapping

from ..common import APIError
from ..gpu.manifest import bind_parameters, resolve_parameters

WORKBENCH_ACTIONS = frozenset(
    {
        "health",
        "list_allowed_workflows",
        "get_reviewed_graph",
        "set_reviewed_params",
        "queue_reviewed_workflow",
        "job_status",
        "read_safe_log",
        "preview_artifacts",
        "stop_current_job",
    }
)
WRITE_ACTIONS = frozenset({"set_reviewed_params", "queue_reviewed_workflow", "stop_current_job"})
SENSITIVE_KEYS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "connection_token",
        "connectioninfo",
        "connection_info",
        "authorization",
        "cookie",
        "secret",
        "password",
        "credential",
        "credentials",
        "api_key",
        "apikey",
        "client_secret",
        "clientsecret",
        "private_key",
        "privatekey",
        "secret_key",
        "secretkey",
        "handle",
        "runtime_handle",
        "signed_url",
        "url",
        "code",
        "shell",
        "python",
        "node",
        "nodes",
        "api_graph",
        "editable_graph",
    }
)


class WorkbenchError(APIError):
    """A typed API error that also works for direct service callers."""

    def __init__(self, code: str, message: str, status: int = 422, retryable: bool = False):
        super().__init__(code, message, status, retryable)


class _OperationLedger:
    """Repository-backed idempotency ledger for workbench write actions."""

    def __init__(self, repo):
        self.repo = repo
        self._lock = threading.RLock()

    @staticmethod
    def digest(body: Mapping) -> str:
        return hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()

    @staticmethod
    def _id(owner: str, instance_id: str, action: str, key: str) -> str:
        return hashlib.sha256(
            json.dumps([owner, instance_id, action, key], ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()

    def _load(self, owner: str, instance_id: str, action: str, key: str):
        return self.repo.get("operations", self._id(owner, instance_id, action, key), owner)

    def existing(self, owner: str, instance_id: str, action: str, key: str, body: Mapping):
        body_hash = self.digest(body)
        with self._lock:
            row = self._load(owner, instance_id, action, key)
            if not row:
                return None
            if row.get("body_hash") != body_hash:
                raise WorkbenchError("IDEMPOTENCY_CONFLICT", "相同幂等键不能用于不同工作台请求", 409)
            return copy.deepcopy(row.get("result"))

    def put(self, owner: str, instance_id: str, action: str, key: str, body: Mapping, result):
        operation_id = self._id(owner, instance_id, action, key)
        data = {
            "instance_id": instance_id,
            "action": action,
            "key": key,
            "body_hash": self.digest(body),
            "result": copy.deepcopy(result),
        }
        with self._lock:
            try:
                self.repo.create("operations", owner, data, record_id=operation_id)
            except ValueError:
                # Another process stored the same key first.
                existing = self._load(owner, instance_id, action, key)
                if not existing:
                    raise
                if existing.get("body_hash") != data["body_hash"]:
                    raise WorkbenchError("IDEMPOTENCY_CONFLICT", "相同幂等键不能用于不同工作台请求", 409)
        return result


class WorkbenchService:
    """Controlled workbench with a deterministic mock backend.

    A trusted real adapter may be supplied by the server, but it is never
    selected from a client-provided URL.
    """

    def __init__(self, repo=None, *, registry=None, admit_job=None, trusted_mcp=None, clock=None):
        self.repo = repo
        self.registry = registry
        self.admit_job = admit_job
        self.trusted_mcp = trusted_mcp
        self.clock = clock or time.time
        self._ops = _OperationLedger(repo)

    @staticmethod
    def _is_mock(instance: Mapping) -> bool:
        return bool(instance.get("mock") or instance.get("is_mock") or instance.get("provider") == "mock")

    @staticmethod
    def _is_history(instance: Mapping) -> bool:
        return instance.get("state") in {"unknown", "deleted", "releasing", "cleanup_pending", "quarantined"}

    def _rows(self, kind: str, owner: str):
        source = self.repo.store if kind == "assets" else self.repo
        return source.list(kind, owner)

    def _workflows(self, owner: str, *, include_drafts: bool = False):
        rows = self.registry.workflows(owner) if self.registry is not None else self._rows("workflows", owner)
        return [row for row in rows if include_drafts or row.get("state") == "published"]

    @staticmethod
    def _workflow_identity(payload: Mapping):
        key = payload.get("workflow_key") or payload.get("key")
        version = payload.get("workflow_version", payload.get("version"))
        if not isinstance(key, str) or not key or version in (None, ""):
            raise WorkbenchError("VALIDATION_ERROR", "必须指定已发布工作流 key 和 version")
        return key, version

    def _workflow(self, owner: str, payload: Mapping, *, include_drafts: bool = False):
        workflow_id = payload.get("workflow_id")
        if workflow_id:
            for row in self._workflows(owner, include_drafts=include_drafts):
                if row.get("id") == workflow_id:
                    return row
            raise WorkbenchError("WORKFLOW_UNPUBLISHED", "工作流未发布或不属于当前用户", 409)
        key, version = self._workflow_identity(payload)
        for row in self._workflows(owner, include_drafts=include_drafts):
            if row.get("workflow_key") == key and row.get("workflow_version") == version:
                return row
        raise WorkbenchError("WORKFLOW_UNPUBLISHED", "工作流未发布或不属于当前用户", 409)

    def _mock_or_unverified(self, instance: Mapping):
        if self._is_mock(instance):
            return
        if self.trusted_mcp is None:
            raise WorkbenchError("COMFY_UNVERIFIED", "真实实例尚未接入受信 Comfy MCP 协议", 503, True)

    def _forbid_client_surface(self, payload: Mapping):
        # These names are never accepted, even when nested.  Graphs are read
        # from the published registry and URLs are owned by the server.
        def scan(value):
            if isinstance(value, Mapping):
                for key, item in value.items():
                    if re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).lower() in SENSITIVE_KEYS:
                        raise WorkbenchError("WORKBENCH_INPUT_FORBIDDEN", "工作台不接受任意代码、URL、节点或凭据", 422)
                    scan(item)
            elif isinstance(value, list):
                for item in value:
                    scan(item)

        scan(payload)

    def _write_draft(self, owner, instance, workflow, params, bound_graph):
        identity = f"{workflow['workflow_key']}@{workflow['workflow_version']}"
        value = {"params": copy.deepcopy(params), "bound_graph": copy.deepcopy(bound_graph), "draft": True}
        persisted = self.repo.get("instances", instance["id"], owner) or instance
        drafts = dict(persisted.get("workbench_drafts") or {})
        drafts[identity] = value
        self.repo.update("instances", instance["id"], {"workbench_drafts": drafts}, owner)
        return copy.deepcopy(value)

    @staticmethod
    def _is_verified(instance: Mapping) -> bool:
        """Only explicit runtime evidence can make a real instance writable."""
        return WorkbenchService._is_mock(instance) or bool(
            instance.get("verified") is True or instance.get("runtime_verified") is True
        )

    @classmethod
    def _safe_public_value(cls, value):
        """Copy public JSON while removing credential/URL-shaped fields."""
        blocked = {
            re.sub(r"[^a-z0-9]+", "_", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).lower()).strip("_")
            for key in SENSITIVE_KEYS
        }
        if isinstance(value, Mapping):
            result = {}
            for key, item in value.items():
                normalized = re.sub(r"[^a-z0-9]+", "_", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).lower()).strip("_")
                if normalized in blocked or "url" in normalized:
                    continue
                result[str(key)] = cls._safe_public_value(item)
            return result
        if isinstance(value, list):
            return [cls._safe_public_value(item) for item in value]
        if isinstance(value, tuple):
            return [cls._safe_public_value(item) for item in value]
        if isinstance(value, str):
            return re.sub(r"(?:https?|wss?)://[^\s\"'<>]+", "[REDACTED_URL]", value)
        return copy.deepcopy(value)

    @classmethod
    def _safe_preview_graph(cls, value):
        """Return graph metadata without turning preview into a runtime proxy.

        Published graphs are still treated as untrusted presentation data at
        this boundary.  Keep node/link structure useful to the viewer, while
        dropping executable or credential-shaped fields.  In particular this
        method intentionally does not reuse ``_safe_public_value`` because a
        UI graph commonly has a legitimate ``nodes`` collection.
        """
        blocked = {
            "authorization",
            "cookie",
            "password",
            "secret",
            "token",
            "access_token",
            "refresh_token",
            "connection_token",
            "connection_info",
            "connectioninfo",
            "connection_string",
            "connectionstring",
            "credential",
            "credentials",
            "api_key",
            "apikey",
            "client_secret",
            "clientsecret",
            "private_key",
            "privatekey",
            "secret_key",
            "secretkey",
            "runtime_handle",
            "signed_url",
            "python",
            "shell",
            "command",
            "script",
            "install",
            "websocket",
        }
        if isinstance(value, Mapping):
            blocked = {
                re.sub(r"[^a-z0-9]+", "_", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).lower()).strip("_")
                for key in blocked
            }
            result = {}
            for key, item in value.items():
                normalized = re.sub(r"[^a-z0-9]+", "_", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).lower()).strip("_")
                if normalized in blocked or "url" in normalized:
                    continue
                result[str(key)] = cls._safe_preview_graph(item)
            return result
        if isinstance(value, list):
            return [cls._safe_preview_graph(item) for item in value]
        if isinstance(value, tuple):
            return [cls._safe_preview_graph(item) for item in value]
        if isinstance(value, str):
            return re.sub(r"(?:https?|wss?)://[^\s\"'<>]+", "[REDACTED_URL]", value)
        return copy.deepcopy(value)

    @staticmethod
    def _evidence_status(value):
        """Map stored booleans to honest review evidence states.

        ``None``/absent means that this offline snapshot has no evidence.  It
        must not be rendered as a failed check or as a successful runtime
        probe.  A stored ``False`` is kept distinct as an explicit
        not-confirmed result.
        """
        if value is True:
            return "confirmed"
        if value is False:
            return "not_confirmed"
        return "not_detected"

    @classmethod
    def _public_weight_files(cls, manifest):
        dependencies = manifest.get("dependencies") if isinstance(manifest, Mapping) else None
        raw = manifest.get("weight_files") if isinstance(manifest, Mapping) else None
        if not isinstance(raw, list) and isinstance(dependencies, Mapping):
            raw = dependencies.get("weight_files")
        if not isinstance(raw, list):
            return []

        result = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            public = {}
            for key in ("repo_id", "filename", "revision", "sha256", "target", "size"):
                if item.get(key) is not None:
                    public[key] = cls._safe_public_value(item[key])
            metadata_complete = all(
                isinstance(public.get(key), str) and bool(public[key])
                for key in ("repo_id", "filename", "revision", "sha256", "target")
            )
            public["metadata"] = {
                "status": "confirmed" if metadata_complete else "confirmed_missing",
                "label": "已确认" if metadata_complete else "清单字段缺失",
            }

            verification = item.get("verification")
            trusted_sources = {"prep_report", "runtime_worker", "worker", "cache_probe"}
            trusted = (
                isinstance(verification, Mapping)
                and verification.get("verified") is True
                and verification.get("source") in trusted_sources
            )
            explicit = verification.get("status") if isinstance(verification, Mapping) else None
            if trusted and str(explicit).lower() in {"missing", "absent", "confirmed_missing"}:
                availability_status = "confirmed_missing"
            elif trusted and str(explicit).lower() in {"present", "available", "verified"}:
                availability_status = "confirmed"
            else:
                # A manifest's self-asserted ``present``/``missing`` flag is
                # not a cache probe.  Only a separately marked, trusted
                # verification result may confirm runtime availability.
                availability_status = "not_detected"
            public["availability"] = {
                "status": availability_status,
                "label": {
                    "confirmed": "已确认存在",
                    "confirmed_missing": "确认缺失",
                    "not_detected": "未检测",
                }[availability_status],
                "source": "stored_manifest" if availability_status == "not_detected" else "stored_evidence",
            }
            result.append(public)
        return result

    @classmethod
    def _node_references(cls, graph):
        if not isinstance(graph, Mapping):
            return []
        result = []
        for node_id, node in graph.items():
            if not isinstance(node, Mapping):
                continue
            class_type = node.get("class_type")
            if not isinstance(class_type, str):
                continue
            result.append(
                {
                    "node_id": str(node_id),
                    "class_type": class_type,
                    "inputs": cls._safe_preview_graph(node.get("inputs", {})),
                }
            )
        return result

    @classmethod
    def _review_evidence(cls, workflow, model, node_references, manifest, weight_files):
        allowlist = manifest.get("node_allowlist") if isinstance(manifest, Mapping) else None
        if not isinstance(allowlist, list):
            dependencies = manifest.get("dependencies") if isinstance(manifest, Mapping) else None
            allowlist = dependencies.get("node_allowlist") if isinstance(dependencies, Mapping) else None
        classes = sorted({item["class_type"] for item in node_references})
        node_reviewed = (
            isinstance(allowlist, list)
            and bool(classes)
            and all(class_type in allowlist for class_type in classes)
        )
        if isinstance(allowlist, list) and classes and not node_reviewed:
            node_status = "not_in_allowlist"
        else:
            node_status = "confirmed" if node_reviewed else "not_detected"
        weight_statuses = [item["metadata"]["status"] for item in weight_files]
        if not weight_files:
            weight_status = "not_detected"
        elif all(status == "confirmed" for status in weight_statuses):
            weight_status = "confirmed"
        else:
            weight_status = "confirmed_missing"
        model_review = {
            "status": cls._evidence_status(model.get("review_approved") if isinstance(model, Mapping) else None),
            "review_approved": model.get("review_approved") if isinstance(model, Mapping) else None,
            "license_accepted": model.get("license_accepted") if isinstance(model, Mapping) else None,
        }
        return {
            "workflow": {
                "status": cls._evidence_status(workflow.get("review_approved")),
                "review_approved": workflow.get("review_approved"),
                "reviewed_by": cls._safe_public_value(workflow.get("reviewed_by"))
                if workflow.get("reviewed_by") is not None
                else None,
                "reviewed_at": workflow.get("reviewed_at"),
            },
            "model": model_review,
            "nodes": {
                "status": node_status,
                "classes": classes,
                "allowlist": cls._safe_public_value(allowlist) if isinstance(allowlist, list) else [],
            },
            "weights": {"status": weight_status},
            "smoke": {
                "status": cls._evidence_status(workflow.get("smoke_test_passed")),
                "passed": workflow.get("smoke_test_passed"),
                "kind": cls._safe_public_value(workflow.get("smoke_test_kind")),
            },
        }

    def get_reviewed_graph(self, owner, payload: Mapping):
        """Read an owner-scoped graph without requiring a live/verified instance.

        Published previews keep the review boundary; an explicit allow_draft
        request exposes only the same sanitized graph as read-only presentation.
        """
        allow_draft = payload.get("allow_draft") is True
        workflow = self._workflow(owner, payload, include_drafts=allow_draft)
        if workflow.get("state") != "published" and not allow_draft:
            raise WorkbenchError("WORKFLOW_UNPUBLISHED", "工作流未发布或不属于当前用户", 409)
        graph = workflow.get("api_graph")
        if not isinstance(graph, Mapping):
            raise WorkbenchError("WORKFLOW_INVALID", "已发布工作流缺少受审查图", 409)

        editable = workflow.get("editable_graph")
        manifest = workflow.get("manifest") if isinstance(workflow.get("manifest"), Mapping) else {}
        weight_files = self._public_weight_files(manifest)
        node_references = self._node_references(graph)
        models = []
        model_row = None
        for row in self._rows("models", owner):
            if row.get("model_id") != workflow.get("model_id"):
                continue
            model_row = row
            summary = {"model_id": row.get("model_id")}
            for key in (
                "name",
                "display_name",
                "description",
                "enabled",
                "is_mock",
                "license_status",
                "disabled_reason",
                "revision",
                "sha256",
                "review_approved",
                "license_accepted",
            ):
                if row.get(key) is not None:
                    summary[key] = self._safe_public_value(row[key])
            models.append(summary)

        dependencies = manifest.get("dependencies") if isinstance(manifest, Mapping) else None
        comfyui_commit = manifest.get("comfyui_commit")
        if comfyui_commit is None and isinstance(dependencies, Mapping):
            comfyui_commit = dependencies.get("comfyui_commit")
        custom_nodes = manifest.get("custom_nodes")
        if custom_nodes is None and isinstance(dependencies, Mapping):
            custom_nodes = dependencies.get("custom_nodes")
        manifest_evidence = {
            "version": self._safe_public_value(manifest.get("version", manifest.get("manifest_version"))),
            "comfyui_commit": self._safe_public_value(comfyui_commit),
            "custom_nodes": self._safe_public_value(custom_nodes or []),
            "node_allowlist": self._safe_public_value(manifest.get("node_allowlist", [])),
            "weight_files": copy.deepcopy(weight_files),
        }

        return {
            "workflow_key": workflow["workflow_key"],
            "workflow_version": workflow["workflow_version"],
            "model_id": workflow.get("model_id"),
            "description": workflow.get("description"),
            "reviewed": workflow.get("state") == "published",
            "published": workflow.get("state") == "published",
            "state": workflow.get("state"),
            "ui_graph": self._safe_preview_graph(editable) if isinstance(editable, (Mapping, list)) else None,
            "api_graph": self._safe_preview_graph(graph),
            "parameters": self._public_parameters(workflow.get("manifest") or {}),
            "models": models,
            "manifest": manifest_evidence,
            "weight_files": weight_files,
            "node_references": node_references,
            "review": self._review_evidence(workflow, model_row, node_references, manifest, weight_files),
            "runtime": {
                "mode": "offline",
                "gpu_detection": {
                    "status": "not_detected",
                    "label": "未检测",
                    "reason": "离线预览不启动实例、不执行节点，也不探测 GPU",
                },
                "native_comfy_ui": False,
                "native_comfy_ui_label": "非原生 ComfyUI 在线 UI",
            },
        }

    @classmethod
    def _public_parameters(cls, manifest):
        raw = manifest.get("parameters") if isinstance(manifest, Mapping) else None
        if isinstance(raw, Mapping):
            result = []
            for key, definition in raw.items():
                if not isinstance(definition, Mapping):
                    continue
                item = cls._safe_public_value(definition)
                item.setdefault("key", str(key))
                result.append(item)
            return result
        if isinstance(raw, list):
            result = []
            for item in raw:
                if not isinstance(item, Mapping):
                    continue
                public = cls._safe_public_value(item)
                key = public.get("key") or public.get("id") or public.get("name")
                if key is not None:
                    public.setdefault("key", str(key))
                result.append(public)
            return result
        return []

    def _draft_values(self, owner, instance):
        if instance.get("owner_id") not in (None, owner):
            return {}
        source = self.repo.get("instances", instance.get("id"), owner) or instance
        drafts = source.get("workbench_drafts") or {}
        return {identity: self._safe_public_value(draft["params"]) for identity, draft in drafts.items()}

    @staticmethod
    def _safe_artifact(asset: Mapping, job_id: str):
        result = {"job_id": job_id}
        allowed = {"id", "name", "filename", "mime", "status", "created_at"}
        for key in allowed:
            if key in asset and asset[key] is not None:
                result[key] = WorkbenchService._safe_public_value(asset[key])
        thumbnail = asset.get("thumbnail_url")
        # Only a server-relative path is safe to hand to the browser.  Drop
        # query strings so a signed URL/token cannot cross this boundary.
        if isinstance(thumbnail, str) and thumbnail.startswith("/") and not thumbnail.startswith("//"):
            result["thumbnail_url"] = thumbnail.split("?", 1)[0].split("#", 1)[0]
        return result

    def _describe_outputs(self, owner, instance):
        instance_id = instance.get("id")
        jobs = [
            job
            for job in self._rows("jobs", owner)
            if job.get("owner_id", owner) == owner and job.get("instance_id") == instance_id
        ]
        job_ids = {job.get("id") for job in jobs if isinstance(job.get("id"), str)}
        assets_by_id = {
            asset.get("id"): asset
            for asset in self._rows("assets", owner)
            if asset.get("owner_id", owner) == owner and asset.get("id") is not None
        }
        artifacts = []
        seen_assets = set()
        for job in jobs:
            output_ids = job.get("output_asset_ids", [])
            if not isinstance(output_ids, list):
                continue
            for asset_id in output_ids:
                asset = assets_by_id.get(asset_id)
                marker = (job.get("id"), asset_id)
                if not isinstance(asset, Mapping) or marker in seen_assets:
                    continue
                seen_assets.add(marker)
                artifacts.append(self._safe_artifact(asset, job["id"]))
        logs = []
        for event in self._rows("events", owner):
            if event.get("owner_id", owner) != owner or event.get("job_id") not in job_ids:
                continue
            log = {"job_id": event["job_id"], "message": self._safe_log(event.get("message", ""))}
            for key in ("id", "created_at"):
                if event.get(key) is not None:
                    log[key] = event[key]
            logs.append(log)
        return artifacts, logs

    def describe(self, owner: str, instance: Mapping) -> dict:
        drafts = self._draft_values(owner, instance)
        workflows = []
        for row in self._workflows(owner):
            identity = f"{row.get('workflow_key')}@{row.get('workflow_version')}"
            manifest = row.get("manifest") if isinstance(row.get("manifest"), Mapping) else {}
            item = {
                "workflow_key": row.get("workflow_key"),
                "workflow_version": row.get("workflow_version"),
                "model_id": row.get("model_id"),
                "description": row.get("description"),
                "mock": bool(row.get("smoke_test_kind") == "mock" or row.get("model_id") == "demo-mock"),
                "manifest": {"parameters": self._safe_public_value(manifest.get("parameters", {}))},
                "parameters": self._public_parameters(manifest),
            }
            if identity in drafts:
                item["draft"] = {"values": copy.deepcopy(drafts[identity])}
            workflows.append(item)
        parameters = self._public_parameters(
            self._workflows(owner)[0].get("manifest", {}) if self._workflows(owner) else {}
        )
        values = drafts.get(
            f"{workflows[0]['workflow_key']}@{workflows[0]['workflow_version']}"
        ) if workflows else None
        artifacts, logs = self._describe_outputs(owner, instance)
        read_only = self._is_history(instance) or not self._is_verified(instance)
        capabilities = set(WORKBENCH_ACTIONS)
        if read_only:
            capabilities.difference_update(WRITE_ACTIONS)
        actions = sorted(capabilities)
        result = {
            "instance_id": instance.get("id"),
            "state": instance.get("state"),
            "read_only": read_only,
            "mock": self._is_mock(instance),
            "comfy": {
                "available": self._is_mock(instance),
                "verified": bool(self._is_verified(instance) and not self._is_mock(instance)),
                "simulated": self._is_mock(instance),
            },
            # ``actions`` is the historical name; ``capabilities`` is the UI contract.
            "actions": actions,
            "capabilities": actions,
            "allowed_workflows": workflows,
            "parameters": parameters,
            "drafts": drafts,
            "values": copy.deepcopy(values) if isinstance(values, Mapping) else {},
            "artifacts": artifacts,
            "logs": logs,
        }
        return result

    def _operation(self, owner, instance, action, payload, key):
        if action not in WORKBENCH_ACTIONS:
            raise WorkbenchError("WORKBENCH_ACTION_FORBIDDEN", "工作台动作不在固定 allowlist", 403)
        if action in WRITE_ACTIONS and not self._is_verified(instance):
            raise WorkbenchError("COMFY_UNVERIFIED", "实例尚未通过服务端运行时验证", 503, True)
        if action in WRITE_ACTIONS:
            if not isinstance(key, str) or not key or len(key) > 200:
                raise WorkbenchError("IDEMPOTENCY_REQUIRED", "写入工作台必须提供固定幂等键", 422)
            previous = self._ops.existing(owner, str(instance["id"]), action, key, payload)
            if previous is not None:
                return previous
        self._forbid_client_surface(payload)
        if self._is_history(instance):
            if action in WRITE_ACTIONS:
                raise WorkbenchError("INSTANCE_READ_ONLY", "实例已失效，工作台仅可读取历史", 409)
            if action == "health":
                return {"available": False, "verified": False, "read_only": True, "state": instance.get("state")}

        handler = getattr(self, f"_action_{action}")
        result = handler(owner, instance, payload, key) if action == "queue_reviewed_workflow" else handler(owner, instance, payload)
        if action in WRITE_ACTIONS:
            self._ops.put(owner, str(instance["id"]), action, key, payload, result)
        return result

    def execute(self, owner: str, instance: Mapping, action: str, payload: Mapping | None = None, key: str | None = None):
        payload = dict(payload or {})
        if not isinstance(action, str):
            raise WorkbenchError("VALIDATION_ERROR", "action必须是字符串")
        return self._operation(owner, instance, action, payload, key)

    def _action_health(self, owner, instance, payload):
        self._mock_or_unverified(instance)
        if self._is_mock(instance):
            return {
                "available": True,
                "verified": False,
                "mock": True,
                "simulated": True,
                "status": "simulated",
                "capabilities": sorted(WORKBENCH_ACTIONS),
            }
        return self.trusted_mcp.health(owner, instance)

    def _action_list_allowed_workflows(self, owner, instance, payload):
        self._mock_or_unverified(instance)
        return {"workflows": self.describe(owner, instance)["allowed_workflows"], "mock": self._is_mock(instance)}

    def _action_get_reviewed_graph(self, owner, instance, payload):
        # Graph reading is the one deliberately offline-safe workbench
        # operation.  It must not be blocked by COMFY_UNVERIFIED or instance
        # health; all mutating actions retain their existing gates.
        result = self.get_reviewed_graph(owner, payload)
        result["graph"] = copy.deepcopy(result["api_graph"])
        return result

    def _action_set_reviewed_params(self, owner, instance, payload):
        self._mock_or_unverified(instance)
        workflow = self._workflow(owner, payload)
        params = payload.get("params")
        if not isinstance(params, Mapping):
            raise WorkbenchError("VALIDATION_ERROR", "params必须是对象")
        try:
            resolved = resolve_parameters(workflow["manifest"], dict(params))
            bound = bind_parameters(workflow["manifest"], workflow["api_graph"], resolved)
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkbenchError("PARAMETERS_INVALID", str(exc)[:300]) from exc
        draft = self._write_draft(owner, instance, workflow, resolved, bound)
        return {"workflow_key": workflow["workflow_key"], "workflow_version": workflow["workflow_version"], **draft}

    def _action_queue_reviewed_workflow(self, owner, instance, payload, operation_key=None):
        self._mock_or_unverified(instance)
        if instance.get("state") in {"busy", "running"} or instance.get("current_job_id"):
            raise WorkbenchError("INSTANCE_BUSY", "实例正在执行任务，不能绕过实例锁", 409)
        workflow = self._workflow(owner, payload)
        job_input = payload.get("job_input")
        if job_input is None:
            job_input = {
                name: value
                for name, value in payload.items()
                if name not in {"workflow_id", "workflow_key", "workflow_version", "job_key", "idempotency_key"}
            }
        if not isinstance(job_input, Mapping):
            raise WorkbenchError("VALIDATION_ERROR", "queue的JobInput必须是对象")
        body = copy.deepcopy(dict(job_input))
        body.setdefault("project_id", instance.get("project_id"))
        body.setdefault("model_id", workflow.get("model_id"))
        body.setdefault("params", payload.get("params", {}))
        body.setdefault("asset_ids", [])
        body.setdefault("loras", [])
        body.setdefault("preset_id", None)
        policy = body.get("execution_policy") or {}
        if not isinstance(policy, Mapping):
            raise WorkbenchError("VALIDATION_ERROR", "JobInput的execution_policy必须是对象")
        body["workflow_key"] = workflow["workflow_key"]
        body["workflow_version"] = workflow["workflow_version"]
        body["execution_policy"] = {
            **dict(policy),
            "allocation": "existing",
            "instance_id": instance["id"],
            "account_id": instance.get("account_id"),
            "provider": "mock" if self._is_mock(instance) else dict(policy).get("provider", "colab"),
        }
        key = operation_key
        if not isinstance(key, str) or not key:
            raise WorkbenchError("IDEMPOTENCY_REQUIRED", "排队必须提供工作台幂等键")
        if self.admit_job is None:
            raise WorkbenchError("QUEUE_UNAVAILABLE", "实例队列服务未接入", 503, True)
        job = self.admit_job(owner, body, key, instance=instance)
        if not isinstance(job, Mapping):
            raise WorkbenchError("QUEUE_REJECTED", "任务排队响应无效", 503, True)
        return {"accepted": True, "mock": self._is_mock(instance), "job": copy.deepcopy(dict(job))}

    def _find_job(self, owner, instance, payload):
        job_id = payload.get("job_id") or instance.get("current_job_id")
        if not isinstance(job_id, str) or not job_id:
            raise WorkbenchError("JOB_NOT_FOUND", "未指定属于该实例的任务", 404)
        for job in self._rows("jobs", owner):
            if job.get("id") == job_id and job.get("instance_id") == instance.get("id"):
                return job
        raise WorkbenchError("JOB_NOT_FOUND", "任务不属于该实例", 404)

    def _action_job_status(self, owner, instance, payload):
        self._mock_or_unverified(instance)
        return {"job": copy.deepcopy(self._find_job(owner, instance, payload))}

    def _safe_log(self, value):
        text = str(value)
        for marker in ("token", "authorization", "password", "secret", "cookie"):
            if marker in text.lower():
                return "[REDACTED]"
        return re.sub(r"(?:https?|wss?)://[^\\s\\\"'<>]+", "[REDACTED_URL]", text[:4000])

    def _action_read_safe_log(self, owner, instance, payload):
        self._mock_or_unverified(instance)
        job_id = payload.get("job_id") or instance.get("current_job_id")
        rows = [event.get("message", "") for event in self._rows("events", owner) if event.get("job_id") == job_id]
        return {"job_id": job_id, "lines": [self._safe_log(item) for item in rows], "redacted": True}

    def _action_preview_artifacts(self, owner, instance, payload):
        self._mock_or_unverified(instance)
        job = self._find_job(owner, instance, payload)
        assets = []
        for asset_id in job.get("output_asset_ids", []) if isinstance(job, Mapping) else []:
            for row in self._rows("assets", owner):
                if row.get("id") == asset_id:
                    assets.append(self._safe_public_value(row))
        return {"job_id": job.get("id"), "artifacts": assets, "mock": self._is_mock(instance)}

    def _action_stop_current_job(self, owner, instance, payload):
        self._mock_or_unverified(instance)
        job = self._find_job(owner, instance, payload)
        if self._is_mock(instance):
            self.repo.update(
                "jobs",
                job["id"],
                {"cancel_requested": True, "requested_outcome": "cancelled"},
                owner,
            )
            return {
                "accepted": True,
                "state": "stop_requested",
                "job_id": job["id"],
                "mock": True,
                "simulated": True,
            }
        if self.trusted_mcp is None:
            raise WorkbenchError("COMFY_UNVERIFIED", "真实实例尚未接入受信 Comfy MCP 协议", 503, True)
        return self.trusted_mcp.stop_current_job(owner, instance, job.get("id"))


# Names used by integrations and tests; keep one implementation.
ControlledWorkbench = WorkbenchService
Workbench = WorkbenchService

__all__ = [
    "WORKBENCH_ACTIONS",
    "WRITE_ACTIONS",
    "WorkbenchError",
    "WorkbenchService",
    "ControlledWorkbench",
    "Workbench",
]
