"""Request ids, structured logs, and the numbers worth watching.

Phases 1-6 built a system that is correct. This module is about being able to tell
whether it still is, from outside, without attaching a debugger -- which is a
different property and the one that matters once something is deployed.

## Request ids

Every request gets an id: the caller's ``X-Request-ID`` if they sent one, otherwise
a fresh uuid4. It goes into a :class:`~contextvars.ContextVar`, so every log line
emitted anywhere during that request carries it without a single function having to
pass it down, and it comes back on the response header so a caller can quote it.

That last part is what makes the 500 handler useful rather than merely polite. A
generic "internal error, request 9f3a..." is something a user can put in an email,
and something you can grep for. A stack trace on the wire is neither -- it tells an
attacker about your file layout and tells the user nothing they can act on.

## Structured logs

``LOG_FORMAT=json`` emits one JSON object per line, which is what a log ingester
wants. ``text`` stays the default locally because a person reads those. The
formatter is hand-rolled and about twenty lines: a logging dependency for this would
be a dependency to keep current forever, and the thing it does is ``json.dumps``.

## The metrics, and the one that matters

Standard HTTP counters and a latency histogram, and then four gauges that are about
*this* system rather than about any web service:

``ledgerline_ledger_imbalance_minor_units``
    The SUM over every ledger entry in the database. **It must be zero.** Not
    "should usually be near zero" -- double-entry means credits and debits cancel
    exactly, and Phase 1 has enforced that per-posting since the beginning. This
    gauge is that invariant, for the whole ledger, as one number you can alert on.
    If it is ever non-zero, something wrote money outside every flow that was
    supposed to be the only way to write money, and nothing else in this list
    matters until you know why.

``ledgerline_outbox_pending``
    How far behind the publisher is. Phase 5b guarantees an event exists if and only
    if the money moved; it does not guarantee anyone has been *told* yet. This is
    the lag in that, and a number that climbs without falling means the worker is
    dead.

``ledgerline_payments_stuck``
    Payments in ``processing`` past ``RECONCILE_STUCK_AFTER_SECONDS`` -- the backlog
    the Phase 5a sweep exists to clear. Steady-state zero. A non-zero value that
    persists means the reconciler is not running, which is the failure mode Phase 5a
    explicitly called out as "durability buys the ability to recover; something still
    has to perform the recovery".

``ledgerline_refunds_over_limit``
    Payments whose succeeded refunds exceed what was charged. Migration 0007's
    trigger makes this unreachable, which is exactly why it is worth exporting: a
    number that should be structurally impossible is a cheap and very loud check
    that the structure is still there.

The last three are read from Postgres on scrape rather than incremented in
process, and that is deliberate. A counter maintained in memory tells you what
*this* process thinks it did; a query tells you what is true, survives a restart,
and is the same answer from every machine. Scrapes are infrequent and the queries
are indexed.
"""

import json
import logging
import time
import uuid
from contextvars import ContextVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: The current request's id, or "-" outside a request (a worker, a CLI, a test).
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

REQUEST_ID_HEADER = "X-Request-ID"

# Longest client-supplied request id accepted. A caller controls this value and it
# lands in every log line, so it is bounded and sanitised rather than trusted --
# an unbounded one is a way to write megabytes into your own logs for free.
MAX_REQUEST_ID_LENGTH = 128


def new_request_id(supplied: str | None) -> str:
    """Use the caller's id when it is sane, otherwise mint one.

    Propagating a caller's id is what makes a trace span two services, so it is
    worth honouring -- but only after truncating it and stripping anything that is
    not safely printable. A log line is a string somebody will later grep, paste
    into a terminal, or render in a browser, and a newline in the middle of one is
    how a single request forges log entries that never happened.
    """
    if supplied:
        cleaned = "".join(
            char for char in supplied.strip() if char.isprintable() and char not in "\r\n"
        )
        if cleaned:
            return cleaned[:MAX_REQUEST_ID_LENGTH]
    return str(uuid.uuid4())


