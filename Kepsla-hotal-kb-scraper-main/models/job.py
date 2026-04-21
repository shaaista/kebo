"""Database models for scrape job tracking."""

from __future__ import annotations

import enum
import logging
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text, event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from config.settings import settings

Base = declarative_base()
logger = logging.getLogger(__name__)


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    DISCOVERING = "discovering"
    CRAWLING = "crawling"
    PROPERTIES_DETECTED = "properties_detected"
    EXTRACTING = "extracting"
    DOWNLOADING_IMAGES = "downloading_images"
    GENERATING = "generating"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


class ScrapeJob(Base):
    """Tracks a single scrape-to-KB generation job."""

    __tablename__ = "scrape_jobs"

    # Identity — UUID stored as 36-char string e.g. "550e8400-e29b-41d4-a716-446655440000"
    id  = Column(String(36), primary_key=True)
    url = Column(Text, nullable=False)
    status = Column(String(30), default=JobStatus.PENDING.value)

    # Progress tracking
    progress_pct     = Column(Integer, default=0)
    progress_msg     = Column(String(500), default="Queued...")
    pages_found      = Column(Integer, default=0)
    pages_crawled    = Column(Integer, default=0)
    pages_failed     = Column(Integer, default=0)
    properties_found = Column(Integer, default=0)

    # Review and publish payloads (large JSON blobs — use LONGTEXT on MySQL)
    properties_data  = Column(Text(length=4294967295), default="")
    review_data      = Column(Text(length=4294967295), default="")
    job_context_data = Column(Text(length=4294967295), default="")

    # Background queue metadata
    queue_state         = Column(String(20), default="idle")
    task_type           = Column(String(50), default="")
    task_payload        = Column(Text(length=4294967295), default="")
    queue_attempts      = Column(Integer, default=0)
    max_attempts        = Column(Integer, default=1)
    next_retry_at       = Column(DateTime, nullable=True)
    worker_id           = Column(String(100), default="")
    worker_started_at   = Column(DateTime, nullable=True)
    worker_heartbeat_at = Column(DateTime, nullable=True)

    # Output
    output_dir  = Column(Text, default="")
    kb_preview  = Column(Text(length=4294967295), default="")

    # Error
    error_message = Column(Text(length=4294967295), default="")

    # Timestamps
    created_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Engine — picks the right async driver based on DATABASE_URL:
#   MySQL:  mysql+aiomysql://user:pass@host:3306/dbname
#   SQLite: sqlite+aiosqlite:///./scraper.db  (local dev)
# ---------------------------------------------------------------------------

_engine_kwargs: dict[str, object] = {"echo": False}

if settings.database_url.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"timeout": 30}

if settings.database_url.startswith("mysql"):
    # MySQL connection pool settings for production
    _engine_kwargs["pool_size"] = 10
    _engine_kwargs["max_overflow"] = 20
    _engine_kwargs["pool_pre_ping"] = True   # detect stale connections
    _engine_kwargs["pool_recycle"] = 3600    # recycle connections every hour

engine = create_async_engine(settings.database_url, **_engine_kwargs)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# SQLite-only optimisations (skipped when using MySQL)
# ---------------------------------------------------------------------------

if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
        """Reduce writer contention for SQLite-backed local runs."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


def _migrate_sqlite_schema(sync_conn) -> None:
    """Apply lightweight schema upgrades for local SQLite databases only.

    On MySQL the company DB schema is managed externally via the CREATE TABLE
    script — this function is a no-op for any non-SQLite dialect.
    """
    if sync_conn.dialect.name != "sqlite":
        return

    inspector = inspect(sync_conn)
    if "scrape_jobs" not in inspector.get_table_names():
        return

    existing_columns = {column["name"] for column in inspector.get_columns("scrape_jobs")}

    migrations = {
        "properties_data":  "ALTER TABLE scrape_jobs ADD COLUMN properties_data TEXT DEFAULT ''",
        "review_data":      "ALTER TABLE scrape_jobs ADD COLUMN review_data TEXT DEFAULT ''",
        "job_context_data": "ALTER TABLE scrape_jobs ADD COLUMN job_context_data TEXT DEFAULT ''",
        "queue_state":      "ALTER TABLE scrape_jobs ADD COLUMN queue_state TEXT DEFAULT 'idle'",
        "task_type":        "ALTER TABLE scrape_jobs ADD COLUMN task_type TEXT DEFAULT ''",
        "task_payload":     "ALTER TABLE scrape_jobs ADD COLUMN task_payload TEXT DEFAULT ''",
        "queue_attempts":   "ALTER TABLE scrape_jobs ADD COLUMN queue_attempts INTEGER DEFAULT 0",
        "max_attempts":     "ALTER TABLE scrape_jobs ADD COLUMN max_attempts INTEGER DEFAULT 1",
        "next_retry_at":    "ALTER TABLE scrape_jobs ADD COLUMN next_retry_at DATETIME",
        "worker_id":        "ALTER TABLE scrape_jobs ADD COLUMN worker_id TEXT DEFAULT ''",
        "worker_started_at":   "ALTER TABLE scrape_jobs ADD COLUMN worker_started_at DATETIME",
        "worker_heartbeat_at": "ALTER TABLE scrape_jobs ADD COLUMN worker_heartbeat_at DATETIME",
    }

    for column_name, ddl in migrations.items():
        if column_name in existing_columns:
            continue
        sync_conn.execute(text(ddl))
        logger.info("Applied SQLite schema migration: added scrape_jobs.%s", column_name)


async def init_db() -> None:
    """Create tables if they don't exist.

    - SQLite (local dev): also runs lightweight column migration.
    - MySQL (production): assumes table already created via the DDL script.
      create_all() is safe to call — it skips existing tables.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_sqlite_schema)


async def get_session() -> AsyncSession:
    """Yield a DB session for dependency injection."""
    async with async_session() as session:
        yield session
