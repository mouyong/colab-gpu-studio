"""Independent SQLAlchemy Store for Colab GPU Studio.

This database is deliberately separate from Atelier.  ``owner_id`` and
``project_id`` are opaque application identifiers; no Atelier package is
imported here.
"""

from __future__ import annotations

import hashlib
import os
import threading
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import JSON, DateTime, String, create_engine, select, text
from sqlalchemy import delete as sql_delete
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utc_isoformat(value: datetime | None) -> str | None:
    # SQLite drops tzinfo from DateTime columns; all stored timestamps are UTC.
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


_DISPATCH_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"colab-gpu-studio:dispatch").digest()[:8], "big", signed=True
)
_ADMISSION_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"colab-gpu-studio:admission").digest()[:8], "big", signed=True
)
_MEMORY_DISPATCH_LOCKS: dict[str, threading.Lock] = {}
_MEMORY_ADMISSION_LOCKS: dict[str, threading.Lock] = {}
_MEMORY_DISPATCH_LOCKS_GUARD = threading.Lock()


class Base(DeclarativeBase):
    pass


class CommonMixin:
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    state: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    version: Mapped[int] = mapped_column(default=0, nullable=False)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


MODELS: dict[str, type] = {}


def _entity(name: str, table: str):
    return type(name, (CommonMixin, Base), {"__tablename__": table})


# The parent API uses these ordinary tables for its own auth/project/storage
# boundary.  They are independent copies, not Atelier tables.
UserRow = _entity("UserRow", "users")
ProjectRow = _entity("ProjectRow", "projects")
AssetRow = _entity("AssetRow", "assets")
SettingRow = _entity("SettingRow", "settings")
ProviderCredentialRow = _entity("ProviderCredentialRow", "provider_credentials")
MODELS.update(
    {
        "users": UserRow,
        "projects": ProjectRow,
        "assets": AssetRow,
        "settings": SettingRow,
        "provider_credentials": ProviderCredentialRow,
    }
)


