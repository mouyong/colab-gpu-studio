"""Authenticated, redacted Google OAuth client configuration endpoints."""

from __future__ import annotations

import hashlib
import os
import re

from fastapi import APIRouter, Depends, Request

from .common import APIError, admin, current_user, get_store, ok
from .gpu.accounts import AccountService
from .gpu.google import GoogleService
from .gpu.repository import Repository
from .security import decrypt_secret, encrypt_secret

router = APIRouter(prefix="/api/gpu")
_CLIENT_ID = re.compile(r"^[0-9]{3,32}-[A-Za-z0-9_-]{3,128}\.apps\.googleusercontent\.com$")
_MAX_SECRET = 4096


def config_id(owner_id: str, account_id: str | None = None) -> str:
    """Return the stable owner-wide or account-scoped configuration id.

    The owner-wide id is intentionally unchanged for backwards compatibility;
    account-scoped ids are deterministic and cannot collide with that legacy id.
    """
    if account_id is None:
        return "google:oauth-client:" + hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:40]
    return "google:oauth-client:" + hashlib.sha256(
        (owner_id + "\0" + account_id).encode("utf-8")
    ).hexdigest()[:40]


def callback_uri() -> str:
    return GoogleService.callback_uri()


def _environment_config() -> tuple[str, str]:
    client_id = os.getenv("GPU_GOOGLE_CLIENT_ID", "").strip()
    secret = (os.getenv("GPU_GOOGLE_SECRET") or os.getenv("GPU_GOOGLE_CLIENT_SECRET") or "").strip()
    return client_id, secret


def _account_id(body: dict | None, *, query: str | None = None) -> str | None:
    value = body.get("account_id") if body is not None else query
    present = "account_id" in body if body is not None else query is not None
    if not present:
        return None
    if not isinstance(value, str) or not value.strip():
        raise APIError("ACCOUNT_INVALID", "account_id必须为非空字符串", 422)
    return value.strip()


def _parse(body: dict) -> tuple[str, str, str | None]:
    if not isinstance(body, dict) or body.get("confirmed") is not True:
        raise APIError("CONFIRMATION_REQUIRED", "必须明确确认保存Google OAuth客户端配置", 403)
    account_id = _account_id(body)
    client_json = body.get("client_json")
    if client_json is not None:
        if not isinstance(client_json, dict) or "installed" in client_json or "web" not in client_json:
            raise APIError("GOOGLE_CLIENT_INVALID", "仅支持Web类型Google OAuth客户端", 422)
        # auth_uri/token_uri are intentionally ignored; endpoints are constants
        # in GoogleService and cannot be supplied by a client.
        web = client_json["web"]
        if not isinstance(web, dict):
            raise APIError("GOOGLE_CLIENT_INVALID", "client_json.web必须是对象", 422)
        client_id, client_secret = web.get("client_id"), web.get("client_secret")
        redirects = web.get("redirect_uris")
        if not isinstance(redirects, list) or callback_uri() not in redirects:
            raise APIError("GOOGLE_REDIRECT_MISMATCH", "redirect_uris必须包含服务端固定回调地址", 422)
    else:
        client_id, client_secret = body.get("client_id"), body.get("client_secret")
    if not isinstance(client_id, str) or not _CLIENT_ID.fullmatch(client_id.strip()):
        raise APIError("GOOGLE_CLIENT_INVALID", "client_id格式无效", 422)
    if not isinstance(client_secret, str) or not 8 <= len(client_secret) <= _MAX_SECRET:
        raise APIError("GOOGLE_CLIENT_INVALID", "client_secret长度无效", 422)
    return client_id.strip(), client_secret, account_id


def _is_config(row: dict) -> bool:
    return (
        row.get("provider") == "google"
        and row.get("purpose") == "oauth_client_config"
        and not row.get("revoked")
    )


def _configs(repo: Repository, owner_id: str) -> list[dict]:
    return [row for row in repo.list("credentials", owner_id) if _is_config(row)]


def _trusted_legacy_default(repo: Repository, owner_id: str, row: dict) -> bool:
    default_id = AccountService.default_account_id(owner_id)
    if row.get("account_id") != default_id:
        return False
    if row.get("id") == config_id(owner_id, default_id):
        return False
    account = repo.get("accounts", default_id, owner_id)
    return bool(account and (account.get("legacy_default") is True or account.get("legacy_trusted") is True))


def _effective_config(repo: Repository, owner_id: str, rows: list[dict], account_id: str | None) -> dict | None:
    if account_id is not None:
        exact = [row for row in rows if row.get("account_id") == account_id]
        if exact:
            expected = config_id(owner_id, account_id)
            return next((row for row in exact if row.get("id") == expected), exact[0])
    shared = next((row for row in rows if row.get("account_id") in (None, "")), None)
    if shared:
        return shared
    return next((row for row in rows if _trusted_legacy_default(repo, owner_id, row)), None)


