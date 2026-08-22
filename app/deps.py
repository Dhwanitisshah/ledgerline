"""Shared FastAPI dependencies."""

from collections.abc import AsyncIterator

from fastapi import HTTPException, status
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


def reject_test_affordances_if_disabled(**requested: object) -> None:
    """Refuse a request that drives the fake processor, unless this env allows it.

    The knobs (`force_outcome`, `force_latency_ms`, `force_crash_after_processor`)
    exist so a smoke script can produce a declined charge or a crashed one on
    demand, which is the only way several of this project's guarantees can be shown
    working. That is worth a great deal locally and is a liability in production:
    `force_crash_after_processor` lets an unauthenticated caller strand a payment in
    'processing' at will -- the exact state Phase 5a built a sweep to recover from.

    So they are gated rather than deleted. Gated, because deleting them would take
    the crash reproduction and four of the seven smoke scripts with it, and this
    project's whole method is keeping the broken and the awkward runnable on demand.
    See `Settings.test_affordances_allowed`, which defaults *off* in production by
    deriving from APP_ENV rather than from a flag somebody has to remember to set.

    422 rather than 403: the field is not forbidden to this caller, it is not
    accepted by this deployment. There is nothing to authenticate as.
    """
    if settings.test_affordances_allowed:
        return

    used = sorted(name for name, value in requested.items() if value not in (None, False))
    if not used:
        return

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=(
            f"{', '.join(used)} is not accepted by this deployment "
            f"(APP_ENV={settings.APP_ENV}). These fields drive the fake processor "
            "and exist for local smoke tests; set ALLOW_TEST_AFFORDANCES=true to "
            "enable them on a non-production environment."
        ),
    )
