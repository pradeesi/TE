"""
Purpose: Asynchronous database connection, session management, and schema initialization.
Architecture/Context: Foundation for all data persistence across the CRM console and MCP server.
                      In Cloud Run both services share one Cloud SQL for PostgreSQL 16 database via
                      the built-in Cloud SQL unix socket:
                        postgresql+asyncpg://USER:PASS@/DB?host=/cloudsql/PROJECT:REGION:INSTANCE
                      Locally it targets docker-compose Postgres or, if allowed, a SQLite file.
Dependencies/Side Effects:
    - Reads settings.DATABASE_URL and settings.DB_ALLOW_SQLITE_FALLBACK.
    - DB_ALLOW_SQLITE_FALLBACK=true (local dev): an unreachable Postgres or an unpopulated
      '?host=/cloudsql/' socket path silently switches to ./telecom_local.db.
    - DB_ALLOW_SQLITE_FALLBACK=false (cloud): any Postgres failure is logged at ERROR and raised,
      so the MCP server and CRM can never split-brain onto separate stores.
    - Schema creation on PostgreSQL is serialized with a transaction-level advisory lock because
      the MCP server and CRM boot concurrently against the same database.
"""

import asyncio
import logging
import re
from typing import AsyncGenerator, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
)

from src.core.config import settings
from src.db.models import Base

logger = logging.getLogger("hybrid_mcp.database")

SQLITE_FALLBACK_URL = "sqlite+aiosqlite:///./telecom_local.db"
# Arbitrary constant shared by every replica; serializes CREATE TABLE across MCP + CRM boots.
SCHEMA_ADVISORY_LOCK_KEY = 72_310_001
# Cloud SQL sockets can take a few seconds to appear on cold start; retry before giving up.
DB_CONNECT_ATTEMPTS = 5
DB_CONNECT_BACKOFF_SECONDS = 2.0

# Matches a Cloud SQL socket parameter with NO instance name (e.g. '?host=/cloudsql/'), which
# is what an unrendered template produces. A populated '/cloudsql/P:R:I' path does NOT match.
_EMPTY_CLOUDSQL_HOST = re.compile(r"[?&]host=/cloudsql/?(?:&|$)")


class DatabaseConfigurationError(RuntimeError):
    """
    Summary:
        Raised when the configured database cannot be used and SQLite fallback is disabled.

    Parameters:
        message (str): Human-readable reason.

    Return Value:
        N/A (exception class).

    Exceptions/Errors:
        N/A.
    """


class SessionFactoryProxy:
    """
    Summary:
        Dynamic callable proxy for SQLAlchemy async_sessionmaker.
        Ensures all modules importing AsyncSessionLocal transparently route to the active
        engine (PostgreSQL or SQLite fallback) even if reconfigured during application lifespan.

    Parameters:
        None.

    Return Value:
        N/A (class).

    Exceptions/Errors:
        RuntimeError: When invoked before configure().
    """

    def __init__(self) -> None:
        """
        Summary:
            Initializes the proxy without an underlying sessionmaker.

        Parameters:
            None.

        Return Value:
            None.

        Exceptions/Errors:
            None.
        """
        self._sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None

    def configure(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        """
        Summary:
            Updates the underlying sessionmaker reference.

        Parameters:
            sessionmaker (async_sessionmaker[AsyncSession]): Configured SQLAlchemy session factory.

        Return Value:
            None.

        Exceptions/Errors:
            None.
        """
        self._sessionmaker = sessionmaker

    def __call__(self, *args, **kwargs) -> AsyncSession:
        """
        Summary:
            Invokes the active sessionmaker to yield an AsyncSession.

        Parameters:
            *args, **kwargs: Forwarded to the sessionmaker.

        Return Value:
            AsyncSession: Fresh asynchronous SQLAlchemy session.

        Exceptions/Errors:
            RuntimeError: If called before configure() has set an active sessionmaker.
        """
        if self._sessionmaker is None:
            raise RuntimeError("Database session factory has not been initialized.")
        return self._sessionmaker(*args, **kwargs)


# Global singleton proxy instance
AsyncSessionLocal = SessionFactoryProxy()


def _is_sqlite(url: str) -> bool:
    """
    Summary:
        Reports whether a SQLAlchemy URL targets SQLite.

    Parameters:
        url (str): SQLAlchemy database URL.

    Return Value:
        bool: True for sqlite URLs.

    Exceptions/Errors:
        None.
    """
    return url.strip().lower().startswith("sqlite")


def _redact_url(url: str) -> str:
    """
    Summary:
        Hides the password component of a database URL for safe logging.

    Parameters:
        url (str): SQLAlchemy database URL.

    Return Value:
        str: URL with ':<password>@' replaced by ':***@'.

    Exceptions/Errors:
        None.
    """
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url)


