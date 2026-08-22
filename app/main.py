"""The application: routers, the HTTP edge, and the three operational endpoints.

Phase 7 turns this file from four lines into the place where the deployment story
is told. Three things it now owns:

* **Middleware order.** Registered here, and the ordering is load-bearing -- see
  ``app/middleware.py`` for what has to wrap what and why.
* **Liveness versus readiness.** Two endpoints, deliberately different. Conflating
  them is how a database blip turns into a restart loop.
* **Metrics.** ``/metrics``, including the four gauges that are about this system
  rather than about web services in general.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app import metrics
from app.config import settings
from app.db import async_session, engine
from app.middleware import LedgerlineMiddleware
from app.observability import collect_domain_gauges, configure_logging
from app.routers import accounts, charges, refunds, transactions, webhooks, withdrawals

logger = logging.getLogger("ledgerline.app")


DESCRIPTION = """
A payments backend built in phases, where each phase closes a specific failure that
the previous one left standing.

**Money is always integer minor units** (paise, cents). No float, no `Decimal`, at
any layer -- request bodies are `StrictInt`, so `2500.0` is a 422 rather than a
quiet coercion.

**Balances are derived, never stored.** `accounts` has no balance column; a balance
is a SQL `SUM` over `ledger_entries`. The same rule is applied to "how much has been
refunded", which is a `SUM` over `refunds` rather than a column on `payments`.

**The ledger is append-only**, enforced by Postgres triggers rather than by
convention. Corrections are made by posting a reversing entry -- which is exactly
what a refund is.

### Idempotency

`POST /charges` and `POST /charges/{id}/refund` **require** an `Idempotency-Key`
header. Same key and same body replays the original response byte for byte; same key
with a different body is a 422. Keys expire after 24 hours.

### Reliability

A successful charge or refund writes its outbox event in the same transaction as
the ledger postings, so downstream systems are told **if and only if** the money
moved. Delivery is at-least-once with an exactly-once effect, enforced by the
consumer's primary key.

### Operational endpoints

`/health` is liveness and touches no dependency. `/ready` checks the database.
`/metrics` is Prometheus text, and includes `ledgerline_ledger_imbalance_minor_units`
-- the whole double-entry invariant as one number that must be zero.
"""

TAGS_METADATA = [
    {
        "name": "accounts",
        "description": "Accounts, and the derived balance. There is no balance column.",
    },
    {
        "name": "transactions",
        "description": (
            "Raw double-entry postings. Every posting must be single-currency and "
            "must sum to zero; both are checked inside the transaction, against rows "
            "read back from the database, before COMMIT."
        ),
    },
    {
        "name": "charges",
        "description": (
            "The payment lifecycle. A charge that fails moves no money -- not less "
            "money, none. Requires an Idempotency-Key."
        ),
    },
    {
        "name": "refunds",
        "description": (
            "Money going back, as a reversing posting rather than an edit. A partial "
            "refund leaves the payment `succeeded`; only a full one moves it to "
            "`refunded`. Refunds can never exceed the original charge -- enforced by "
            "a database trigger, not just by the route."
        ),
    },
    {
        "name": "withdrawals",
        "description": (
            "The first operation that can refuse. Guarded by a row lock so two "
            "concurrent withdrawals cannot both read the same balance. Not "
            "idempotent, by documented omission."
        ),
    },
    {
        "name": "webhooks",
        "description": (
            "Processor callbacks, deduplicated by the provider's event id. The "
            "payload is not the authority: an event says *when* to settle, and the "
            "processor's own books say *what* the settlement is."
        ),
    },
    {"name": "operations", "description": "Liveness, readiness and metrics."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Configure logging on the way up, dispose the pool on the way down.

    Logging is configured here rather than at import so that the settings are read
    once, at startup, by the process that will actually serve traffic -- and so a
    test importing this module does not reach in and reconfigure the root logger
    underneath pytest.

    Disposing the engine on shutdown matters on a platform that stops machines: a
    pool left open holds Postgres backends until they time out, which on a small
    managed instance is a connection budget spent on processes that have exited.
    """
    configure_logging(log_format=settings.LOG_FORMAT, level=settings.LOG_LEVEL)
    logger.info(
        "ledgerline starting: env=%s test_affordances=%s rate_limit=%s",
        settings.APP_ENV,
        settings.test_affordances_allowed,
        f"{settings.RATE_LIMIT_REQUESTS}/{settings.RATE_LIMIT_WINDOW_SECONDS}s"
        if settings.RATE_LIMIT_ENABLED
        else "disabled",
    )
    yield
    await engine.dispose()


