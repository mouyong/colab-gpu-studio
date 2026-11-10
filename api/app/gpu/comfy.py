"""Controlled ComfyUI executor for a verified Colab/Jupyter runtime.

This module never executes caller supplied Python or shell.  It accepts only a
published workflow snapshot whose Comfy version, node allow-list, and weight
revision digests were reviewed by the registry.  The only bootstrap code is a
fixed marker-lock writer used to make an execution id restart-safe.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from urllib.parse import urlsplit, urlunsplit

import httpx

from .compute import ProviderUnavailable

log = logging.getLogger(__name__)


class ComfyError(ProviderUnavailable):
    pass


_EXECUTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ComfyClient:
    """Small, authenticated ComfyUI HTTP client; no arbitrary endpoint proxy."""

    def __init__(
        self, base_url: str, token: str, *, client: httpx.Client | None = None, timeout: float = 15.0
    ):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ComfyError("Comfy连接地址未验证")
        self.base_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
        self.token = token
        self.client = client or httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=False)
        self._owns_client = client is None

    def _request(
        self,
        method: str,
        path: str,
        *,
        params=None,
        json_body=None,
        content=None,
        headers=None,
        files=None,
        data=None,
    ) -> httpx.Response:
        if (
            path not in {"/prompt", "/system_stats", "/history", "/upload/image"}
            and not path.startswith("/history/")
            and not path.startswith("/view")
        ):
            raise ComfyError("Comfy endpoint未被允许")
        merged = {
            "Authorization": "Bearer " + self.token,
            "X-API-Key": self.token,
            "Accept": "application/json",
        }
        if headers:
            merged.update(headers)
        try:
            response = self.client.request(
                method,
                self.base_url + path,
                params=params,
                json=json_body,
                content=content,
                headers=merged,
                files=files,
                data=data,
            )
        except httpx.HTTPError as exc:
            raise ComfyError("Comfy连接失败") from exc
        if response.status_code >= 400:
            raise ComfyError(f"Comfy请求失败（HTTP {response.status_code}）")
        return response

    def system_stats(self) -> dict:
        value = self._request("GET", "/system_stats").json()
        return value if isinstance(value, dict) else {}

    def queue(self, graph: dict, execution_id: str) -> str:
        response = self._request(
            "POST",
            "/prompt",
            json_body={
                "prompt": graph,
                "client_id": execution_id,
                "extra_data": {"extra_pnginfo": {"atelier_execution_id": execution_id}},
            },
        )
        try:
            result = response.json()
            prompt_id = result.get("prompt_id")
            if not isinstance(prompt_id, str) or not prompt_id:
                raise ValueError
            return prompt_id
        except (ValueError, TypeError, AttributeError):
            raise ComfyError("Comfy队列响应无效")

    def history(self, prompt_id: str) -> dict:
        response = self._request("GET", "/history/" + prompt_id)
        value = response.json()
        return value if isinstance(value, dict) else {}

    def view(self, filename: str, subfolder: str = "", folder_type: str = "output") -> bytes:
        response = self._request(
            "GET", "/view", params={"filename": filename, "subfolder": subfolder, "type": folder_type}
        )
        return response.content

    def upload(self, content: bytes, filename: str, mime: str) -> dict:
        response = self._request(
            "POST",
            "/upload/image",
            files={"image": (filename, content, mime)},
            data={"overwrite": "true", "type": "input", "subfolder": ""},
        )
        try:
            value = response.json()
            return value if isinstance(value, dict) else {}
        except ValueError:
            return {}

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


_FIXED_BOOTSTRAP = r"""from pathlib import Path
import json
_execution_id = __ATELIER_EXECUTION_ID__
_root = Path("/content/ai-studio")
_root.mkdir(mode=0o700, parents=True, exist_ok=True)
_marker = _root / (_execution_id + ".json")
_lock = _root / (_execution_id + ".lock")
if _marker.exists():
    _result = {"existing": True, "execution_id": _execution_id}
elif _lock.exists():
    raise RuntimeError("Atelier execution lock is held")
else:
    _lock.open("x").close()
    _marker.write_text(json.dumps({"execution_id": _execution_id, "state": "reserved"}, sort_keys=True), encoding="utf-8")
    _result = {"existing": False, "execution_id": _execution_id}