def resolve_database_url(url: str, allow_sqlite_fallback: bool) -> str:
    """
    Summary:
        Returns the URL the engine should use, applying the empty-Cloud-SQL-socket shortcut
        only when SQLite fallback is allowed.

    Parameters:
        url (str): Configured DATABASE_URL.
        allow_sqlite_fallback (bool): Value of DB_ALLOW_SQLITE_FALLBACK.

    Return Value:
        str: Effective database URL.

    Exceptions/Errors:
        DatabaseConfigurationError: If the URL is blank, or has an empty Cloud SQL socket path
        while fallback is disabled.
    """
    cleaned = (url or "").strip()
    if not cleaned:
        if allow_sqlite_fallback:
            logger.warning("DATABASE_URL is empty; using local SQLite fallback.")
            return SQLITE_FALLBACK_URL
        raise DatabaseConfigurationError("DATABASE_URL is empty and DB_ALLOW_SQLITE_FALLBACK is false.")

    if _EMPTY_CLOUDSQL_HOST.search(cleaned):
        if allow_sqlite_fallback:
            logger.warning(
                "DATABASE_URL has an unpopulated Cloud SQL socket path; switching to local SQLite fallback."
            )
            return SQLITE_FALLBACK_URL
        raise DatabaseConfigurationError(
            "DATABASE_URL has an unpopulated '/cloudsql/' socket path and DB_ALLOW_SQLITE_FALLBACK is false."
        )
    return cleaned


def _build_engine(url: str) -> AsyncEngine:
    """
    Summary:
        Creates an async SQLAlchemy engine with health-checked pooled connections.

    Parameters:
        url (str): SQLAlchemy database URL.

    Return Value:
        AsyncEngine: Configured engine (no connection is opened yet).

    Exceptions/Errors:
        sqlalchemy.exc.ArgumentError / ImportError: If the URL or driver is invalid.
    """
    if _is_sqlite(url):
        return create_async_engine(url, echo=settings.DEBUG, future=True)
    return create_async_engine(url, echo=settings.DEBUG, future=True, pool_pre_ping=True)


def _configure_sessions(active_engine: AsyncEngine) -> None:
    """
    Summary:
        Points the global session factory proxy at the given engine.

    Parameters:
        active_engine (AsyncEngine): Engine to bind sessions to.

    Return Value:
        None.

    Exceptions/Errors:
        None.
    """
    AsyncSessionLocal.configure(
        async_sessionmaker(
            bind=active_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False
        )
    )


def _initial_engine() -> AsyncEngine:
    """
    Summary:
        Builds the import-time engine from settings, honoring DB_ALLOW_SQLITE_FALLBACK.

    Parameters:
        None.

    Return Value:
        AsyncEngine: Engine for the configured (or fallback) URL.

    Exceptions/Errors:
        DatabaseConfigurationError: If the URL is unusable and fallback is disabled.
    """
    allow_fallback = settings.DB_ALLOW_SQLITE_FALLBACK
    url = resolve_database_url(settings.DATABASE_URL, allow_fallback)
    try:
        return _build_engine(url)
    except Exception as exc:  # noqa: BLE001 - driver/URL errors surface as many exception types
        if not allow_fallback:
            logger.error("Failed to create database engine for %s: %s", _redact_url(url), exc)
            raise DatabaseConfigurationError(f"Cannot create database engine: {exc}") from exc
        logger.warning("Failed to create engine for %s (%s). Using SQLite fallback.", _redact_url(url), exc)
        return _build_engine(SQLITE_FALLBACK_URL)