class Store:
    MODELS = MODELS

    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get("DATABASE_URL", "sqlite:///./colab-gpu-studio.db")
        if self.url.startswith("sqlite:///") and not self.url.endswith(":memory:"):
            Path(self.url.removeprefix("sqlite:///")).expanduser().parent.mkdir(parents=True, exist_ok=True)
        connect_args = {"check_same_thread": False} if self.url.startswith("sqlite") else {}
        kwargs = {"pool_pre_ping": True}
        if self.url in {"sqlite:///:memory:", "sqlite://"}:
            kwargs["poolclass"] = StaticPool
        self._memory_lock = threading.RLock()
        self._dispatch_state = threading.local()
        self.engine = create_engine(self.url, connect_args=connect_args, **kwargs)
        self.Session = sessionmaker(self.engine, expire_on_commit=False, class_=Session)
        self.create_all()

    def create_all(self) -> None:
        with self.db_lock():
            Base.metadata.create_all(self.engine)

    @contextmanager
    def db_lock(self):
        """Serialize use of StaticPool's single in-memory SQLite connection."""
        if self.engine.dialect.name == "sqlite" and self.engine.url.database in {None, ":memory:"}:
            with self._memory_lock:
                yield
        else:
            with nullcontext():
                yield

    def _model(self, kind: str):
        try:
            return MODELS[kind]
        except KeyError as exc:
            raise ValueError(f"未知实体类型: {kind}") from exc

    @staticmethod
    def _row(row):
        if row is None:
            return None
        reserved = {"id", "owner_id", "project_id", "state", "version", "created_at", "updated_at"}
        result = {
            "id": row.id,
            "owner_id": row.owner_id,
            "project_id": row.project_id,
            "created_at": utc_isoformat(row.created_at),
            "updated_at": utc_isoformat(row.updated_at),
        }
        result.update({key: value for key, value in (row.data or {}).items() if key not in reserved})
        if row.state is not None:
            result["state"] = row.state
        if row.version:
            result["version"] = row.version
        return result

    def create(self, kind, owner_id, data, project_id=None, record_id=None):
        model = self._model(kind)
        if not isinstance(data, dict):
            raise ValueError("data必须是对象")
        payload = dict(data)
        row = model(
            id=record_id or uuid.uuid4().hex,
            owner_id=owner_id,
            project_id=project_id if project_id is not None else payload.get("project_id"),
            state=payload.get("state"),
            data=payload,
            version=0,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        with self.db_lock():
            with self.Session.begin() as session:
                session.add(row)
                session.flush()
        return self._row(row)

    def get(self, kind, id, owner_id=None):
        model = self._model(kind)
        with self.db_lock():
            with self.Session() as session:
                stmt = select(model).where(model.id == id)
                if owner_id is not None:
                    stmt = stmt.where(model.owner_id == owner_id)
                return self._row(session.scalar(stmt))

    def list(self, kind, owner_id=None, project_id=None):
        model = self._model(kind)
        with self.db_lock():
            with self.Session() as session:
                stmt = select(model).order_by(model.created_at.desc())
                if owner_id is not None:
                    stmt = stmt.where(model.owner_id == owner_id)
                if project_id is not None:
                    stmt = stmt.where(model.project_id == project_id)
                return [self._row(row) for row in session.scalars(stmt).all()]

    def update(self, kind, id, patch, owner_id=None):
        model = self._model(kind)
        with self.db_lock():
            with self.Session.begin() as session:
                stmt = select(model).where(model.id == id)
                if owner_id is not None:
                    stmt = stmt.where(model.owner_id == owner_id)
                row = session.scalar(stmt)
                if row is None:
                    raise KeyError(id)
                row.data = {**(row.data or {}), **dict(patch)}
                if "state" in patch:
                    row.state = patch["state"]
                if "project_id" in patch:
                    row.project_id = patch["project_id"]
                row.version += 1
                row.updated_at = utcnow()
                session.flush()
                return self._row(row)

    def delete(self, kind, id, owner_id=None):
        model = self._model(kind)
        with self.db_lock():
            with self.Session.begin() as session:
                stmt = sql_delete(model).where(model.id == id)
                if owner_id is not None:
                    stmt = stmt.where(model.owner_id == owner_id)
                if not session.execute(stmt).rowcount:
                    raise KeyError(id)
        return True

    def _sqlite_dispatch_lock_path(self) -> Path | None:
        if self.engine.dialect.name != "sqlite":
            return None
        database = self.engine.url.database
        if not database or database == ":memory:":
            return None
        path = Path(database).expanduser().resolve()
        return path.with_name(path.name + ".dispatch.lock")

    def _sqlite_admission_lock_path(self) -> Path | None:
        if self.engine.dialect.name != "sqlite":
            return None
        database = self.engine.url.database
        if not database or database == ":memory:":
            return None
        path = Path(database).expanduser().resolve()
        return path.with_name(path.name + ".admission.lock")

    def _dispatch_enter(self) -> None:
        self._dispatch_state.depth = getattr(self._dispatch_state, "depth", 0) + 1

    def _dispatch_exit(self) -> None:
        depth = getattr(self._dispatch_state, "depth", 0)
        self._dispatch_state.depth = max(0, depth - 1)

    def _dispatch_held_by_current_thread(self) -> bool:
        return bool(getattr(self._dispatch_state, "depth", 0))

    @contextmanager
    def upload_lock(self, identity):
        """Non-blocking, database-scoped upload coordination; released on crash."""
        key = hashlib.sha256(identity.encode()).hexdigest()
        if self.engine.dialect.name == 'postgresql':
            lock_key = int.from_bytes(bytes.fromhex(key)[:8], 'big', signed=True)
            with self.engine.connect() as connection:
                acquired = bool(connection.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': lock_key}).scalar())
                try:
                    yield acquired
                finally:
                    if acquired:
                        connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': lock_key})
            return
        if self.engine.dialect.name != 'sqlite':
            raise RuntimeError('unsupported upload lock dialect')
        path = self._sqlite_dispatch_lock_path()
        if path is None:
            with _MEMORY_DISPATCH_LOCKS_GUARD:
                lock = _MEMORY_DISPATCH_LOCKS.setdefault(f'upload:{id(self.engine)}:{key}', threading.Lock())
            acquired = lock.acquire(blocking=False)
            try:
                yield acquired
            finally:
                if acquired:
                    lock.release()
            return
        from .filelocks import file_lock

        path = path.with_name(path.name + '.upload.' + key)
        with path.open('a+') as handle:
            with file_lock(handle, blocking=False) as acquired:
                yield acquired

    @contextmanager
    def admission_lock(self, timeout=1.0):
        """Serialize admissions without waiting on a long scheduler snapshot.

        ``dispatch_lock`` is also used around scheduler/watchdog list scans.  A
        large historical job snapshot can therefore outlive the HTTP timeout,
        even though admission only needs the database transaction to be
        atomic.  Admissions always use the separate cross-process lock, even
        when a caller already holds ``dispatch_lock``.
        """
        import time

        deadline = time.monotonic() + timeout
        while True:
            with self._admission_try_lock() as acquired:
                if acquired:
                    yield True
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                yield False
                return
            time.sleep(min(0.01, remaining))

    @contextmanager
    def _admission_try_lock(self):
        """Try the admission-only lock once, without touching dispatch state."""
        if self.engine.dialect.name == "postgresql":
            connection = self.engine.connect()
            acquired = False
            try:
                acquired = bool(
                    connection.execute(
                        text("SELECT pg_try_advisory_lock(:lock_key)"),
                        {"lock_key": _ADMISSION_LOCK_KEY},
                    ).scalar()
                )
                yield acquired
            finally:
                try:
                    if acquired:
                        connection.execute(
                            text("SELECT pg_advisory_unlock(:lock_key)"),
                            {"lock_key": _ADMISSION_LOCK_KEY},
                        )
                finally:
                    connection.close()
            return

        lock_path = self._sqlite_admission_lock_path()
        if lock_path is None and self.engine.dialect.name == "sqlite":
            with _MEMORY_DISPATCH_LOCKS_GUARD:
                lock = _MEMORY_ADMISSION_LOCKS.setdefault(self.url, threading.Lock())
            acquired = lock.acquire(blocking=False)
            try:
                yield acquired
            finally:
                if acquired:
                    lock.release()
            return
        if lock_path is not None:
            from .filelocks import file_lock

            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+") as lock_file:
                with file_lock(lock_file, blocking=False) as acquired:
                    yield acquired
            return
        raise RuntimeError(f"不支持的数据库准入锁方言: {self.engine.dialect.name}")

    @contextmanager
    def dispatch_lock(self):
        """Non-blocking cross-process lock for short scheduler ticks."""
        if self.engine.dialect.name == "postgresql":
            connection = self.engine.connect()
            acquired = False
            try:
                acquired = bool(
                    connection.execute(
                        text("SELECT pg_try_advisory_lock(:lock_key)"),
                        {"lock_key": _DISPATCH_LOCK_KEY},
                    ).scalar()
                )
                if acquired:
                    self._dispatch_enter()
                yield acquired
            finally:
                try:
                    if acquired:
                        connection.execute(
                            text("SELECT pg_advisory_unlock(:lock_key)"),
                            {"lock_key": _DISPATCH_LOCK_KEY},
                        )
                finally:
                    if acquired:
                        self._dispatch_exit()
                    connection.close()
            return

        lock_path = self._sqlite_dispatch_lock_path()
        if lock_path is None and self.engine.dialect.name == "sqlite":
            with _MEMORY_DISPATCH_LOCKS_GUARD:
                lock = _MEMORY_DISPATCH_LOCKS.setdefault(self.url, threading.Lock())
            acquired = lock.acquire(blocking=False)
            try:
                if acquired:
                    self._dispatch_enter()
                yield acquired
            finally:
                if acquired:
                    self._dispatch_exit()
                    lock.release()
            return
        if lock_path is not None:
            from .filelocks import file_lock

            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+") as lock_file:
                with file_lock(lock_file, blocking=False) as acquired:
                    if not acquired:
                        yield False
                        return
                    try:
                        self._dispatch_enter()
                        yield True
                    finally:
                        self._dispatch_exit()
            return
        raise RuntimeError(f"不支持的数据库调度锁方言: {self.engine.dialect.name}")
