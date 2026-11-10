"""Recoverable executor for the official Google Colab MCP endpoint.

The adapter has no in-memory execution registry.  Colab's execution directory
(status.json, meta.json and output files) is the source of truth, so a process
restart or a lost JSON-RPC response cannot cause a second worker launch.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from app.security import decrypt_secret

from .google import GoogleService
from .runtime_worker import MAX_OUTPUT_BYTES

log = logging.getLogger("studio.mcp")
MCP_ENDPOINT = "https://colaboratory.googleapis.com/mcp"
_RUNTIME = re.compile(r"^runtimes/[a-zA-Z0-9][a-zA-Z0-9._~-]{0,127}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


_NETWORK_ATTEMPTS = 3


class MCPError(RuntimeError):
    """Safe Colab MCP adapter failure; upstream bodies are never exposed."""


class MCPTransient(MCPError):
    """Colab answered "unavailable"; every call made here is idempotent, so it is retried."""


_TRANSIENT = re.compile(r"unavailable|\b503\b|deadline|timed? ?out|temporar", re.I)
_SECRET = re.compile(r"(ya29\.[\w.-]+|Bearer\s+\S+|[A-Za-z0-9_-]{40,})")


def _error_detail(result: dict) -> str:
    """Secret-redacted, truncated reason from a failed tool result; goes to the backend log only."""
    parts = [result.get("executionError")]
    parts += [c.get("text") for c in result.get("content", []) if isinstance(c, dict)]
    text = " ".join(p if isinstance(p, str) else json.dumps(p) for p in parts if p)
    return _SECRET.sub("***", text).strip()[:300]


def _uuid4(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise MCPError("execution_id必须是UUID4") from exc
    if parsed.version != 4:
        raise MCPError("execution_id必须是UUID4")
    return str(parsed)


def _b64_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return base64.b64encode(raw).decode("ascii")


def build_bootstrap_code(execution_id: str, payload: dict, worker_source: str | None = None) -> str:
    """Return the one trusted code string accepted by ``execute_code``.

    Caller data is transported as base64 JSON, never interpolated as Python.
    The worker source is the checked-in runtime_worker.py, not user supplied
    code.  The O_EXCL lock and marker make repeated starts harmless.
    """
    execution_id = _uuid4(execution_id)
    if not isinstance(payload, dict):
        raise MCPError("worker payload必须是对象")
    if worker_source is None:
        worker_source = Path(__file__).with_name("runtime_worker.py").read_text(encoding="utf-8")
    if not isinstance(worker_source, str) or not worker_source:
        raise MCPError("trusted worker source is unavailable")
    source_b64 = base64.b64encode(worker_source.encode("utf-8")).decode("ascii")
    payload_b64 = _b64_json(payload)
    execution_literal = repr(execution_id)
    return f"""# Colab GPU Studio fixed trusted bootstrap; no caller Python is executed.
import base64 as _base64
import json as _json
import os as _os
from pathlib import Path as _Path
import subprocess as _subprocess
import sys as _sys

_execution_id = {execution_literal}
_root = _Path("/content/ai-studio")
_dir = _root / _execution_id
_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
_lock = _dir / ".worker.lock"
_marker = _dir / "marker.json"
_status = _dir / "status.json"
_worker = _dir / "runtime_worker.py"
_payload = _dir / "payload.json"
_log = _dir / "worker.log"
_result = {{"execution_id": _execution_id, "directory": str(_dir), "started": False, "existing": False}}
if _marker.exists():
    _result["existing"] = True
elif _status.exists() or _lock.exists():
    _result["existing"] = True