engine: AsyncEngine = _initial_engine()
_configure_sessions(engine)


def current_dialect_name() -> str:
    """
    Summary:
        Returns the SQL dialect of the active engine ('postgresql' or 'sqlite').

    Parameters:
        None.

    Return Value:
        str: Dialect name.

    Exceptions/Errors:
        None.
    """
    return engine.dialect.name


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Summary:
        FastAPI dependency and context provider yielding an isolated asynchronous database session.

    Parameters:
        None.

    Return Value:
        AsyncGenerator[AsyncSession, None]: Yields one active session, rolled back on error.

    Exceptions/Errors:
        Re-raises any exception raised by the consumer after rolling back.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def _create_schema(active_engine: AsyncEngine) -> None:
    """
    Summary:
        Creates all tables, serialized with a PostgreSQL advisory lock when applicable.

    Parameters:
        active_engine (AsyncEngine): Engine to create the schema on.

    Return Value:
        None.

    Exceptions/Errors:
        sqlalchemy.exc.SQLAlchemyError / OSError: On connectivity or DDL failures.
    """
    async with active_engine.begin() as conn:
        if active_engine.dialect.name == "postgresql":
            # Released automatically at COMMIT; prevents duplicate CREATE TABLE races.
            await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEMA_ADVISORY_LOCK_KEY})
        await conn.run_sync(Base.metadata.create_all)


async def init_db() -> None:
    """
    Summary:
        Initializes database schema and tables. PostgreSQL connections are retried briefly
        (Cloud SQL sockets may lag on cold start). If they still fail, the behaviour depends on
        DB_ALLOW_SQLITE_FALLBACK: switch to SQLite (true) or raise (false).

    Parameters:
        None.

    Return Value:
        None.

    Exceptions/Errors:
        DatabaseConfigurationError: If the primary database is unreachable and fallback is disabled.
    """
    global engine

    if _is_sqlite(str(engine.url)):
        await _create_schema(engine)
        logger.info("SQLite database schema initialized at %s", engine.url.database)
        return

    last_error: Optional[BaseException] = None
    for attempt in range(1, DB_CONNECT_ATTEMPTS + 1):
        try:
            logger.info("Verifying database connectivity and creating schema tables (attempt %d)...", attempt)
            await _create_schema(engine)
            logger.info("Database schema initialized successfully on %s.", engine.dialect.name)
            return
        except Exception as exc:  # noqa: BLE001 - asyncpg/OS/SQLAlchemy errors all mean "not reachable"
            last_error = exc
            logger.warning("Database connection attempt %d/%d failed: %s", attempt, DB_CONNECT_ATTEMPTS, exc)
            if attempt < DB_CONNECT_ATTEMPTS:
                await asyncio.sleep(DB_CONNECT_BACKOFF_SECONDS * attempt)

    if not settings.DB_ALLOW_SQLITE_FALLBACK:
        logger.error(
            "Primary database %s unreachable and DB_ALLOW_SQLITE_FALLBACK=false; refusing to start: %s",
            _redact_url(str(engine.url)), last_error
        )
        raise DatabaseConfigurationError(f"Primary database unreachable: {last_error}") from last_error

    logger.warning("Primary database unreachable (%s). Falling back to local SQLite engine...", last_error)
    await engine.dispose()
    engine = _build_engine(SQLITE_FALLBACK_URL)
    _configure_sessions(engine)
    await _create_schema(engine)
    logger.info("SQLite fallback database initialized at ./telecom_local.db")