app = FastAPI(
    title="Ledgerline",
    description=DESCRIPTION,
    version="7.0.0",
    openapi_tags=TAGS_METADATA,
    lifespan=lifespan,
    contact={"name": "Ledgerline", "url": "https://github.com/Dhwanitisshah/ledgerline"},
    license_info={"name": "MIT"},
)

# ONE middleware, not four. It was four small BaseHTTPMiddleware classes until
# Phase 4's concurrency harness caught what that costs: each such layer runs the
# rest of the app inside an anyio task group with streams between the halves, and
# four of those took twelve concurrent charges from ~0.4s to ~2s. This is pure ASGI
# and adds no task groups; the four concerns are still four separate blocks inside
# it. See app/middleware.py.
app.add_middleware(LedgerlineMiddleware)

# Off unless configured. This is an API with no browser client, and an allow-list
# that starts permissive is one nobody ever tightens.
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Idempotency-Key", "X-Request-ID"],
    )

app.include_router(accounts.router)
app.include_router(transactions.router)
app.include_router(charges.router)
# A second router on the /charges prefix: a refund is an operation *on a charge*
# and its URL says so, but the charge flow's file is long enough already.
app.include_router(refunds.router)
app.include_router(withdrawals.router)
app.include_router(webhooks.router)


@app.get("/health", tags=["operations"], summary="Liveness -- is the process alive?")
async def health() -> dict[str, str]:
    """Dependency-free on purpose, and that purpose is now load-bearing.

    A platform restarts a machine that fails its liveness check. If this touched
    Postgres, then a database blip -- a failover, a connection limit, a slow query
    -- would fail liveness on every machine at once and the platform would respond
    by restarting all of them, which is the one action guaranteed to make a database
    problem worse.

    Liveness answers "is this process wedged?". Readiness answers "can it serve?".
    They are different questions and they deserve different endpoints; see /ready.
    """
    return {"status": "ok", "env": settings.APP_ENV}


@app.get("/ready", tags=["operations"], summary="Readiness -- can it serve traffic?")
async def ready(response: Response) -> dict[str, object]:
    """Checks the one dependency this service has.

    Returns 503 when the database is unreachable, which tells a load balancer to
    stop sending traffic to this machine **without** telling the platform to restart
    it. That distinction is the entire reason this is a second endpoint.

    ``SELECT 1`` rather than anything cleverer: this is a question about whether a
    connection can be got and a round trip completed, and a readiness probe that
    runs a real query is a readiness probe that fails for reasons unrelated to
    readiness.
    """
    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        logger.exception("readiness check failed")
        response.status_code = 503
        return {"status": "unavailable", "database": "unreachable", "detail": str(exc)[:200]}

    return {"status": "ready", "database": "ok"}


@app.get(
    "/metrics",
    tags=["operations"],
    summary="Prometheus metrics",
    response_class=Response,
)
async def prometheus_metrics() -> Response:
    """HTTP counters and latency, plus four gauges read from Postgres on scrape.

    The gauges are queried here rather than tracked in memory because every one of
    them asks what is *true right now* rather than what this process has seen since
    it started -- and after a deploy those are very different numbers. Four indexed
    counts on a scrape interval is a cost worth paying for an answer that survives a
    restart and is identical from every machine.

    A failure to read them degrades to serving the HTTP metrics alone rather than
    failing the scrape: losing the request rate because the database is briefly busy
    would blind you at precisely the moment the graphs matter.
    """
    try:
        metrics.set_domain_gauges(
            await collect_domain_gauges(
                async_session, stuck_after_seconds=settings.RECONCILE_STUCK_AFTER_SECONDS
            )
        )
    except Exception:
        logger.exception("could not collect domain gauges; serving HTTP metrics only")

    return Response(
        content=metrics.render(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