else:
    try:
        # mode=x maps to an O_EXCL|O_CREAT exclusive lock.
        _lock.open("x").close()
    except FileExistsError:
        _result["existing"] = True
    else:
        _worker.write_bytes(_base64.b64decode({source_b64!r}))
        _payload.write_bytes(_base64.b64decode({payload_b64!r}))
        _marker.write_text(_json.dumps({{"execution_id": _execution_id, "state": "reserved"}}, sort_keys=True), encoding="utf-8")
        _log_handle = _log.open("ab")
        _proc = _subprocess.Popen([_sys.executable, str(_worker), str(_payload)], cwd=str(_dir),
                                  stdin=_subprocess.DEVNULL, stdout=_log_handle, stderr=_subprocess.STDOUT,
                                  shell=False, start_new_session=True)
        _log_handle.close()
        _marker.write_text(_json.dumps({{"execution_id": _execution_id, "state": "launched", "pid": _proc.pid}}, sort_keys=True), encoding="utf-8")
        _result.update({{"started": True, "pid": _proc.pid}})
print(_json.dumps(_result, separators=(",", ":")))
"""


def build_poll_code(execution_id: str) -> str:
    """Return a read-only status reader with a hard 100MB output bound."""
    execution_id = _uuid4(execution_id)
    encoded = base64.b64encode(execution_id.encode()).decode("ascii")
    return f"""# Colab GPU Studio fixed read-only recovery probe.
import base64 as _base64
import json as _json
from pathlib import Path as _Path

_execution_id = _base64.b64decode({encoded!r}).decode("ascii")
_dir = _Path("/content/ai-studio") / _execution_id
_status_path = _dir / "status.json"
_meta_path = _dir / "meta.json"
_limit = 100 * 1024 * 1024
_result = {{"execution_id": _execution_id, "state": "running", "outputs": []}}
if not _status_path.exists():
    _result["marker"] = (_dir / "marker.json").exists()
else:
    _status = _json.loads(_status_path.read_text(encoding="utf-8"))
    if isinstance(_status, dict):
        _result.update({{key: _status.get(key) for key in ("state", "error_code", "error_message", "stage", "timings", "stage_started", "gpu_diagnostics") if key in _status}})
    if _result.get("state") == "completed" and _meta_path.exists():
        _meta = _json.loads(_meta_path.read_text(encoding="utf-8"))
        _artifacts = _meta.get("artifacts") if isinstance(_meta, dict) else None
        if isinstance(_artifacts, list):
            if len(_artifacts) > 100 or len(_json.dumps(_artifacts)) > 1048576:
                raise ValueError("artifact metadata exceeds limit")
            _result["artifacts"] = _artifacts
        _total = 0
        for _item in (_meta.get("outputs", []) if isinstance(_meta, dict) and _artifacts is None else []):
            if not isinstance(_item, dict):
                continue
            _relative = _item.get("path")
            if not isinstance(_relative, str):
                continue
            _file = (_dir / _relative).resolve()
            if _dir.resolve() not in _file.parents or not _file.is_file():
                continue
            _size = _file.stat().st_size
            if _size > _limit or _total + _size > _limit:
                _result = {{"execution_id": _execution_id, "state": "failed", "error_code": "gpu_output_too_large", "error_message": "output exceeds 100MB"}}
                break
            _content = _file.read_bytes()
            _total += len(_content)
            _result.setdefault("outputs", []).append({{**_item, "content": _base64.b64encode(_content).decode("ascii"), "content_encoding": "base64"}})
# A terminal status wins. For older markers without PID retain timeout recovery.
if _result.get('state') in ('running', 'starting', 'reserved'):
    _marker_path = _dir / 'marker.json'
    if _marker_path.is_file():
        _marker_data = _json.loads(_marker_path.read_text())
        _pid = _marker_data.get('pid')
        if type(_pid) is int and _pid > 0:
            import os as _os
            try:
                _os.kill(_pid, 0)
                _proc_stat = _Path('/proc') / str(_pid) / 'stat'
                _dead = _proc_stat.is_file() and _proc_stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z'
            except ProcessLookupError:
                _dead = True
            except PermissionError:
                _dead = False
            if _dead:
                _result.update({{'state':'failed', 'error_code':'gpu_worker_exited',
                    'error_message':'Worker process exited before completion'}})
