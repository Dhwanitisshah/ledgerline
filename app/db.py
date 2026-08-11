"""The engine and session factory.

The pool is sized explicitly rather than left at SQLAlchemy's defaults (5
connections plus 10 overflow). Phase 4's harness fires 50 simultaneous requests,
and with the default pool 35 of them would sit in a Python queue waiting for a
connection -- never reaching Postgres at the same time as each other, and
therefore never exercising the races the harness exists to catch. A concurrency
test throttled by its own client is a test that passes for the wrong reason.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    # Recycle before Postgres or any proxy in between decides a connection has
    # been idle too long; a stale connection surfaces as a mid-transaction failure
    # that looks exactly like a concurrency bug and is not one.
    pool_pre_ping=True,
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
