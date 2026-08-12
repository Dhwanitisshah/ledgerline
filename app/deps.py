"""Shared FastAPI dependencies."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import async_session
from app.processor import FakeProcessor, ProcessorAdapter, ProcessorBooks

# The fake processor's books, bound to the same engine as everything else because
# this project has one database. They are handed the *session factory*, not a
# session, so that every write the processor makes opens its own transaction and
# commits independently of whatever the request is doing. See app/processor.py --
# that independence is the whole of Phase 5a.
processor_books = ProcessorBooks(async_session)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a session; routes drive commit/rollback themselves.

    The posting route needs to roll back explicitly when the balancing invariant
    fails, so this dependency deliberately does not auto-commit. Closing the
    session rolls back anything still open, which is the safe default for a route
    that raised partway through -- and in the charge flow it is what guarantees a
    half-finished charge leaves nothing behind.
    """
    async with async_session() as session:
        yield session


def get_processor() -> ProcessorAdapter:
    """The processor a charge uses unless the request overrides it.

    A dependency rather than a module-level singleton so tests can swap it with
    ``app.dependency_overrides`` and a real adapter can replace it later without
    the route changing.
    """
    return FakeProcessor(
        outcome=settings.PROCESSOR_OUTCOME,
        latency_ms=settings.PROCESSOR_LATENCY_MS,
        books=processor_books,
    )
