"""Safe, independent HTTP connector for the deployed Atelier API.

This module intentionally does not import Atelier code or share its database.  It
only speaks the documented HTTP contract and keeps authentication state inside
the ``httpx.Client`` session.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import mimetypes
import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

_SESSION_COOKIE = "atelier_session"
_DEFAULT_ALLOWED_HOST = "atelier.tizenty.xyz"
_DEFAULT_MAX_UPLOAD_BYTES = 512 * 1024 * 1024
_JOB_ID_RE = re.compile(r"(?:^|[-_.])job(?:[-_.]|$)", re.IGNORECASE)
_CLIENT_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")

# These names are never accepted from a downstream job payload.  Authorization
# and credentials belong to the connector/server configuration, not the job.
_FORBIDDEN_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "cookie",
    "password",
    "refresh_token",
    "secret",
    "access_token",
)
_FORBIDDEN_KEYS = {
    "auth",
    "confirmed",
    "headers",
    "owner_id",
    "session",
    "token",
    "user_id",
}


class AtelierConnectorError(RuntimeError):
    """Base class for connector failures."""


class AtelierSecurityError(AtelierConnectorError):
    """The request would violate the connector's security boundary."""


class AtelierIntegrityError(AtelierConnectorError):
    """The remote receipt or downloaded bytes do not match the local artifact."""


class ConfirmationRequiredError(AtelierConnectorError):
    """A billable downstream submission was attempted without confirmation."""


class AtelierHTTPError(AtelierConnectorError):
    """Atelier returned a non-success response."""

    def __init__(self, status_code: int, method: str, path: str):
        super().__init__(f"Atelier HTTP {status_code} for {method} {path}")
        self.status_code = status_code
        self.method = method
        self.path = path