_result
"""


def _bootstrap_script(execution_id: str) -> str:
    if not _EXECUTION_ID.fullmatch(execution_id):
        raise ComfyError("execution_id无效")
    return _FIXED_BOOTSTRAP.replace("__ATELIER_EXECUTION_ID__", repr(execution_id))


class _JupyterBootstrap:
    """Run only the fixed marker script over Jupyter REST + websocket."""

    def __init__(self, base_url: str, token: str, *, client: httpx.Client, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.client = client
        self.timeout = timeout

    def run(self, execution_id: str) -> None:
        script = _bootstrap_script(execution_id)
        headers = {"Authorization": "token " + self.token, "Accept": "application/json"}
        session_id = None
        try:
            response = self.client.post(
                self.base_url + "/api/sessions",
                headers=headers,
                json={
                    "path": "ai-studio-marker.py",
                    "name": "ai-studio-marker.py",
                    "type": "console",
                    "kernel": {"name": "python3"},
                },
            )
            if response.status_code < 200 or response.status_code >= 300:
                raise ComfyError("Jupyter marker session创建失败")
            session = response.json()
            session_id = session["id"]
            kernel_id = session["kernel"]["id"]
            parsed = urlsplit(self.base_url)
            scheme = "wss" if parsed.scheme == "https" else "ws"
            ws_url = urlunsplit(
                (scheme, parsed.netloc, f"/api/kernels/{kernel_id}/channels", "session_id=" + session_id, "")
            )
            from websockets.sync.client import connect

            with connect(
                ws_url, additional_headers=headers, open_timeout=self.timeout, close_timeout=self.timeout
            ) as websocket:
                msg_id = uuid.uuid4().hex
                websocket.send(
                    json.dumps(
                        {
                            "header": {
                                "msg_id": msg_id,
                                "msg_type": "execute_request",
                                "session": session_id,
                                "username": "atelier",
                                "version": "5.3",
                            },
                            "parent_header": {},
                            "metadata": {},
                            "content": {
                                "code": script,
                                "silent": False,
                                "store_history": False,
                                "allow_stdin": False,
                                "stop_on_error": True,
                            },
                        }
                    )
                )
                for _ in range(40):
                    message = json.loads(websocket.recv(timeout=self.timeout))
                    if message.get("parent_header", {}).get("msg_id") != msg_id:
                        continue
                    if message.get("msg_type") == "error":
                        raise ComfyError("固定Jupyter bootstrap执行失败")
                    if message.get("msg_type") == "execute_reply":
                        if message.get("content", {}).get("status") != "ok":
                            raise ComfyError("固定Jupyter bootstrap执行失败")
                        return
            raise ComfyError("固定Jupyter bootstrap未确认")
        except ComfyError:
            raise
        except Exception as exc:
            raise ComfyError("Jupyter连接协议未证实，已拒绝执行") from exc
        finally:
            if session_id:
                try:
                    self.client.delete(self.base_url + "/api/sessions/" + session_id, headers=headers)
                except (httpx.HTTPError, OSError):
                    log.debug("unable to clean up Jupyter marker session", exc_info=True)


class ComfyGPUExecutor:
    is_mock = False

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        client: httpx.Client | None = None,
        expected_comfy_version: str | None = None,
    ):
        self.base_url = base_url
        self.token = token
        self.client = client or httpx.Client(timeout=httpx.Timeout(15.0), follow_redirects=False)
        self._owns_client = client is None
        self.expected_comfy_version = expected_comfy_version
        self._executions: dict[str, dict] = {}

    @classmethod
    def from_environment(cls, **kwargs):
        import os

        return cls(base_url=os.getenv("COMFY_API_URL"), token=os.getenv("COMFY_API_TOKEN"), **kwargs)

    @staticmethod
    def _manifest(job: dict) -> tuple[dict, dict]:
        snapshot = job.get("snapshot") or {}
        manifest = snapshot.get("manifest") or {}
        graph = snapshot.get("bound_graph") or snapshot.get("api_graph")
        if not isinstance(manifest, dict) or not isinstance(graph, dict):
            raise ComfyError("工作流快照缺少已审查Comfy图")
        if manifest.get("state") != "published" or manifest.get("reviewed") is not True:
            raise ComfyError("工作流尚未通过发布审查")
        if not isinstance(manifest.get("comfy_version"), str) or not manifest.get("comfy_version"):
            raise ComfyError("工作流缺少固定Comfy版本")
        allowlist = manifest.get("node_allowlist")
        if not isinstance(allowlist, list) or not allowlist or any(not isinstance(x, str) for x in allowlist):
            raise ComfyError("工作流缺少节点allowlist")
        weights = manifest.get("weights")
        if not isinstance(weights, list) or any(
            not isinstance(w, dict)
            or not w.get("revision")
            or not re.fullmatch(r"[0-9a-fA-F]{64}", str(w.get("sha256", "")))
            for w in weights
        ):
            raise ComfyError("工作流权重revision或sha256未锁定")
        for node in graph.values():
            if not isinstance(node, dict) or node.get("class_type") not in allowlist:
                raise ComfyError("工作流包含未允许的Comfy节点")
        return manifest, graph

    def _connection(self, handle: dict) -> tuple[str, str, str | None]:
        if not isinstance(handle, dict):
            raise ComfyError("Colab连接句柄无效")
        url = handle.get("comfy_url") or handle.get("connection_url") or self.base_url
        token = handle.get("connection_token") or handle.get("token") or self.token
        jupyter_url = handle.get("jupyter_url")
        if not isinstance(url, str) or not isinstance(token, str) or not token:
            raise ComfyError("未取得经过验证的Colab connectionInfo，已拒绝执行")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ComfyError("Colab连接协议未验证，已拒绝执行")
        return url, token, jupyter_url

    def start(self, handle: dict, job: dict, execution_id: str) -> dict:
        if not _EXECUTION_ID.fullmatch(execution_id):
            raise ComfyError("execution_id无效")
        if execution_id in self._executions:
            return {"execution_id": execution_id, "idempotent": True}
        url, token, jupyter_url = self._connection(handle)
        manifest, graph = self._manifest(job)
        if self.expected_comfy_version and manifest["comfy_version"] != self.expected_comfy_version:
            raise ComfyError("Comfy版本与发布审查不匹配")
        comfy = ComfyClient(url, token, client=self.client)
        # The marker is mandatory. Without a verified Jupyter connection there
        # is no safe way to create the required /content/ai-studio lock file.
        if not isinstance(jupyter_url, str) or not jupyter_url:
            comfy.close()
            raise ComfyError("未提供Jupyter connectionInfo，无法建立执行锁")
        try:
            stats = comfy.system_stats()
            system = stats.get("system") if isinstance(stats.get("system"), dict) else stats
            runtime_version = system.get("comfyui_version") or system.get("comfy_version")
            if runtime_version != manifest["comfy_version"]:
                comfy.close()
                raise ComfyError("运行时Comfy版本未通过固定版本校验")
            _JupyterBootstrap(jupyter_url, token, client=self.client).run(execution_id)
            prompt_id = comfy.queue(graph, execution_id)
        except ComfyError:
            comfy.close()
            raise
        self._executions[execution_id] = {
            "prompt_id": prompt_id,
            "client": comfy,
            "manifest": manifest,
            "started": True,
        }
        return {"execution_id": execution_id, "prompt_id": prompt_id}

    def poll(self, handle: dict, job: dict) -> dict:
        execution_id = job.get("execution_id")
        state = self._executions.get(execution_id)
        if not state:
            raise ComfyError("执行状态不可恢复，拒绝重复推理")
        history = state["client"].history(state["prompt_id"])
        item = history.get(state["prompt_id"]) if isinstance(history, dict) else None
        if not isinstance(item, dict):
            return {"state": "running", "outputs": []}
        status = item.get("status") or {}
        if (
            status.get("status_str") == "error"
            or status.get("completed") is False
            and status.get("status_str") == "failed"
        ):
            return {
                "state": "failed",
                "outputs": [],
                "error_code": "comfy_execution_failed",
                "message": "Comfy工作流执行失败",
            }
        outputs = []
        for node in (item.get("outputs") or {}).values():
            if not isinstance(node, dict):
                continue
            for category, mime in (("images", "image/png"), ("gifs", "image/gif"), ("videos", "video/mp4")):
                for entry in node.get(category, []) or []:
                    if not isinstance(entry, dict) or not isinstance(entry.get("filename"), str):
                        continue
                    content = state["client"].view(
                        entry["filename"], str(entry.get("subfolder", "")), str(entry.get("type", "output"))
                    )
                    outputs.append(
                        {
                            "filename": entry["filename"],
                            "content": content,
                            "mime": mime,
                            "metadata": {
                                "execution_id": execution_id,
                                "comfy_version": state["manifest"]["comfy_version"],
                            },
                        }
                    )
        return {"state": "completed", "outputs": outputs}

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


__all__ = ["ComfyClient", "ComfyError", "ComfyGPUExecutor", "ProviderUnavailable"]