print(_json.dumps(_result, separators=(",", ":")))
"""


def build_chunk_code(execution_id: str, index: int, offset: int) -> str:
    execution_id = _uuid4(execution_id)
    if type(index) is not int or not 0 <= index < 100 or type(offset) is not int or not 0 <= offset <= 2*1024**3:
        raise MCPError('invalid chunk request')
    return f"""# Trusted bounded artifact reader; no caller paths.
import base64 as _b
import json as _j
from pathlib import Path as _P
_e = {execution_id!r}
_root = _P('/content/ai-studio') / _e
_meta = _j.loads((_root / 'meta.json').read_text())
if _meta.get('execution_id') != _e:
    raise ValueError('execution mismatch')
_item = _meta['artifacts'][{index}]
_path = (_root / _item['path']).resolve()
if _root.resolve() not in _path.parents or not _path.is_file():
    raise ValueError('artifact path escaped')
with _path.open('rb') as _f:
    _f.seek({offset})
    _chunk = _f.read(1024*1024)
print(_j.dumps({{'execution_id': _e, 'offset': {offset}, 'content': _b.b64encode(_chunk).decode()}}))
"""


def _walk(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


class MCPGPUExecutor:
    is_mock = False

    def __init__(
        self,
        store,
        owner_id: str,
        *,
        account_id: str | None = None,
        google: GoogleService | None = None,
        client: httpx.Client | None = None,
        endpoint: str = MCP_ENDPOINT,
        timeout: float = 30.0,
        artifact_store=None,
    ):
        if store is None or not owner_id:
            raise MCPError("MCPGPUExecutor需要Repository和owner_id")
        self.artifact_store = artifact_store
        self.store = store
        self.owner_id, self.account_id = owner_id, account_id
        self.client = client or httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=False)
        self._owns_client = client is None
        self.google = google or GoogleService(store, owner_id, account_id=account_id, client=self.client)
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        if self.endpoint != MCP_ENDPOINT:
            # Tests may inject a MockTransport endpoint, but production code must
            # not turn this adapter into an arbitrary outbound proxy.
            parsed = httpx.URL(self.endpoint)
            if parsed.scheme != "https" or not parsed.host:
                raise MCPError("Colab MCP endpoint未验证")

    @classmethod
    def from_environment(cls, store=None, owner_id=None, **kwargs):
        return cls(store, owner_id, **kwargs)

    def _rpc(self, method: str, params: dict) -> Any:
        token = self.google.access_token("colab")
        request_id = str(uuid.uuid4())
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/json,text/event-stream",
            "Content-Type": "application/json",
        }
        # Every call made here is safe to repeat (create_session has a fixed session id,
        # execute_code has an execution id, status probes only read), so a dropped or
        # timed-out connection is retried a few times instead of killing a running job.
        for attempt in range(_NETWORK_ATTEMPTS):
            try:
                response = self.client.post(self.endpoint, json=body, headers=headers)
                break
            except httpx.TransportError as exc:
                if attempt == _NETWORK_ATTEMPTS - 1:
                    raise MCPTransient(f"Colab MCP网络请求失败（{type(exc).__name__}，已重试{attempt}次）") from exc
                time.sleep(2 * (attempt + 1))
            except httpx.HTTPError as exc:
                raise MCPError(f"Colab MCP网络请求失败（{type(exc).__name__}）") from exc
        if response.status_code in {408, 429, 500, 502, 503, 504}:
            raise MCPTransient(f'Colab MCP temporarily unavailable (HTTP {response.status_code})')
        if response.status_code < 200 or response.status_code >= 300:
            raise MCPError(f"Colab MCP请求失败（HTTP {response.status_code}）")
        envelope = self._parse_response(response)
        if not isinstance(envelope, dict):
            raise MCPError("Colab MCP响应格式无效")
        if envelope.get("error"):
            error = envelope['error']
            message = error.get('message', '') if isinstance(error, dict) else ''
            code = error.get('code') if isinstance(error, dict) else None
            # Protocol/authorization failures are permanent, even if text happens
            # to contain a transient phrase. Never echo upstream messages.
            if code not in (-32700, -32600, -32601, -32602, 401, 403) and isinstance(message, str):
                permanent = re.search(r'permission|unauthori|forbidden|invalid|denied', message, re.I)
                if not permanent and _TRANSIENT.search(message):
                    raise MCPTransient('Colab MCP temporary RPC failure')
            raise MCPError("Colab MCP工具调用失败")
        return envelope.get("result")

    @staticmethod
    def _parse_response(response: httpx.Response) -> dict:
        content_type = response.headers.get("content-type", "").lower()
        if "text/event-stream" not in content_type:
            try:
                value = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise MCPError("Colab MCP JSON响应无效") from exc
            if isinstance(value, dict):
                return value
            raise MCPError("Colab MCP JSON响应不是对象")
        found = None
        for line in response.text.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                candidate = json.loads(data)
            except json.JSONDecodeError as exc:
                raise MCPError("Colab MCP SSE响应无效") from exc
            if isinstance(candidate, dict) and ("result" in candidate or "error" in candidate):
                found = candidate
        if found is None:
            raise MCPError("Colab MCP SSE响应为空")
        return found

    @staticmethod
    def _tool_value(result: Any) -> Any:
        if not isinstance(result, dict):
            raise MCPError("Colab MCP工具返回无效结构")
        if result.get("isError") or result.get("executionError"):
            detail = _error_detail(result)
            log.warning("Colab executor error: %s", detail)  # restricted backend log only
            name = (result.get("executionError") or {}).get("name") if isinstance(result.get("executionError"), dict) else None
            error = MCPTransient if _TRANSIENT.search(detail) else MCPError
            raise error(f"Colab固定执行程序报错（{name or '工具错误'}），详见后台日志")
        if result.get("outputTruncated"):
            raise MCPError("Colab文本传输被截断，结果未保存；请使用受控文件传输或降低产物大小")
        if isinstance(result.get("structuredContent"), dict):
            return MCPGPUExecutor._tool_value(result["structuredContent"])
        if "stdout" in result:
            text = result.get("stdout", "")
            if not isinstance(text, str):
                raise MCPError("Colab stdout结构无效")
            for line in reversed(text.splitlines()):
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        return value
                except json.JSONDecodeError:
                    continue
            raise MCPError("固定执行程序没有返回完整JSON状态")
        if "content" in result:
            for item in result.get("content", []):
                if (
                    isinstance(item, dict)
                    and item.get("type") == "text"
                    and isinstance(item.get("text"), str)
                ):
                    try:
                        return MCPGPUExecutor._tool_value(json.loads(item["text"]))
                    except json.JSONDecodeError:
                        continue
            raise MCPError("MCP工具没有返回可校验的JSON内容")
        return result

    def _tool(self, name: str, arguments: dict) -> Any:
        for attempt in range(_NETWORK_ATTEMPTS):
            try:
                return self._tool_value(self._rpc("tools/call", {"name": name, "arguments": arguments}))
            except MCPTransient:
                if attempt == _NETWORK_ATTEMPTS - 1:
                    raise
                time.sleep(3 * (attempt + 1))

    def _tunnel_lease(self, handle: dict) -> dict | None:
        if not isinstance(handle, dict) or handle.get("owner_id") != self.owner_id:
            return None
        runtime_id = handle.get("runtime_id")
        if not runtime_id and isinstance(handle.get("runtime_name"), str) and handle["runtime_name"].startswith("runtimes/"):
            runtime_id = handle["runtime_name"].split("/", 1)[1]
        if not runtime_id or not hasattr(self.store, "list"):
            return None
        req_id = handle.get("request_id")
        job_id = handle.get("owner_job_id") or handle.get("job_id")
        for lease in self.store.list("leases", self.owner_id):
            if lease.get("runtime_id") != runtime_id:
                continue
            if req_id and lease.get("request_id") != req_id:
                continue
            if job_id and lease.get("owner_job_id") != job_id:
                continue
            if lease.get("tunnel_endpoint"):
                return lease
        return None

    @staticmethod
    def _ws_pack_default(msg: dict) -> bytes:
        body = json.dumps(msg, default=str).encode("utf-8")
        # Jupyter's default protocol needs at least two buffers (json + zero-length
        # padding), otherwise strict servers reject the frame before reading it.
        buffers = [body, b""]
        nbufs = len(buffers)
        offsets = [4 * (nbufs + 1)]
        for index in range(nbufs - 1):
            offsets.append(offsets[-1] + len(buffers[index]))
        frame = bytearray(offsets[-1] + len(buffers[-1]))
        frame[0:4] = nbufs.to_bytes(4, "big")
        for index, offset in enumerate(offsets):
            start = 4 * (index + 1)
            frame[start : start + 4] = offset.to_bytes(4, "big")
        for index, buffer in enumerate(buffers):
            start = offsets[index]
            frame[start : start + len(buffer)] = buffer
        return bytes(frame)

    @staticmethod
    def _ws_unpack_default(raw: str | bytes) -> dict:
        if isinstance(raw, str):
            return json.loads(raw)
        nbufs = int.from_bytes(raw[:4], "big")
        if nbufs < 1:
            raise ValueError("invalid jupyter ws frame")
        offsets = [int.from_bytes(raw[4 * (i + 1) : 4 * (i + 2)], "big") for i in range(nbufs)]
        json_stop = offsets[1] if nbufs > 1 else len(raw)
        return json.loads(raw[offsets[0] : json_stop].decode("utf-8"))

    def _refresh_tunnel_proxy(self, lease: dict) -> tuple[str, str]:
        url = lease.get("proxy_url")
        enc_tok = lease.get("encrypted_proxy_token")
        exp = float(lease.get("proxy_token_expires_at") or 0)
        if url and enc_tok and (exp == 0 or exp > time.time() + 120):
            return str(url).rstrip("/"), decrypt_secret(enc_tok)
        from .colab import GoogleColabProvider

        provider = GoogleColabProvider(
            self.store,
            self.owner_id,
            account_id=self.account_id or lease.get("account_id"),
            client=self.client,
            google=self.google,
        )
        matched = provider._sync_tunnel_lease(lease)
        if matched is None:
            raise MCPError("Colab 隧道运行时已释放或不存在")
        fresh = self.store.get("leases", lease["id"], self.owner_id) or lease
        url = fresh.get("proxy_url")
        enc_tok = fresh.get("encrypted_proxy_token")
        if not url or not enc_tok:
            raise MCPError("Colab 隧道代理凭据缺失")
        return str(url).rstrip("/"), decrypt_secret(enc_tok)

    def _execute_tunnel_code(self, lease: dict, code: str) -> dict:
        from websockets.sync.client import connect as ws_connect

        proxy_url, proxy_token = self._refresh_tunnel_proxy(lease)
        auth_params = {"colab-runtime-proxy-token": proxy_token, "authuser": "0"}
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Colab-Client-Agent": "colab-cli",
            "X-Colab-Runtime-Proxy-Token": proxy_token,
        }
        kernel_id = lease.get("kernel_id")
        try:
            if kernel_id:
                r = self.client.get(f"{proxy_url}/api/kernels/{kernel_id}", headers=headers, params=auth_params)
                if r.status_code == 404:
                    kernel_id = None
                elif r.status_code >= 400:
                    raise MCPTransient(f"Colab kernel inspect HTTP {r.status_code}")
            if not kernel_id:
                rk = self.client.get(f"{proxy_url}/api/kernels", headers=headers, params=auth_params)
                existing = rk.json() if rk.status_code == 200 else None
                if isinstance(existing, list) and existing:
                    kernel_id = existing[0].get("id")
                else:
                    rc = self.client.post(
                        f"{proxy_url}/api/kernels", json={"name": "python3"}, headers=headers, params=auth_params
                    )
                    if rc.status_code >= 400:
                        raise MCPTransient(f"Colab kernel start HTTP {rc.status_code}")
                    kernel_id = rc.json().get("id")
                if not isinstance(kernel_id, str) or not kernel_id:
                    raise MCPError("Colab 隧道未能创建 Jupyter Kernel")
                self.store.update("leases", lease["id"], {"kernel_id": kernel_id}, self.owner_id)
        except httpx.HTTPError as exc:
            raise MCPTransient(f"Colab kernel HTTP error ({type(exc).__name__})") from exc

        ws_base = re.sub(r"^http", "ws", proxy_url, count=1)
        session_id = uuid.uuid4().hex
        msg_id = uuid.uuid4().hex
        query = urlencode({**auth_params, "session_id": session_id})
        ws_url = f"{ws_base}/api/kernels/{kernel_id}/channels?{query}"
        req_msg = {
            "header": {
                "msg_id": msg_id,
                "username": "studio",
                "session": session_id,
                "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "msg_type": "execute_request",
                "version": "5.3",
            },
            "parent_header": {},
            "metadata": {},
            "content": {
                "code": code,
                "silent": False,
                "store_history": False,
                "user_expressions": {},
                "allow_stdin": False,
                "stop_on_error": True,
            },
            "channel": "shell",
        }
        stdout_parts: list[str] = []
        error_info: dict | None = None
        reply_seen = False
        idle_seen = False
        deadline = time.monotonic() + self.timeout
        try:
            with ws_connect(
                ws_url,
                additional_headers={
                    "X-Colab-Client-Agent": "colab-cli",
                    "X-Colab-Runtime-Proxy-Token": proxy_token,
                },
                open_timeout=min(15.0, self.timeout),
                close_timeout=5.0,
                max_size=32 * 1024 * 1024,
            ) as ws:
                ws.send(self._ws_pack_default(req_msg))
                while time.monotonic() < deadline:
                    remaining = max(0.5, deadline - time.monotonic())
                    raw = ws.recv(timeout=remaining)
                    msg = self._ws_unpack_default(raw)
                    if (msg.get("parent_header") or {}).get("msg_id") != msg_id:
                        continue
                    mtype = (msg.get("header") or {}).get("msg_type") or msg.get("msg_type")
                    content = msg.get("content") or {}
                    if mtype == "stream":
                        if content.get("name") == "stdout":
                            stdout_parts.append(str(content.get("text") or ""))
                    elif mtype in ("execute_result", "display_data"):
                        plain = (content.get("data") or {}).get("text/plain")
                        if isinstance(plain, str):
                            stdout_parts.append(plain + "\n")
                    elif mtype == "error":
                        error_info = {
                            "name": content.get("ename") or "ExecutionError",
                            "value": content.get("evalue") or "",
                        }
                    elif mtype == "execute_reply":
                        reply_seen = True
                        if content.get("status") == "error" and error_info is None:
                            error_info = {
                                "name": content.get("ename") or "ExecutionError",
                                "value": content.get("evalue") or "",
                            }
                    elif mtype == "status" and content.get("execution_state") == "idle":
                        idle_seen = True
                    if reply_seen and idle_seen:
                        break
        except TimeoutError as exc:
            raise MCPTransient("Colab 隧道 Kernel 执行超时") from exc
        except Exception as exc:
            if isinstance(exc, (MCPError, MCPTransient)):
                raise
            raise MCPTransient(f"Colab 隧道 WebSocket 异常（{type(exc).__name__}）") from exc
        if error_info is not None:
            return {"isError": True, "executionError": error_info, "stdout": "".join(stdout_parts)}
        return {"stdout": "".join(stdout_parts)}

    def _session(self, handle: dict, job_id: str) -> str:
        if not isinstance(handle, dict) or handle.get("owner_id") != self.owner_id:
            raise MCPError("拒绝使用不属于当前owner的Colab租约")
        parent = handle.get("runtime_name")
        if not isinstance(parent, str) or not _RUNTIME.fullmatch(parent):
            raise MCPError("Colab runtime句柄未验证")
        if not isinstance(job_id, str) or not _SAFE_ID.fullmatch(job_id):
            raise MCPError("job id不适合作为稳定MCP sessionId")
        session_id = "studio-" + hashlib.sha256(job_id.encode()).hexdigest()[:32]
        if self._tunnel_lease(handle) is not None:
            return f"{parent}/sessions/{session_id}"
        result = self._tool("create_session", {"parent": parent, "sessionId": session_id})
        session = next(
            (
                value.get("name") or value.get("session")
                for value in _walk(result)
                if isinstance(value, dict)
                and isinstance(value.get("name") or value.get("session"), str)
                and (value.get("name") or value.get("session")).startswith(parent + "/sessions/")
            ),
            None,
        )
        if not session or not re.fullmatch(re.escape(parent) + r"/sessions/[a-z][a-z0-9-]{0,62}", session):
            raise MCPError("Colab MCP create_session未返回合法完整session资源名")
        return session

    @staticmethod
    def _payload(job: dict, execution_id: str, root: str) -> dict:
        snapshot = job.get("snapshot")
        if not isinstance(snapshot, dict):
            raise MCPError("job缺少不可变snapshot")
        # Explicit allow-list prevents credentials or arbitrary caller fields from
        # crossing into the remote runtime.
        payload = {
            "execution_id": execution_id,
            "owner_id": job.get("owner_id"),
            "output_transport": "mcp-chunks-v1",
            "job_id": job.get("id"),
            "execution_dir": root,
            "max_runtime_minutes": job.get("max_runtime_minutes", 10),
            "download_timeout_seconds": min(float(job.get("download_timeout_seconds", 600)), 600),
            "system_smoke": snapshot.get("system_smoke") is True,
            "snapshot": snapshot,
        }
        cache = job.get("drive_cache")
        if isinstance(cache, dict):
            payload["drive_cache"] = {
                "folder_id": cache.get("folder_id"),
                "runtime_folder_id": cache.get("runtime_folder_id"),
                "access_token": cache.get("access_token"),
            }
        return payload

    def _exec_code_on_handle(self, handle: dict, session: str, execution_id: str, code: str) -> Any:
        tunnel_lease = self._tunnel_lease(handle)
        if tunnel_lease is not None:
            for attempt in range(_NETWORK_ATTEMPTS):
                try:
                    return self._tool_value(self._execute_tunnel_code(tunnel_lease, code))
                except MCPTransient:
                    if attempt == _NETWORK_ATTEMPTS - 1:
                        raise
                    time.sleep(2 * (attempt + 1))
        return self._tool("execute_code", {"session": session, "executionId": execution_id, "code": code})

    def start(self, handle: dict, job: dict, execution_id: str) -> dict:
        execution_id = _uuid4(execution_id)
        if not isinstance(job, dict) or not isinstance(job.get("id"), str):
            raise MCPError("job无效")
        session = self._session(handle, job["id"])
        root = "/content/ai-studio/" + execution_id
        code = build_bootstrap_code(execution_id, self._payload(job, execution_id, root))
        result = self._exec_code_on_handle(handle, session, execution_id, code)
        return {
            "execution_id": execution_id,
            "session": session,
            "idempotent": bool(isinstance(result, dict) and result.get("existing")),
            "remote": result if isinstance(result, dict) else {},
        }

    def poll(self, handle: dict, job: dict) -> dict:
        if not isinstance(job, dict) or not isinstance(job.get("id"), str):
            raise MCPError("job无效")
        execution_id = _uuid4(job.get("execution_id"))
        session = self._session(handle, job["id"])
        probe_id = str(uuid.uuid4())
        code = build_poll_code(execution_id)
        result = self._exec_code_on_handle(handle, session, probe_id, code)
        if not isinstance(result, dict):
            raise MCPError("Colab状态响应无效")
        if result.get("execution_id") not in {None, execution_id}:
            raise MCPError("Colab状态执行标识不匹配")
        state = result.get("state", "running")
        # The scheduler cleans gpu_diagnostics before saving it.
        evidence = {"execution_id": execution_id, "gpu_diagnostics": result.get("gpu_diagnostics")}
        if state in {"reserved", "starting", "running", None}:
            stage = result.get("stage")
            return {
                **evidence,
                "state": "running",
                "outputs": [],
                "stage": stage if isinstance(stage, str) else None,
                "timings": result.get("timings") if isinstance(result.get("timings"), dict) else {},
            }
        if state == "failed":
            return {
                **evidence,
                "state": "failed",
                "outputs": [],
                "error_code": result.get("error_code", "gpu_worker_failed"),
                "message": str(result.get("error_message", "GPU worker failed"))[:1400],
                "timings": result.get("timings") if isinstance(result.get("timings"), dict) else {},
            }
        if state != "completed":
            raise MCPError("Colab状态值无效")
        if 'artifacts' in result:
            artifacts=result['artifacts']
            if (not isinstance(artifacts,list) or not 1 <= len(artifacts) <= 100
                    or result.get('execution_id')!=execution_id
                    or len(json.dumps(artifacts))>1048576):
                raise MCPError('invalid artifact manifest')
            if any('storage' not in item for item in artifacts):
                from app.config import settings

                from .artifacts import LocalArtifactStore
                objects = self.artifact_store or LocalArtifactStore(settings.data_dir / 'assets' / 'artifacts')
                local = []
                for index, item in enumerate(artifacts):
                    value = objects.begin(self.owner_id, execution_id, item['filename'], item['mime'],
                        item['size_bytes'], item['sha256'])
                    key = value['artifact_id']
                    offset = objects.offset(key, self.owner_id, execution_id)
                    while offset < item['size_bytes']:
                        reply = self._exec_code_on_handle(
                            handle,
                            session,
                            str(uuid.uuid4()),
                            build_chunk_code(execution_id, index, offset),
                        )
                        if not isinstance(reply, dict) or reply.get('execution_id') != execution_id or reply.get('offset') != offset:
                            raise MCPError('artifact chunk identity mismatch')
                        try:
                            chunk = base64.b64decode(reply['content'], validate=True)
                        except (KeyError, ValueError, TypeError) as exc:
                            raise MCPError('artifact chunk encoding invalid') from exc
                        if not chunk or len(chunk) > 1024**2:
                            raise MCPError('artifact chunk size invalid')
                        offset = objects.append(key, self.owner_id, execution_id, offset, chunk)
                    local.append(objects.verify(key, self.owner_id, execution_id))
                artifacts = local
            return {**evidence, 'state':'completed','artifacts':artifacts,
                    'timings':result.get('timings') or {}}
        outputs = []
        total = 0
        for item in result.get("outputs", []):
            if not isinstance(item, dict) or not isinstance(item.get("content"), str):
                continue
            try:
                content = base64.b64decode(item["content"], validate=True)
            except (ValueError, TypeError) as exc:
                raise MCPError("Colab输出编码无效") from exc
            total += len(content)
            if total > MAX_OUTPUT_BYTES:
                raise MCPError("Colab输出超过100MB上限")
            outputs.append(
                {
                    "filename": str(item.get("filename", "output.bin")),
                    "content": content,
                    "mime": str(item.get("mime", "application/octet-stream")),
                    "metadata": item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
                }
            )
        timings = result.get("timings") if isinstance(result.get("timings"), dict) else {}
        return {**evidence, "state": "completed", "outputs": outputs, "timings": timings}

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


__all__ = ["MCP_ENDPOINT", "MCPError", "MCPGPUExecutor", "build_bootstrap_code", "build_poll_code"]