def _public(repo: Repository, owner_id: str, account_id: str | None = None) -> dict:
    row = _effective_config(repo, owner_id, _configs(repo, owner_id), account_id)
    if row:
        return {
            "configured": True,
            "client_id": row.get("client_id"),
            "redirect_uri": callback_uri(),
            "source": "encrypted_database",
        }
    env_id, env_secret = _environment_config()
    return {
        "configured": bool(env_id and env_secret),
        "client_id": env_id or None,
        "redirect_uri": callback_uri(),
        "source": "environment" if env_id or env_secret else "unconfigured",
    }


def _connected(repo: Repository, owner_id: str) -> list[dict]:
    return [
        row
        for row in repo.list("credentials", owner_id)
        if row.get("provider", "google") == "google"
        and row.get("purpose") in {"colab", "drive"}
        and not row.get("revoked")
        and row.get("encrypted_refresh_token")
    ]


def _same_config(row: dict | None, client_id: str, client_secret: str) -> bool:
    if not row:
        env_id, env_secret = _environment_config()
        return env_id == client_id and env_secret == client_secret
    try:
        return row.get("client_id") == client_id and decrypt_secret(row["encrypted_client_secret"]) == client_secret
    except (KeyError, TypeError, ValueError):
        return False


def _dedicated(repo: Repository, owner_id: str, rows: list[dict], account_id: str | None) -> bool:
    identity = account_id or AccountService.default_account_id(owner_id)
    return any(row.get("account_id") == identity and not _trusted_legacy_default(repo, owner_id, row) for row in rows)


def _affected_by_scoped(row: dict, owner_id: str, account_id: str) -> bool:
    token_account = row.get("account_id")
    if token_account == account_id:
        return True
    return account_id == AccountService.default_account_id(owner_id) and token_account in (None, "")


def _invalidate_states_direct(repo: Repository, owner_id: str, account_id: str | None, rows: list[dict], shared: bool) -> None:
    default_id = AccountService.default_account_id(owner_id)
    for state in repo.list("credentials", owner_id):
        if state.get("purpose") != "_oauth_state" or state.get("used_at"):
            continue
        state_account = state.get("account_id")
        if shared:
            affected = not _dedicated(repo, owner_id, rows, state_account)
        else:
            affected = state_account == account_id or (
                account_id == default_id and state_account in (None, "")
            )
        if affected:
            repo.update("credentials", state["id"], {"used_at": GoogleService(repo, owner_id).clock()}, owner_id)


def _invalidate_states(repo: Repository, owner_id: str, account_id: str | None, rows: list[dict], shared: bool) -> None:
    if not shared and account_id is not None:
        GoogleService(repo, owner_id, account_id=account_id).invalidate_oauth_states(owner_id, account_id=account_id)
        return
    _invalidate_states_direct(repo, owner_id, account_id, rows, shared)


@router.get("/auth/google/config")
def get_google_config(
    request: Request,
    account_id: str | None = None,
    user=Depends(current_user),
    st=Depends(get_store),
):
    account_id = _account_id(None, query=account_id)
    repo = Repository(st)
    if account_id is not None:
        AccountService(repo, user["id"]).require(account_id)
    return ok(_public(repo, user["id"], account_id), request)


@router.post("/auth/google/config")
def save_google_config(body: dict, request: Request, user=Depends(current_user), st=Depends(get_store)):
    admin(user)
    client_id, client_secret, account_id = _parse(body)
    repo = Repository(st)
    owner = user["id"]
    if account_id is not None:
        AccountService(repo, owner).require(account_id)
    with st.dispatch_lock() as acquired:
        if not acquired:
            raise APIError("GOOGLE_CONFIG_BUSY", "Google OAuth配置正在保存，请稍后重试", 503, True)
        record_rows = _configs(repo, owner)
        current = _effective_config(repo, owner, record_rows, account_id)
        connected = _connected(repo, owner)
        if account_id is None:
            affected = [row for row in connected if not _dedicated(repo, owner, record_rows, row.get("account_id"))]
            shared = True
        else:
            affected = [row for row in connected if _affected_by_scoped(row, owner, account_id)]
            shared = False
        if affected and not _same_config(current, client_id, client_secret):
            raise APIError("GOOGLE_CONFIG_CONNECTED", "请先断开受影响的Colab或Drive连接后再更换Google客户端", 409)
        data = {
            "provider": "google",
            "purpose": "oauth_client_config",
            "account_id": account_id,
            "client_id": client_id,
            "encrypted_client_secret": encrypt_secret(client_secret),
        }
        record = next((row for row in record_rows if row.get("id") == config_id(owner, account_id)), None)
        if record is None and account_id is None:
            record = next((row for row in record_rows if row.get("account_id") in (None, "")), None)
        if record:
            repo.update("credentials", record["id"], data, owner)
        else:
            repo.create(
                "credentials",
                owner,
                data,
                record_id=config_id(owner, account_id),
            )
        _invalidate_states(repo, owner, account_id, record_rows, shared)
    return ok(_public(repo, owner, account_id), request)


__all__ = ["router", "config_id"]
