"""Google Drive persistence with durable identities and verified bytes."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.security import decrypt_secret, encrypt_secret

from .google import GoogleError, GoogleService


class DriveError(GoogleError):
    pass


class _DriveSessionExpired(DriveError):
    """Only explicit 404/410 permits replacement after remote reconciliation."""


class _DriveTransportError(DriveError):
    """A request may have reached Drive even though no response was received."""


FOLDER_MIME = "application/vnd.google-apps.folder"
_ALLOWED_ROOTS = {"workflows", "outputs", "inputs", "registry", "presets"}
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_GOOGLE_UPLOAD_HOSTS = {"www.googleapis.com", "content.googleapis.com"}
_RANGE_RE = re.compile(r"bytes\s*=\s*\d+-(\d+)", re.IGNORECASE)


class DriveStorage:
    """Persist files below ``AI-Studio/<owner>/<business-key>`` on Drive.

    The business key, rather than a scheduler job dictionary or transient job
    id, determines the Drive path.  ``files`` repository rows retain the root
    folder id and verified remote file ids so retries are idempotent.
    """

    def __init__(
        self,
        job,
        store=None,
        owner_id: str | None = None,
        *,
        account_id: str | None = None,
        client: httpx.Client | None = None,
        google: GoogleService | None = None,
        base_url: str = "https://www.googleapis.com/drive/v3",
        upload_url: str = "https://www.googleapis.com/upload/drive/v3",
        timeout: float = 20.0,
        max_retries: int = 3,
        sleeper=time.sleep,
    ):
        self.job = job if isinstance(job, dict) else {"id": str(job)}
        self.job_id = str(self.job.get("id") or self.job.get("job_id") or job)
        self.store = store
        self.owner_id = owner_id or self.job.get("owner_id")
        raw_account_id = (
            account_id
            if account_id is not None
            else (self.job.get("drive_account_id") or self.job.get("account_id"))
        )
        self.compute_account_id = self.job.get("account_id") if account_id is None else account_id
        if not self.store or not self.owner_id:
            raise DriveError("DriveStorage需要Repository和owner_id")
        self.account_id = self._resolve_storage_account_id(raw_account_id)
        self.client = client or httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=False)
        self._owns_client = client is None
        self.google = google or GoogleService(store, self.owner_id, account_id=self.account_id, client=self.client)
        self.base_url = base_url.rstrip("/")
        self.upload_url = upload_url.rstrip("/")
        self.max_retries = max(1, min(int(max_retries), 5))
        self.sleeper = sleeper
        self._owner_folder_id: str | None = None
        self._root_folder_id: str | None = None

    def _request(
        self,
        method: str,
        url: str,
        *,
        params=None,
        content=None,
        headers=None,
        json_body=None,
        retryable: bool | None = None,
    ) -> httpx.Response:
        method = method.upper()
        if retryable is None:
            retryable = method in {"GET", "PUT", "HEAD"}
        token = self.google.access_token("drive")
        merged = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        if headers:
            merged.update(headers)
        for attempt in range(self.max_retries if retryable else 1):
            try:
                response = self.client.request(
                    method, url, params=params, content=content, headers=merged, json=json_body
                )
            except httpx.HTTPError as exc:
                if not retryable or attempt + 1 >= self.max_retries:
                    raise _DriveTransportError("Drive API网络请求失败") from exc
                self.sleeper(min(2**attempt, 4))
                continue
            if (
                not retryable
                or response.status_code not in _RETRYABLE_STATUSES
                or attempt + 1 >= self.max_retries
            ):
                return response
            self.sleeper(min(2**attempt, 4))
        raise _DriveTransportError("Drive API请求失败")

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        try:
            value = response.json()
            if not isinstance(value, dict):
                raise TypeError
            return value
        except (ValueError, TypeError):
            raise DriveError("Drive API响应格式无效")

    def _ensure_ok(self, response: httpx.Response, action: str) -> dict:
        if response.status_code < 200 or response.status_code >= 300:
            raise DriveError(f"Drive {action}失败（HTTP {response.status_code}）")
        return self._json(response)

    @staticmethod
    def _q(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    def _find_folder(self, name: str, parent: str) -> str | None:
        q = f"name = '{self._q(name)}' and mimeType = '{FOLDER_MIME}' and trashed = false and '{self._q(parent)}' in parents"
        response = self._request(
            "GET",
            self.base_url + "/files",
            params={"q": q, "pageSize": "10", "fields": "files(id,name,mimeType)"},
        )
        payload = self._ensure_ok(response, "目录查询")
        files = payload.get("files") or []
        return files[0].get("id") if files and isinstance(files[0], dict) else None

    def _folder(self, name: str, parent: str) -> str:
        existing = self._find_folder(name, parent)
        if existing:
            return existing
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}
        try:
            response = self._request(
                "POST", self.base_url + "/files", json_body=body, params={"fields": "id,name,mimeType"}
            )
        except DriveError:
            # A lost POST response may still mean that Drive created the folder.
            existing = self._find_folder(name, parent)
            if existing:
                return existing
            raise
        if response.status_code < 200 or response.status_code >= 300:
            existing = self._find_folder(name, parent) if response.status_code >= 500 else None
            if existing:
                return existing
            raise DriveError(f"Drive目录创建失败（HTTP {response.status_code}）")
        payload = self._json(response)
        folder_id = payload.get("id")
        if not isinstance(folder_id, str) or not folder_id:
            raise DriveError("Drive目录创建响应无效")
        return folder_id

    def _repo_files(self) -> list[dict]:
        return self.store.list("files", self.owner_id)

    def _resolve_storage_account_id(self, candidate: str | None) -> str | None:
        from .accounts import AccountService

        return AccountService(self.store, self.owner_id).resolve_drive_account_id(candidate)

    def _legacy_account_id(self) -> str:
        from .accounts import AccountService

        return AccountService.default_account_id(self.owner_id)

    def _is_legacy_account(self) -> bool:
        """Whether this storage represents the pre-account default identity."""
        return self.account_id is None or self.account_id == self._legacy_account_id()

    def _account_matches(self, row: dict) -> bool:
        """Match legacy/default rows without weakening explicit account isolation."""
        row_account = row.get("account_id")
        if self._is_legacy_account():
            return row_account in {None, "", self._legacy_account_id()}
        return row_account == self.account_id

    def _remember_root(self, remote_id: str) -> None:
        rows = [
            row
            for row in self._repo_files()
            if row.get("kind") == "drive_root" and self._account_matches(row)
        ]
        data = {
            "kind": "drive_root",
            "name": "AI-Studio",
            "remote_id": remote_id,
            "root_id": remote_id,
            "state": "verified",
            "updated_at_remote": True,
        }
        if self.account_id is not None:
            data["account_id"] = self.account_id
        if rows:
            self.store.update("files", rows[0]["id"], data, self.owner_id)
            return
        root_scope = "legacy" if self._is_legacy_account() else self.account_id
        record_id = hashlib.sha256(f"drive-root:{self.owner_id}:{root_scope}".encode()).hexdigest()
        try:
            self.store.create("files", self.owner_id, data, record_id=record_id)
        except ValueError:
            pass  # recorded by a concurrent request; the remote root is the same

    def _root_folder(self) -> str:
        if self._root_folder_id:
            return self._root_folder_id
        for row in self._repo_files():
            if row.get("kind") == "drive_root" and self._account_matches(row):
                remote_id = row.get("remote_id") or row.get("root_id")
                if isinstance(remote_id, str) and remote_id:
                    self._root_folder_id = remote_id
                    return remote_id
        root = self._folder("AI-Studio", "root")
        self._root_folder_id = root
        self._remember_root(root)
        return root

    def _normalize_key(self, key: str) -> str:
        if not isinstance(key, str) or not key or key.startswith("/") or "\\" in key:
            raise DriveError("Drive key路径无效")
        normalized = posixpath.normpath(key)
        parts = normalized.split("/")
        if (
            normalized in {".", ".."}
            or normalized.startswith("../")
            or any(not part or part == ".." for part in parts)
        ):
            raise DriveError("Drive key路径无效")
        if parts[0] not in _ALLOWED_ROOTS or len(parts) < 2:
            raise DriveError("Drive业务目录无效")
        if any(len(part) > 128 for part in parts):
            raise DriveError("Drive key路径过长")
        return normalized

    def _job_parent(self, key: str) -> str:
        normalized = self._normalize_key(key)
        if self._owner_folder_id is None:
            self._owner_folder_id = self._folder(self.owner_id, self._root_folder())
        parent = self._owner_folder_id
        for part in normalized.split("/")[:-1]:
            parent = self._folder(part, parent)
        return parent

    def model_cache(self) -> dict:
        """Folder id plus a short-lived drive.file token for the runtime's model cache.

        The cache lives at ``AI-Studio/<owner>/models`` in the user's Drive.  The
        token is handed to the trusted worker only, never stored or logged.
        """
        if self._owner_folder_id is None:
            self._owner_folder_id = self._folder(self.owner_id, self._root_folder())
        return {
            "folder_id": self._folder("models", self._owner_folder_id),
            "runtime_folder_id": self._folder("runtime-cache", self._owner_folder_id),
            "access_token": self.google.access_token("drive"),
        }

    def lora_folder(self) -> str:
        """``AI-Studio/<owner>/loras/h3``; created on first use."""
        if self._owner_folder_id is None:
            self._owner_folder_id = self._folder(self.owner_id, self._root_folder())
        return self._folder("h3", self._folder("loras", self._owner_folder_id))

    def list_loras(self) -> list[dict]:
        """.safetensors files in the LoRA folder (only files this app uploaded are visible under drive.file)."""
        folder = self.lora_folder()
        q = f"'{self._q(folder)}' in parents and trashed = false and mimeType != '{FOLDER_MIME}'"
        response = self._request(
            "GET",
            self.base_url + "/files",
            params={"q": q, "pageSize": "200", "orderBy": "modifiedTime desc",
                    "fields": "files(id,name,size,modifiedTime,md5Checksum)"},
        )
        files = self._ensure_ok(response, "LoRA 列表").get("files") or []
        return [
            {"id": f["id"], "name": f["name"], "size": int(f.get("size") or 0),
             "modified_at": f.get("modifiedTime"), "md5": f.get("md5Checksum")}
            for f in files if str(f.get("name", "")).lower().endswith(".safetensors")
        ]

    def lora_upload_session(self, name: str, size: int, origin: str) -> str:
        """Resumable upload session the browser PUTs the file to directly (no bytes pass through this server)."""
        response = self._request(
            "POST",
            self.upload_url + "/files",
            params={"uploadType": "resumable", "fields": "id,name,size"},
            json_body={"name": name, "parents": [self.lora_folder()]},
            headers={"X-Upload-Content-Type": "application/octet-stream", "X-Upload-Content-Length": str(size), "Origin": origin},
        )
        location = response.headers.get("location")
        if response.status_code >= 300 or not location:
            raise DriveError(f"LoRA 上传会话创建失败（HTTP {response.status_code}）")
        return location

    def _find_existing_file(self, name: str, parent: str, key_hash: str, sha256: str) -> dict | None:
        q = (
            f"name = '{self._q(name)}' and trashed = false and '{self._q(parent)}' in parents "
            f"and appProperties has {{ key='studio_key_hash' and value='{self._q(key_hash)}' }} "
            f"and appProperties has {{ key='studio_sha256' and value='{self._q(sha256)}' }}"
        )
        fields = "files(id,name,size,mimeType,parents,appProperties)"
        response = self._request(
            "GET", self.base_url + "/files", params={"q": q, "pageSize": "10", "fields": fields}
        )
        payload = self._ensure_ok(response, "文件查询")
        files = payload.get("files") or []
        for row in files:
            if not isinstance(row, dict):
                continue
            props = row.get("appProperties") or {}
            if props.get("studio_key_hash") == key_hash and props.get("studio_sha256") == sha256:
                return row
        return None

    def _local_match(
        self, rows: list[dict], key_hash: str, sha256: str, account_id: str | None = None
    ) -> dict | None:
        # ``account_id`` is retained for callers from the pre-isolation code;
        # the storage instance is the authority for the matching policy.
        del account_id
        for row in rows:
            if (
                row.get("kind") == "drive_file"
                and self._account_matches(row)
                and (row.get("key_hash") or row.get("studio_key_hash")) == key_hash
                and row.get("sha256") == sha256
            ):
                remote_id = row.get("remote_id") or row.get("remote_file_id")
                if remote_id:
                    return row
        return None

    def _save_file_record(
        self, key: str, key_hash: str, sha256: str, mime: str, remote: dict, size: int
    ) -> None:
        data = {
            "kind": "drive_file",
            "storage_key": key,
            "key_hash": key_hash,
            "studio_key_hash": key_hash,
            "remote_id": remote.get("id"),
            "remote_file_id": remote.get("id"),
            "name": remote.get("name") or posixpath.basename(key),
            "mime": mime,
            "size": size,
            "sha256": sha256,
            "app_properties": remote.get("appProperties") or {},
            "state": "verified",
        }
        if self.account_id is not None:
            data["account_id"] = self.account_id
        rows = [
            row
            for row in self._repo_files()
            if row.get("kind") == "drive_file"
            and row.get("key_hash") == key_hash
            and self._account_matches(row)
            and row.get("sha256") == sha256
        ]
        if rows:
            self.store.update("files", rows[0]["id"], data, self.owner_id)
            return
        file_scope = "legacy" if self._is_legacy_account() else self.account_id
        record_id = hashlib.sha256(
            f"drive-file:{self.owner_id}:{file_scope}:{key_hash}:{sha256}".encode()
        ).hexdigest()
        try:
            self.store.create("files", self.owner_id, data, record_id=record_id)
        except ValueError:
            # A concurrent upload of the same content already recorded it.
            if not self._local_match(self._repo_files(), key_hash, sha256, self.account_id):
                raise DriveError("Drive本地持久化失败")

    def _multipart(self, metadata: dict, content: bytes, mime: str) -> tuple[bytes, str]:
        boundary = "atelier_" + uuid.uuid4().hex
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
            + json.dumps(metadata, separators=(",", ":"), ensure_ascii=False).encode()
            + f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode()
            + content
            + f"\r\n--{boundary}--\r\n".encode()
        )
        return body, f"multipart/related; boundary={boundary}"

    def _recover_file(self, name: str, parent: str, key_hash: str, sha256: str) -> dict | None:
        try:
            return self._find_existing_file(name, parent, key_hash, sha256)
        except DriveError:
            return None

    def _upload(self, name: str, content: bytes, mime: str, parent: str, key_hash: str, sha256: str) -> dict:
        metadata = {
            "name": name,
            "parents": [parent],
            "mimeType": mime,
            "appProperties": {
                "studio_key_hash": key_hash,
                "studio_sha256": sha256,
                "sha256": sha256,
                "atelier_sha256": sha256,
                "atelier_job_id": self.job_id,
            },
        }
        max_bytes = int(os.getenv("GPU_DRIVE_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))
        if len(content) > max_bytes:
            raise DriveError("Drive上传超过服务器限制")
        if len(content) and (mime.startswith("video/") or len(content) > 8 * 1024 * 1024):
            return self._resumable(name, metadata, content, mime, parent, key_hash, sha256)
        body, content_type = self._multipart(metadata, content, mime)
        try:
            response = self._request(
                "POST",
                self.upload_url + "/files",
                params={"uploadType": "multipart", "fields": "id,name,size,mimeType,appProperties"},
                content=body,
                headers={"Content-Type": content_type},
            )
        except DriveError:
            existing = self._recover_file(name, parent, key_hash, sha256)
            if existing:
                return existing
            raise
        if response.status_code < 200 or response.status_code >= 300:
            existing = (
                self._recover_file(name, parent, key_hash, sha256) if response.status_code >= 500 else None
            )
            if existing:
                return existing
            raise DriveError(f"Drive文件上传失败（HTTP {response.status_code}）")
        return self._json(response)

    @staticmethod
    def _controlled_upload_location(location: str) -> bool:
        try:
            parsed = urlsplit(location)
        except ValueError:
            return False
        return (
            parsed.scheme == "https"
            and parsed.hostname in _GOOGLE_UPLOAD_HOSTS
            and parsed.path.startswith("/upload/drive/v3/")
        )

    @staticmethod
    def _range_end(response: httpx.Response) -> int | None:
        value = response.headers.get("Range")
        if not value:
            return None
        match = _RANGE_RE.search(value)
        return int(match.group(1)) if match else None

    def _resume_status(self, location: str, total: int, mime: str) -> tuple[int, dict | None]:
        response = self._request(
            "PUT",
            location,
            content=b"",
            headers={"Content-Type": mime, "Content-Length": "0", "Content-Range": f"bytes */{total}"},
        )
        if response.status_code in (200, 201):
            return total, self._json(response)
        if response.status_code != 308:
            raise DriveError(f"Drive可恢复上传状态查询失败（HTTP {response.status_code}）")
        end = self._range_end(response)
        if end is None:
            raise DriveError("Drive可恢复上传状态缺少范围")
        offset = end + 1
        if offset < 0 or offset > total:
            raise DriveError("Drive可恢复上传状态范围无效")
        return offset, None

    def _resumable(
        self, name: str, metadata: dict, content: bytes, mime: str, parent: str, key_hash: str, sha256: str
    ) -> dict:
        try:
            response = self._request(
                "POST",
                self.upload_url + "/files",
                params={"uploadType": "resumable", "fields": "id,name,size,mimeType,appProperties"},
                json_body=metadata,
                headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(len(content))},
            )
        except DriveError:
            existing = self._recover_file(name, parent, key_hash, sha256)
            if existing:
                return existing
            raise
        if response.status_code < 200 or response.status_code >= 300:
            existing = (
                self._recover_file(name, parent, key_hash, sha256) if response.status_code >= 500 else None
            )
            if existing:
                return existing
            raise DriveError(f"Drive可恢复上传初始化失败（HTTP {response.status_code}）")
        location = response.headers.get("location")
        if not location or not self._controlled_upload_location(location):
            raise DriveError("Drive可恢复上传缺少受控会话地址")
        chunk = 8 * 1024 * 1024
        offset = 0
        while offset < len(content):
            end = min(offset + chunk, len(content)) - 1
            part = content[offset : end + 1]
            try:
                response = self._request(
                    "PUT",
                    location,
                    content=part,
                    headers={"Content-Type": mime, "Content-Range": f"bytes {offset}-{end}/{len(content)}"},
                )
            except DriveError:
                offset, result = self._resume_status(location, len(content), mime)
                if result:
                    return result
                continue
            if response.status_code in (200, 201):
                return self._json(response)
            if response.status_code == 308:
                received = self._range_end(response)
                if received is None:
                    offset, result = self._resume_status(location, len(content), mime)
                    if result:
                        return result
                    continue
                next_offset = received + 1
                if next_offset < offset or next_offset > end + 1:
                    raise DriveError("Drive可恢复上传308偏移无效")
                if next_offset == offset:
                    raise DriveError("Drive可恢复上传308未推进")
                offset = next_offset
                continue
            if response.status_code in _RETRYABLE_STATUSES:
                offset, result = self._resume_status(location, len(content), mime)
                if result:
                    return result
                continue
            raise DriveError(f"Drive可恢复上传失败（HTTP {response.status_code}）")
        raise DriveError("Drive可恢复上传未完成")

    def put_file(self, key: str, source: Path, mime: str) -> dict:
        """Upload a file in bounded chunks; persist encrypted resumable capability."""
        normalized = self._normalize_key(key)
        database = getattr(self.store, 'store', self.store)
        identity = f'{self.owner_id}:{normalized}'
        with database.upload_lock(identity) as acquired:
            if not acquired:
                raise DriveError('Drive file upload busy; retry same key')
            return self._put_file_locked(normalized, source, mime)

    def _put_file_locked(self, key, source, mime):
        normalized = self._normalize_key(key)
        source = Path(source)
        if source.is_symlink() or not source.is_file() or not isinstance(mime, str) or not mime:
            raise DriveError("Drive source file invalid")
        size = source.stat().st_size
        if not 0 < size <= 2 * 1024**3:
            raise DriveError("Drive source size invalid")
        checksum = hashlib.sha256()
        with source.open('rb') as stream:
            while part := stream.read(8 * 1024**2):
                checksum.update(part)
        sha256 = checksum.hexdigest()
        key_hash = hashlib.sha256(normalized.encode()).hexdigest()
        local = self._local_match(self._repo_files(), key_hash, sha256, self.account_id)
        if local:
            remote_id = local.get('remote_id') or local.get('remote_file_id')
            if self._media_download(remote_id, size) != (size, sha256):
                raise DriveError("Drive existing file verification failed")
            return dict(id=remote_id, name=source.name, size=size, mime=mime, sha256=sha256, verified=True)
        parent = self._job_parent(normalized)
        if self._is_legacy_account():
            # Keep the pre-account hash stable so interrupted legacy uploads
            # remain resumable after the account directory migration.
            session_seed = f"upload:{self.owner_id}:{key_hash}:{sha256}"
        else:
            session_seed = f"upload:{self.owner_id}:{self.account_id}:{key_hash}:{sha256}"
        session_id = hashlib.sha256(session_seed.encode()).hexdigest()
        saved = self.store.get('files', session_id, self.owner_id)
        result = None
        if saved and saved.get('state') == 'verified':
            result = {'id': saved.get('remote_id'), 'size': str(size)}
        elif saved:
            location = decrypt_secret(saved['encrypted_session'])
            if not self._controlled_upload_location(location):
                raise DriveError("Drive stored session location invalid")
            try:
                offset, result = self._file_resume_status(location, size, mime)
            except _DriveSessionExpired:
                saved = None
        if not saved and result is None:
            existing = self._find_existing_file(posixpath.basename(normalized), parent, key_hash, sha256)
            if existing:
                result = existing
            else:
                response = self._request('POST', self.upload_url + '/files',
                    params={'uploadType': 'resumable', 'fields': 'id,name,size,mimeType,appProperties'},
                    json_body={'name': posixpath.basename(normalized), 'parents': [parent], 'mimeType': mime,
                        'appProperties': {'studio_key_hash': key_hash, 'studio_sha256': sha256}},
                    headers={'X-Upload-Content-Type': mime, 'X-Upload-Content-Length': str(size)})
                location = response.headers.get('location', '')
                if response.status_code not in (200, 201) or not self._controlled_upload_location(location):
                    raise DriveError("Drive file upload session initialization failed")
                session_data = {'kind': 'drive_upload_session', 'state': 'uploading',
                    'encrypted_session': encrypt_secret(location),
                    'size': size, 'sha256': sha256, 'key_hash': key_hash}
                if self.account_id is not None:
                    session_data['account_id'] = self.account_id
                if self.store.get('files', session_id, self.owner_id):
                    self.store.update('files', session_id, session_data, self.owner_id)
                else:
                    self.store.create('files', self.owner_id, session_data, record_id=session_id)
                offset = 0
        if result is None:
            stalled = 0
            with source.open('rb') as stream:
                while offset < size:
                    stream.seek(offset)
                    chunk = stream.read(min(8 * 1024**2, size - offset))
                    if not chunk:
                        raise DriveError("Drive source changed while uploading")
                    end = offset + len(chunk) - 1
                    previous = offset
                    try:
                        response = self._request('PUT', location, content=chunk, retryable=False,
                            headers={'Content-Type': mime, 'Content-Range': f'bytes {offset}-{end}/{size}'})
                    except _DriveTransportError:
                        offset, result = self._file_resume_status(location, size, mime)
                    else:
                        if response.status_code in (200, 201):
                            result = self._json(response)
                            offset = size
                        elif response.status_code == 308:
                            acknowledged = self._range_end(response)
                            offset = acknowledged + 1 if acknowledged is not None else 0
                            if not previous <= offset <= end + 1:
                                raise DriveError("Drive file acknowledgment invalid")
                        elif response.status_code in _RETRYABLE_STATUSES:
                            offset, result = self._file_resume_status(location, size, mime)
                        else:
                            raise DriveError("Drive file chunk rejected")
                    if result is not None:
                        break
                    stalled = stalled + 1 if offset <= previous else 0
                    if stalled >= self.max_retries:
                        raise DriveError("Drive file upload made no progress")
            if result is None:
                _, result = self._file_resume_status(location, size, mime)
        try:
            reported_size = result.get('size') if isinstance(result, dict) else None
            if isinstance(reported_size, bool) or not isinstance(reported_size, (int, str)):
                raise ValueError('invalid size type')
            valid_size = int(reported_size) == size
        except (TypeError, ValueError, OverflowError):
            valid_size = False
        if not result or not isinstance(result.get('id'), str) or not result['id'] or not valid_size:
            raise DriveError("Drive file upload completion invalid")
        if self._media_download(result['id'], size) != (size, sha256):
            raise DriveError("Drive file upload checksum invalid")
        self._save_file_record(normalized, key_hash, sha256, mime, result, size)
        if self.store.get('files', session_id, self.owner_id):
            # Replace session capability with a terminal identity; never publish the URL.
            self.store.update('files', session_id, {'state': 'verified', 'encrypted_session': '',
                'remote_id': result['id']}, self.owner_id)
        return dict(id=result['id'], name=source.name, size=size, mime=mime, sha256=sha256, verified=True)

    def _file_resume_status(self, location, size, mime):
        response = self._request('PUT', location, content=b'', retryable=False,
            headers={'Content-Type': mime, 'Content-Length': '0', 'Content-Range': f'bytes */{size}'})
        if response.status_code in (200, 201):
            return size, self._json(response)
        if response.status_code in (404, 410):
            raise _DriveSessionExpired('Drive file session expired')
        if response.status_code != 308:
            raise DriveError("Drive file session recovery failed")
        acknowledged = self._range_end(response)
        if response.headers.get('Range') and acknowledged is None:
            raise DriveError("Drive file session range invalid")
        offset = acknowledged + 1 if acknowledged is not None else 0
        if not 0 <= offset <= size:
            raise DriveError("Drive file session offset invalid")
        return offset, None

    def put(self, key: str, content: bytes, mime: str) -> dict:
        normalized = self._normalize_key(key)
        if not isinstance(content, (bytes, bytearray)) or not isinstance(mime, str) or not mime:
            raise DriveError("Drive上传参数无效")
        raw = bytes(content)
        sha256 = hashlib.sha256(raw).hexdigest()
        key_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        local = self._local_match(self._repo_files(), key_hash, sha256, self.account_id)
        if local:
            return {
                "id": local.get("remote_id") or local.get("remote_file_id"),
                "name": local.get("name", posixpath.basename(normalized)),
                "size": int(local.get("size", len(raw))),
                "mime": local.get("mime", mime),
                "sha256": sha256,
                "app_properties": local.get("app_properties") or {},
                "verified": local.get("state") == "verified",
            }
        parent = self._job_parent(normalized)
        existing = self._find_existing_file(posixpath.basename(normalized), parent, key_hash, sha256)
        record = existing or self._upload(posixpath.basename(normalized), raw, mime, parent, key_hash, sha256)
        result = {
            "id": record.get("id"),
            "name": record.get("name", posixpath.basename(normalized)),
            "size": int(record.get("size", len(raw))),
            "mime": mime,
            "sha256": sha256,
            "app_properties": record.get("appProperties") or {},
        }
        if not result["id"] or not self.verify(result, raw):
            raise DriveError("Drive上传后校验失败")
        self._save_file_record(normalized, key_hash, sha256, mime, record, len(raw))
        result["verified"] = True
        return result

    def _media_download(self, file_id: str, max_expectedbytes: int) -> tuple[int, str] | None:
        token = self.google.access_token("drive")
        headers = {"Authorization": "Bearer " + token, "Accept": "application/octet-stream"}
        for attempt in range(self.max_retries):
            response = None
            try:
                request = self.client.build_request(
                    "GET", self.base_url + "/files/" + file_id, params={"alt": "media"}, headers=headers
                )
                response = self.client.send(request, stream=True)
                if response.status_code in _RETRYABLE_STATUSES and attempt + 1 < self.max_retries:
                    response.close()
                    self.sleeper(min(2**attempt, 4))
                    continue
                if response.status_code < 200 or response.status_code >= 300:
                    return None
                length = response.headers.get("Content-Length")
                if length and int(length) > max_expectedbytes:
                    return None
                digest = hashlib.sha256()
                size = 0
                for part in response.iter_bytes(chunk_size=64 * 1024):
                    size += len(part)
                    if size > max_expectedbytes:
                        return None
                    digest.update(part)
                return size, digest.hexdigest()
            except (httpx.HTTPError, ValueError):
                if attempt + 1 >= self.max_retries:
                    return None
                self.sleeper(min(2**attempt, 4))
            finally:
                if response is not None:
                    response.close()
        return None

    def verify(self, record: dict, content: bytes, *, max_expectedbytes: int | None = None) -> bool:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("id"), str)
            or not isinstance(content, (bytes, bytearray))
        ):
            return False
        raw = bytes(content)
        expected_hash = (
            record.get("sha256")
            or (record.get("app_properties") or {}).get("sha256")
            or (record.get("app_properties") or {}).get("studio_sha256")
            or (record.get("app_properties") or {}).get("atelier_sha256")
        )
        if expected_hash != hashlib.sha256(raw).hexdigest():
            return False
        try:
            if record.get("size") is not None and int(record["size"]) != len(raw):
                return False
        except (TypeError, ValueError):
            return False
        limit = len(raw) if max_expectedbytes is None else max_expectedbytes
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < len(raw):
            return False
        downloaded = self._media_download(record["id"], limit)
        return downloaded == (len(raw), expected_hash)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


__all__ = ["DriveError", "DriveStorage"]