class RequestIdFilter(logging.Filter):
    """Attach the current request id to every record, so formatters can use it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. About twenty lines, and worth not taking a dep for.

    ``exc_info`` is rendered into the object rather than appended as a second
    physical line, because a traceback split across lines is a traceback that
    arrives in a log ingester as eight unrelated events.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging(*, log_format: str = "text", level: str = "INFO") -> None:
    """Install one handler on the root logger, replacing whatever was there.

    Replacing rather than adding, because uvicorn installs its own and two handlers
    on one logger is how every line ends up printed twice -- which looks like a
    retry bug for the ten minutes it takes to notice.
    """
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())

    if log_format.strip().lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s [%(request_id)s]: %(message)s"
            )
        )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.strip().upper())

    # uvicorn installs its own handlers on its own loggers before the application
    # starts, so without this its lines bypass the formatter entirely and a
    # "structured logging" deployment emits half-structured logs -- the app's lines
    # as JSON and uvicorn's startup and error lines as plain text, in the same
    # stream. An ingester then drops or mangles every second line.
    #
    # Clearing the handlers and letting the records propagate to root sends them
    # through the formatter above like everything else.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.asgi"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True

    # The access log is the exception: it duplicates the request line this app emits
    # itself, with less in it -- no request id, no route template, no duration. It is
    # silenced rather than reformatted, because two lines per request that disagree
    # about what happened is worse than either alone.
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False


# --- The numbers ------------------------------------------------------------------

# THE invariant, as one number. Credits minus debits over every entry in the
# database. Double-entry means this is zero; if it is not, money was written by
# something that is not one of this project's flows.
_IMBALANCE_SQL = text(
    """
    SELECT COALESCE(
        SUM(CASE direction WHEN 'credit' THEN amount ELSE -amount END), 0
    )::bigint AS imbalance
    FROM ledger_entries
    """
)

_OUTBOX_PENDING_SQL = text(
    "SELECT count(*)::bigint FROM outbox_events WHERE status = 'pending'"
)

# Uses the same partial index the sweep uses (migration 0005), so this stays cheap
# as the payments table grows.
_STUCK_PAYMENTS_SQL = text(
    """
    SELECT count(*)::bigint
    FROM payments
    WHERE status = 'processing'
      AND created_at < now() - (:stuck_after_seconds * interval '1 second')
    """
)

# Should be structurally impossible (migration 0007's trigger). Exported precisely
# because it should be.
_OVER_REFUNDED_SQL = text(
    """
    SELECT count(*)::bigint FROM (
        SELECT r.payment_id
        FROM refunds r
        JOIN payments p ON p.id = r.payment_id
        WHERE r.status = 'succeeded'
        GROUP BY r.payment_id, p.amount
        HAVING SUM(r.amount) > p.amount
    ) breached
    """
)


async def collect_domain_gauges(
    session_factory: async_sessionmaker[AsyncSession], *, stuck_after_seconds: int
) -> dict[str, int]:
    """Read the four domain gauges out of Postgres.

    One session, four indexed counts, on scrape. Read rather than counted in
    process: a counter tells you what this machine thinks it did since it last
    restarted, and every one of these questions is about what is *true right now*,
    which is a different question with a different answer after a deploy.
    """
    async with session_factory() as session:
        imbalance = int((await session.execute(_IMBALANCE_SQL)).scalar_one())
        pending = int((await session.execute(_OUTBOX_PENDING_SQL)).scalar_one())
        stuck = int(
            (
                await session.execute(
                    _STUCK_PAYMENTS_SQL, {"stuck_after_seconds": stuck_after_seconds}
                )
            ).scalar_one()
        )
        over_refunded = int((await session.execute(_OVER_REFUNDED_SQL)).scalar_one())

    return {
        "ledger_imbalance_minor_units": imbalance,
        "outbox_pending": pending,
        "payments_stuck": stuck,
        "refunds_over_limit": over_refunded,
    }
