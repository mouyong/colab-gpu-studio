"""Google OAuth 2.0 credentials for the independent Colab GPU Studio API.

The adapter deliberately stores only encrypted token values.  OAuth callback
state is also persisted as a short-lived credential record so a callback can
resolve its owner without trusting a browser cookie or a caller supplied owner.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import time
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.security import decrypt_secret, encrypt_secret

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


class GoogleError(RuntimeError):
    """Safe adapter error; upstream bodies are never included."""


class GoogleConfigurationError(GoogleError):
    pass


class GoogleOAuthError(GoogleError):
    pass


_PURPOSES = {
    "colab": "https://www.googleapis.com/auth/colaboratory",
    "drive": "https://www.googleapis.com/auth/drive.file",
}
_STATE_CONSUME_LOCK = threading.Lock()
_REFRESH_FALLBACK_LOCKS: dict[str, threading.Lock] = {}
_REFRESH_FALLBACK_GUARD = threading.Lock()


def _safe_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> float:
    return time.time()


class GoogleService:
    """Owner-bound Google OAuth service.

    ``store`` is the independent project's Repository and must expose the
    repository CRUD surface.  Credentials are records with ``owner_id`` and
    ``purpose``.  No access or refresh token is returned from any public method.
    """

    def __init__(
        self,
        store,
        owner_id: str | None,
        *,
        account_id: str | None = None,
        client: httpx.Client | None = None,
        clock=_now,
        state_ttl_seconds: int = 600,
    ):
        self.store = store
        self.owner_id = owner_id
        # None is the legacy single-account slot. Explicit account ids never
        # fall back to that slot or to another account.
        self.account_id = account_id
        self.client = client or httpx.Client(timeout=httpx.Timeout(10.0), follow_redirects=False)
        self._owns_client = client is None
        self.clock = clock
        self.state_ttl_seconds = state_ttl_seconds

    @staticmethod
    def callback_uri() -> str:
        return str(getattr(settings, "site_url", "")).rstrip("/") + "/api/gpu/auth/google/callback"

    def _account_matches(self, row: dict, account_id: str | None = None) -> bool:
        account = self.account_id if account_id is None else account_id
        row_account = row.get("account_id")
        owner = row.get("owner_id") or self.owner_id
        legacy_default = owner and self._legacy_default_id(owner)
        if account is None:
            return row_account in (None, "", legacy_default)
        if row_account == account:
            return True
        # The deterministic migrated default account is the same identity as
        # the old unscoped slot.  Other explicit accounts never use it.
        return account == legacy_default and row_account in (None, "")

    def _legacy_default_config(self, row: dict, owner_id: str) -> bool:
        """Return whether a migrated default app may be shared owner-wide.

        The migration temporarily attached the old owner-level OAuth app to the
        deterministic legacy account.  That row is safe to reuse for a newly
        created account only when the account directory marks that identity as
        the trusted legacy default.  A normal account-specific OAuth app never
        becomes owner-wide through this path.
        """
        default_id = self._legacy_default_id(owner_id)
        if row.get("account_id") != default_id:
            return False
        from app.oauth_config_routes import config_id

        if row.get("id") == config_id(owner_id, default_id):
            return False  # Newly saved dedicated apps are never legacy-shared.
        default = self.store.get("accounts", default_id, owner_id)
        return bool(default and (default.get("legacy_default") is True or default.get("legacy_trusted") is True))

    def _client_config(self, owner_id: str | None = None, account_id: str | None = None) -> dict | None:
        owner = self.owner_id if owner_id is None else owner_id
        if not owner:
            return None
        rows = [
            row for row in self._list("credentials", owner)
            if row.get("provider") == "google"
            and row.get("purpose") == "oauth_client_config"
            and not row.get("revoked")
        ]
        # Web setup saves an owner-wide OAuth application, not an account token.
        # Prefer an account-specific application, then the same owner's shared
        # application. Never borrow another account's app or another owner's app.
        candidates = rows
        exact = [row for row in candidates if account_id is not None and row.get("account_id") == account_id]
        scoped = exact or [row for row in candidates if row.get("account_id") in (None, "")]
        if scoped:
            from app.oauth_config_routes import config_id

            canonical = config_id(owner, account_id) if account_id is not None else config_id(owner)
            rows = sorted(scoped, key=lambda row: row.get("id") != canonical)
        elif account_id is not None:
            # First preserve the original owner-wide application config. If
            # migration attached that same app to the trusted legacy default,
            # permit that exact compatibility case as the final fallback.
            rows = [row for row in rows if row.get("account_id") in (None, "")]
            if not rows:
                # Never borrow a regular account's app or any account's bearer
                # token through this compatibility path.
                rows = [row for row in candidates if self._legacy_default_config(row, owner)]
        else:
            rows = [row for row in candidates if self._legacy_default_config(row, owner)]
        if not rows:
            return None
        row = rows[0]
        try:
            secret = decrypt_secret(row["encrypted_client_secret"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GoogleConfigurationError("Google OAuth配置不可用") from exc
        return {"client_id": str(row.get("client_id", "")).strip(), "client_secret": secret, "id": row.get("id")}

    @staticmethod
    def _legacy_default_id(owner_id: str) -> str:
        from .accounts import AccountService

        return AccountService.default_account_id(owner_id)

    def _config(self, owner_id: str | None = None, account_id: str | None = None) -> tuple[str, str, str]:
        effective_owner = self.owner_id if owner_id is None else owner_id
        effective_account = self.account_id if account_id is None else account_id
        database = self._client_config(effective_owner, effective_account)
        if database:
            client_id, client_secret = database["client_id"], database["client_secret"]
        else:
            client_id = os.getenv("GPU_GOOGLE_CLIENT_ID", "").strip()
            client_secret = (
                os.getenv("GPU_GOOGLE_SECRET") or os.getenv("GPU_GOOGLE_CLIENT_SECRET") or ""
            ).strip()
        redirect = self.callback_uri()
        if not client_id or not client_secret or not redirect:
            raise GoogleConfigurationError("Google OAuth未配置，真实GPU能力已关闭")
        if not redirect.startswith("https://") and str(getattr(settings, "environment", "development")).lower() in {
            "prod",
            "production",
        }:
            raise GoogleConfigurationError("生产环境Google OAuth回调必须使用HTTPS")
        return client_id, client_secret, redirect

    def _list(self, kind: str, owner_id: str | None = None) -> list[dict]:
        return self.store.list(kind, owner_id)

    def _create(self, kind: str, owner_id: str, data: dict) -> dict:
        return self.store.create(kind, owner_id, data)

    def _update(self, kind: str, record_id: str, patch: dict, owner_id: str | None = None) -> dict:
        return self.store.update(kind, record_id, patch, owner_id)


    def _credential(self, purpose: str, owner_id: str | None = None, account_id: str | None = None) -> dict | None:
        owner = self.owner_id if owner_id is None else owner_id
        if not owner:
            return None
        rows = [
            r
            for r in self._list("credentials", owner)
            if r.get("purpose") == purpose
            and r.get("provider", "google") == "google"
            and not r.get("revoked")
            and self._account_matches(r, account_id)
        ]
        return rows[0] if rows else None

    def status(self) -> dict:
        configured = all(self._config_value())
        result: dict[str, dict[str, Any]] = {}
        for purpose in _PURPOSES:
            credential = self._credential(purpose, account_id=self.account_id)
            scope_ok = bool(credential and self._scope_sufficient(purpose, credential.get("scope")))
            connected = bool(credential and credential.get("encrypted_refresh_token") and scope_ok)
            cred_configured = bool(
                credential
                and str(credential.get("client_id", "")).strip()
                and credential.get("encrypted_client_secret")
            )
            result[purpose] = {
                "connected": connected,
                "configured": configured or cred_configured,
                "expires_at": credential.get("expires_at") if credential else None,
            }
            if credential and not scope_ok:
                result[purpose]["message"] = "Google权限不足，请重新授权"
        return result

    @staticmethod
    def _scope_sufficient(purpose: str, scope: Any) -> bool:
        # Older records did not persist Google's scope response.  Explicitly
        # insufficient scopes fail closed; missing legacy values remain usable.
        if not scope:
            return True
        if isinstance(scope, (list, tuple, set)):
            values = {str(item) for item in scope}
        else:
            values = set(str(scope).split())
        return _PURPOSES[purpose] in values

    def _config_value(self) -> tuple[str, str, str]:
        try:
            return self._config(self.owner_id, self.account_id)
        except GoogleConfigurationError:
            return "", "", self.callback_uri()

    def invalidate_oauth_states(self, owner_id: str | None = None, *, account_id: str | None = None) -> None:
        owner = self.owner_id if owner_id is None else owner_id
        if not owner:
            return
        for row in self._list("credentials", owner):
            if account_id is not None and not self._account_matches(row, account_id):
                continue
            if row.get("purpose") == "_oauth_state" and not row.get("used_at"):
                self._update("credentials", row["id"], {"used_at": self.clock()}, owner)

    def start(self, purpose: str) -> dict:
        if purpose not in _PURPOSES:
            raise ValueError("Google OAuth purpose无效")
        if not self.owner_id:
            raise GoogleOAuthError("当前登录用户无效")
        client_id, _secret, redirect = self._config()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self._create(
            "credentials",
            self.owner_id,
            {
                "provider": "google",
                "purpose": "_oauth_state",
                "oauth_purpose": purpose,
                **({"account_id": self.account_id} if self.account_id is not None else {}),
                "state_hash": _safe_id(state),
                "encrypted_code_verifier": encrypt_secret(verifier),
                "expires_at": self.clock() + self.state_ttl_seconds,
                "used_at": None,
            },
        )
        query = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect,
                "response_type": "code",
                "scope": _PURPOSES[purpose],
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return {"authorization_url": AUTH_URL + "?" + query, "state": state}

    def _take_state(self, state: str) -> dict:
        if not isinstance(state, str) or not state or len(state) > 512:
            raise GoogleOAuthError("OAuth state无效或已过期")
        # Lock only the read-and-consume transition.  The network token
        # exchange happens after this context is released.
        with self._state_consume_guard():
            rows = [
                r
                for r in self._list("credentials")
                if r.get("purpose") == "_oauth_state" and r.get("state_hash") == _safe_id(state)
            ]
            if len(rows) != 1:
                raise GoogleOAuthError("OAuth state无效或已过期")
            row = rows[0]
            if row.get("used_at") or row.get("used") or float(row.get("expires_at", 0)) <= self.clock():
                raise GoogleOAuthError("OAuth state无效或已过期")
            # Repository.dispatch_lock is the cross-process SQLite lock.  The
            # process lock keeps the intentionally minimal memory repo safe.
            self._update("credentials", row["id"], {"used_at": self.clock()}, row.get("owner_id"))
            return row

    @contextmanager
    def _state_consume_guard(self):
        dispatch = getattr(self.store, "dispatch_lock", None)
        with _STATE_CONSUME_LOCK:
            if callable(dispatch):
                with dispatch() as acquired:
                    if not acquired:
                        raise GoogleOAuthError("OAuth state无效或已过期")
                    yield
                return
            yield

    def callback(self, code: str, state: str) -> dict:
        if not isinstance(code, str) or not code or len(code) > 8192:
            raise GoogleOAuthError("Google OAuth code无效")
        row = self._take_state(state)
        # The callback starts without an owner.  The consumed state is the
        # only authority for selecting the owner's encrypted client config.
        client_id, client_secret, redirect = self._config(row.get("owner_id"), row.get("account_id"))
        try:
            verifier = decrypt_secret(row["encrypted_code_verifier"])
            response = self.client.post(
                TOKEN_URL,
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect,
                    "grant_type": "authorization_code",
                    "code_verifier": verifier,
                },
            )
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise GoogleOAuthError("Google OAuth token交换失败") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise GoogleOAuthError("Google OAuth token交换失败")
        try:
            payload = response.json()
            access = payload.get("access_token")
            refresh = payload.get("refresh_token")
            expires_in = int(payload.get("expires_in", 3600))
            if not isinstance(access, str) or not access:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise GoogleOAuthError("Google OAuth响应无效")
        old = self._credential(row.get("oauth_purpose"), row.get("owner_id"), row.get("account_id"))
        if not refresh and old:
            try:
                refresh = decrypt_secret(old["encrypted_refresh_token"])
            except (KeyError, TypeError, ValueError):
                refresh = None
        if not refresh:
            raise GoogleOAuthError("Google OAuth未返回可持久化刷新凭证")
        data = {
            "provider": "google",
            "purpose": row["oauth_purpose"],
            "encrypted_access_token": encrypt_secret(access),
            "encrypted_refresh_token": encrypt_secret(refresh),
            "expires_at": self.clock() + max(0, expires_in - 60),
            "scope": payload.get("scope", _PURPOSES[row["oauth_purpose"]]),
            "token_type": payload.get("token_type", "Bearer"),
            **({"account_id": row.get("account_id")} if row.get("account_id") is not None else {}),
        }
        if old:
            self._update("credentials", old["id"], data, row.get("owner_id"))
        else:
            self._create("credentials", row["owner_id"], data)
        connected = self._scope_sufficient(row["oauth_purpose"], data["scope"])
        result = {"purpose": row["oauth_purpose"], "connected": connected, "owner_id": row["owner_id"]}
        if row.get("account_id") is not None:
            result["account_id"] = row["account_id"]
        if not connected:
            result["message"] = "Google权限不足，请重新授权"
        return result

    def access_token(self, purpose: str) -> str:
        if purpose not in _PURPOSES:
            raise ValueError("Google OAuth purpose无效")
        credential = self._credential(purpose, account_id=self.account_id)
        if not credential:
            raise GoogleOAuthError("Google OAuth未连接，真实GPU能力已关闭")
        if not self._scope_sufficient(purpose, credential.get("scope")):
            raise GoogleOAuthError("Google OAuth权限不足，请重新授权")
        try:
            access = decrypt_secret(credential["encrypted_access_token"])
            refresh = decrypt_secret(credential["encrypted_refresh_token"])
        except (KeyError, ValueError) as exc:
            raise GoogleOAuthError("Google OAuth凭证不可用") from exc
        if float(credential.get("expires_at", 0)) > self.clock() + 60:
            return access
        cred_client_id = str(credential.get("client_id", "")).strip()
        if cred_client_id and credential.get("encrypted_client_secret"):
            try:
                client_id = cred_client_id
                client_secret = decrypt_secret(credential["encrypted_client_secret"])
            except (KeyError, TypeError, ValueError) as exc:
                raise GoogleConfigurationError("Google OAuth配置不可用") from exc
        else:
            client_id, client_secret, _redirect = self._config()
        try:
            response = self.client.post(
                TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            raise GoogleOAuthError("Google OAuth刷新失败") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise GoogleOAuthError("Google OAuth刷新失败")
        try:
            payload = response.json()
            new_access = payload.get("access_token")
            expires_in = int(payload.get("expires_in", 3600))
            if not isinstance(new_access, str) or not new_access:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise GoogleOAuthError("Google OAuth刷新响应无效")
        new_refresh = payload.get("refresh_token")
        self._update(
            "credentials",
            credential["id"],
            {
                "encrypted_access_token": encrypt_secret(new_access),
                "encrypted_refresh_token": encrypt_secret(new_refresh)
                if new_refresh
                else credential["encrypted_refresh_token"],
                "expires_at": self.clock() + max(0, expires_in - 60),
            },
            self.owner_id,
        )
        return new_access

    def disconnect(self, purpose: str) -> bool:
        if purpose not in _PURPOSES:
            raise ValueError("Google OAuth purpose无效")
        credential = self._credential(purpose, account_id=self.account_id)
        if not credential:
            return False
        remote_confirmed = False
        try:
            refresh = decrypt_secret(credential.get("encrypted_refresh_token", ""))
            token = refresh or decrypt_secret(credential.get("encrypted_access_token", ""))
            response = self.client.post(REVOKE_URL, data={"token": token})
            remote_confirmed = 200 <= response.status_code < 300
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            # Do not include the token or upstream response body in any error.
            remote_confirmed = False
        # Remove local bearer material and keep a sanitized tombstone that records
        # whether Google confirmed the revoke.
        self._update(
            "credentials",
            credential["id"],
            {
                "revoked": True,
                "encrypted_access_token": None,
                "encrypted_refresh_token": None,
                "expires_at": 0,
                "disconnect_remote_confirmed": remote_confirmed,
                "disconnect_status": "confirmed" if remote_confirmed else "remote_unconfirmed",
            },
            self.owner_id,
        )
        return True

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


__all__ = ["GoogleConfigurationError", "GoogleError", "GoogleOAuthError", "GoogleService"]