class AtelierConnector:
    """Synchronous, allow-listed client for the Atelier HTTP API.

    ``client`` is injectable for deterministic contract tests.  Even with an
    injected transport, URL validation and redirect refusal remain enabled.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = self._validate_base_url(base_url)
        self.username = self._require_text(username, "username")
        self.password = self._require_text(password, "password")
        self._owned_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
        )
        if bool(getattr(self.client, "follow_redirects", False)):
            raise AtelierSecurityError("Atelier connector refuses redirect-following clients")
        self._csrf_token: str | None = None
        self._logged_in = False

    def __enter__(self) -> "AtelierConnector":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._owned_client:
            self.client.close()

    @staticmethod
    def _require_text(value: str, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        return value

    @classmethod
    def _allowed_hosts(cls) -> set[str]:
        raw = os.environ.get("ATELIER_ALLOWED_HOSTS", _DEFAULT_ALLOWED_HOST)
        hosts = {item.strip().lower().rstrip(".") for item in raw.split(",") if item.strip()}
        if not hosts:
            raise AtelierSecurityError("ATELIER_ALLOWED_HOSTS must not be empty")
        return hosts

    @classmethod
    def _validate_base_url(cls, base_url: str) -> str:
        if not isinstance(base_url, str):
            raise AtelierSecurityError("Atelier base URL must be a string")
        parsed = urlsplit(base_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host:
            raise AtelierSecurityError("Atelier base URL must use HTTPS")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise AtelierSecurityError("Atelier base URL may not contain credentials, query, or fragment")
        if parsed.port not in (None, 443):
            raise AtelierSecurityError("Atelier base URL must use port 443")
        if host not in cls._allowed_hosts():
            raise AtelierSecurityError(f"Atelier host is not allow-listed: {host}")
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local")):
            raise AtelierSecurityError("Atelier base URL may not target a local hostname")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise AtelierSecurityError("Atelier base URL may not target an internal or special IP")
        if parsed.path not in ("", "/"):
            raise AtelierSecurityError("Atelier base URL must be an origin, not an arbitrary path")
        return f"https://{host}"

    def _url(self, path: str) -> str:
        if not isinstance(path, str) or not path.startswith("/") or "?" in path or "#" in path:
            raise AtelierSecurityError("connector paths must be fixed API paths")
        return self.base_url + path

    @staticmethod
    def _segment(value: str, name: str) -> str:
        if not isinstance(value, str) or not value or value in {".", ".."}:
            raise AtelierSecurityError(f"invalid {name}")
        if any(char in value for char in "/\\\x00\r\n"):
            raise AtelierSecurityError(f"invalid {name}")
        return value

    @staticmethod
    def _upload_limit() -> int:
        raw = os.environ.get("ATELIER_MAX_UPLOAD_BYTES")
        if raw is None:
            return _DEFAULT_MAX_UPLOAD_BYTES
        try:
            limit = int(raw)
        except ValueError as exc:
            raise AtelierSecurityError("ATELIER_MAX_UPLOAD_BYTES must be an integer") from exc
        if limit <= 0:
            raise AtelierSecurityError("ATELIER_MAX_UPLOAD_BYTES must be positive")
        return limit

    @classmethod
    def _filename(cls, filename: str) -> str:
        if not isinstance(filename, str) or not filename or len(filename) > 255:
            raise AtelierSecurityError("filename must be 1..255 characters")
        if filename in {".", ".."} or any(char in filename for char in "/\\\x00\r\n"):
            raise AtelierSecurityError("filename must be a single safe path component")
        if not _JOB_ID_RE.search(filename):
            raise AtelierSecurityError("filename must contain a controlled job identifier")
        return filename

    @staticmethod
    def _json(response: httpx.Response, method: str, path: str) -> Any:
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise AtelierConnectorError(f"Atelier returned non-JSON for {method} {path}") from exc

    def _raw_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = self._url(path)
        try:
            response = self.client.request(method, url, follow_redirects=False, **kwargs)
        except httpx.TimeoutException:
            # export_asset needs to distinguish a timeout after POST from a
            # normal transport failure so it can reconcile by listing assets.
            raise
        except httpx.TransportError as exc:
            raise AtelierConnectorError(f"Atelier transport failed for {method} {path}") from exc
        if 300 <= response.status_code < 400:
            raise AtelierSecurityError(f"Atelier redirect refused for {method} {path}")
        if response.status_code >= 400:
            raise AtelierHTTPError(response.status_code, method, path)
        return response

    def _json_request(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._json(self._raw_request(method, path, **kwargs), method, path)

    def login(self) -> dict[str, Any]:
        """Authenticate and retain the server-issued CSRF token in memory."""
        data = self._json_request(
            "POST",
            "/api/auth/login",
            json={"username": self.username, "password": self.password},
        )
        if not isinstance(data, Mapping):
            raise AtelierConnectorError("Atelier login response must be an object")
        csrf = data.get("csrf_token")
        if not isinstance(csrf, str) or not csrf:
            raise AtelierSecurityError("Atelier login did not return a CSRF token")
        if _SESSION_COOKIE not in self.client.cookies:
            raise AtelierSecurityError("Atelier login did not establish the session cookie")
        self._csrf_token = csrf
        self._logged_in = True
        # The CSRF token is retained only in the server-side connector state;
        # never project it into a caller's response or persisted receipt.
        return {key: value for key, value in data.items() if key != "csrf_token"}

    def _ensure_login(self) -> None:
        if not self._logged_in or not self._csrf_token:
            self.login()

    def _csrf_headers(self) -> dict[str, str]:
        self._ensure_login()
        assert self._csrf_token is not None
        return {"X-CSRF-Token": self._csrf_token}

    @staticmethod
    def _unwrap_data(data: Any) -> Any:
        if isinstance(data, Mapping) and isinstance(data.get("data"), (list, dict)):
            return data["data"]
        return data

    def projects(self) -> list[dict[str, Any]]:
        self._ensure_login()
        data = self._unwrap_data(self._json_request("GET", "/api/projects"))
        if not isinstance(data, list) or not all(isinstance(item, Mapping) for item in data):
            raise AtelierConnectorError("Atelier projects response must be a list")
        return [dict(item) for item in data]

    list_projects = projects

    def project_assets(self, project_id: str) -> list[dict[str, Any]]:
        project_id = self._segment(project_id, "project_id")
        self._ensure_login()
        data = self._unwrap_data(self._json_request("GET", f"/api/projects/{project_id}/assets"))
        if not isinstance(data, list) or not all(isinstance(item, Mapping) for item in data):
            raise AtelierConnectorError("Atelier assets response must be a list")
        return [dict(item) for item in data]

    list_project_assets = project_assets

    @staticmethod
    def _asset_filename(asset: Mapping[str, Any]) -> str | None:
        for key in ("filename", "name", "original_filename"):
            value = asset.get(key)
            if isinstance(value, str):
                return value
        return None

    def _download_verified(
        self,
        asset: Mapping[str, Any],
        expected_sha256: str,
        expected_size: int,
    ) -> dict[str, Any]:
        asset_id = self._segment(str(asset.get("id", "")), "asset_id")
        response = self._raw_request("GET", f"/api/assets/{asset_id}/download")
        actual_size = len(response.content)
        actual_sha256 = hashlib.sha256(response.content).hexdigest()
        if actual_size != expected_size or actual_sha256 != expected_sha256:
            raise AtelierIntegrityError(
                f"download verification failed for asset {asset_id}: "
                f"expected sha256={expected_sha256} size={expected_size}, "
                f"got sha256={actual_sha256} size={actual_size}"
            )
        result = {
            "id": asset_id,
            "mime": asset.get("mime") or asset.get("content_type") or "application/octet-stream",
            "size": expected_size,
            "sha256": expected_sha256,
            "project_id": asset.get("project_id"),
        }
        return result

    def _find_verified_duplicate(
        self,
        project_id: str,
        filename: str,
        sha256: str,
        size: int,
    ) -> dict[str, Any] | None:
        for asset in self.project_assets(project_id):
            if asset.get("project_id") not in (None, project_id):
                continue
            if asset.get("sha256") != sha256 or asset.get("size") != size:
                continue
            if self._asset_filename(asset) != filename:
                continue
            verified = self._download_verified(asset, sha256, size)
            verified["project_id"] = project_id
            verified["deduplicated"] = True
            return verified
        return None

    def export_asset(self, project_id: str, filename: str, content: bytes) -> dict[str, Any]:
        """Upload one controlled job artifact, idempotently and with read-back verification."""
        project_id = self._segment(project_id, "project_id")
        filename = self._filename(filename)
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise TypeError("content must be bytes-like")
        content = bytes(content)
        if len(content) > self._upload_limit():
            raise AtelierSecurityError("artifact exceeds ATELIER_MAX_UPLOAD_BYTES")
        sha256 = hashlib.sha256(content).hexdigest()
        duplicate = self._find_verified_duplicate(project_id, filename, sha256, len(content))
        if duplicate is not None:
            return duplicate

        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            receipt = self._json_request(
                "POST",
                "/api/assets/upload",
                headers=self._csrf_headers(),
                data={"project_id": project_id},
                files={"file": (filename, content, mime)},
            )
        except httpx.TimeoutException as exc:
            # A timed-out response may mean Atelier committed the upload.  Query
            # and verify it before ever considering another POST.
            recovered = self._find_verified_duplicate(project_id, filename, sha256, len(content))
            if recovered is not None:
                recovered["deduplicated"] = True
                recovered["recovered_after_timeout"] = True
                return recovered
            raise AtelierConnectorError(
                "Atelier upload timed out and no committed artifact was found"
            ) from exc

        receipt = self._unwrap_data(receipt)
        if not isinstance(receipt, Mapping):
            raise AtelierConnectorError("Atelier upload receipt must be an object")
        if receipt.get("project_id") not in (None, project_id):
            raise AtelierIntegrityError("Atelier upload receipt has the wrong project")
        if receipt.get("sha256") != sha256 or receipt.get("size") != len(content):
            raise AtelierIntegrityError("Atelier upload receipt sha256/size does not match local content")
        verified = self._download_verified(receipt, sha256, len(content))
        verified["project_id"] = project_id
        verified["deduplicated"] = False
        return verified

    @classmethod
    def _sanitize_job_value(cls, value: Any, key_path: str = "body") -> Any:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, child in value.items():
                if not isinstance(key, str):
                    raise AtelierSecurityError(f"job payload key at {key_path} must be a string")
                normalized = key.lower().replace("-", "_")
                if normalized in _FORBIDDEN_KEYS or any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
                    raise AtelierSecurityError(f"job payload key is not allowed: {key}")
                if normalized in {"url", "endpoint", "notebook", "python", "shell", "command", "code"}:
                    raise AtelierSecurityError(f"job payload key is not allowed: {key}")
                result[key] = cls._sanitize_job_value(child, f"{key_path}.{key}")
            return result
        if isinstance(value, list):
            return [cls._sanitize_job_value(item, f"{key_path}[]") for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return copy.deepcopy(value)
        raise AtelierSecurityError(f"job payload contains unsupported value at {key_path}")

    @classmethod
    def _job_payload(cls, body: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(body, Mapping):
            raise TypeError("job body must be an object")
        return cls._sanitize_job_value(body)

    def preview_job(self, body: Mapping[str, Any]) -> dict[str, Any]:
        payload = self._job_payload(body)
        result = self._json_request(
            "POST",
            "/api/jobs/preview",
            headers=self._csrf_headers(),
            json=payload,
        )
        if not isinstance(result, Mapping):
            raise AtelierConnectorError("Atelier preview response must be an object")
        return dict(result)

    def submit_job(self, body: Mapping[str, Any], confirmed: bool = False) -> dict[str, Any]:
        if confirmed is not True:
            raise ConfirmationRequiredError("submit_job requires confirmed=True")
        payload = self._job_payload(body)
        request_id = payload.get("client_request_id")
        if not isinstance(request_id, str) or not _CLIENT_REQUEST_ID_RE.fullmatch(request_id):
            raise AtelierSecurityError("submit_job requires a stable client_request_id")
        payload["confirmed"] = True
        result = self._json_request(
            "POST",
            "/api/jobs",
            headers=self._csrf_headers(),
            json=payload,
        )
        if not isinstance(result, Mapping):
            raise AtelierConnectorError("Atelier submit response must be an object")
        return dict(result)
