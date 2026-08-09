"""Shared FastAPI dependencies."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a session; routes drive commit/rollback themselves.

    The posting route needs to roll back explicitly when the balancing invariant
    fails, so this dependency deliberately does not auto-commit. Closing the
    session rolls back anything still open, which is the safe default for a route
    that raised partway through.
    """
    async with async_session() as session:
        yield session
