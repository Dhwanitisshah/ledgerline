"""The HTTP edge: one pure-ASGI middleware doing request ids, limits, logs, metrics.

## Why one, and why not BaseHTTPMiddleware

The obvious shape for this is four small ``BaseHTTPMiddleware`` classes -- an id, a
limiter, an access log, some headers -- and that is how it was first written. It is
also measurably the wrong shape, which the test suite caught before the deploy did.

``BaseHTTPMiddleware`` implements each layer by running the rest of the application
inside an ``anyio`` task group with a memory object stream between the two halves.
That is what lets it expose the tidy ``async def dispatch(request, call_next)``
interface, and it costs a task group, two streams and several context switches
**per layer per request**. Four layers is four of those, and the cost multiplies
under concurrency rather than adding to it.

Phase 4's ``test_the_requests_actually_overlap`` measured the result: twelve
concurrent charges holding a 300ms processor call, which had run in about 0.4s
since Phase 4, started taking 1.5-2.3s against a 1.8s bound. Nothing about the
charge flow had changed. The middleware stack was the whole difference, and a
concurrency harness that is really measuring the middleware is a harness that has
stopped measuring concurrency.

So this is **one** pure-ASGI middleware. It sees ``(scope, receive, send)``, wraps
``send`` to touch the response headers on the way out, and adds no task groups at
all. The four concerns are still four clearly separated blocks below; they are just
not four trips through the ASGI stack.

The ordering they need, which is now sequence rather than nesting:

1. bind a request id -- everything after this logs with it;
2. rate limit -- refuse before any database work, but *after* the id, so a refusal
   is still traceable and still counted;
3. run the app, stamping headers on the response as it starts;
4. log and record metrics, whatever happened, including an exception.

## The 500 handler

An unhandled exception returns a body with the request id and nothing else. No
exception class, no message, no traceback. The full detail goes to the log, keyed
by that same id.

Not paranoia about tracebacks being *interesting* -- it is that a traceback tells
the caller about file paths and library versions while telling them nothing they
can act on, and "quote request 9f3a1c" is something a user can put in an email and
an engineer can grep. Two audiences, two different pieces of information.

The one case that cannot be handled is an exception raised *after* the response has
already started, because the status line is on the wire and cannot be recalled.
That is logged and re-raised, which lets the server close the connection -- the only
honest signal left at that point.
"""

import logging
import time

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import metrics
from app.config import settings
from app.observability import REQUEST_ID_HEADER, new_request_id, request_id_var
from app.ratelimit import FixedWindowLimiter, client_key

logger = logging.getLogger("ledgerline.http")

# One limiter for the process, sized from settings at import. See app/ratelimit.py
# for what "one limiter for the process" does and does not guarantee.
limiter = FixedWindowLimiter(
    limit=settings.RATE_LIMIT_REQUESTS, window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS
)

# A second, stricter budget for requests that CHANGE something. Two tiers because
# reads and writes are not equally expensive or equally dangerous: a GET of a
# balance is one indexed SUM, while a POST /charges takes an advisory lock, commits
# twice, calls the processor and writes an outbox row. One shared budget means
# either the read limit is too tight or the write limit is far too loose, and the
# second mistake is the one that costs money.
#
# Keyed the same way, counted separately: a write consumes from BOTH windows, so a
# client cannot spend its write budget and then continue reading at full rate on a
# machine it is already hammering.
write_limiter = FixedWindowLimiter(
    limit=settings.RATE_LIMIT_WRITE_REQUESTS,
    window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
)

#: Methods that change state. Everything else is a read.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Paths that are never rate limited. A health check refused with a 429 is a
#: platform that concludes the machine is unhealthy and restarts it -- turning a
#: rate limiter into an outage.
UNLIMITED_PATHS = frozenset({"/health", "/ready", "/metrics"})

#: Set on every response. Cheap, and each one closes off a category of mistake:
#:
#: * nosniff -- a browser must not decide a JSON error body is really HTML and
#:   execute it. The one that matters for an API.
#: * CSP default-src 'none' -- nothing loads, because an API response has no
#:   legitimate subresources.
#: * DENY -- no framing, so nothing served from this origin can be clickjacked.
#: * no-referrer -- URLs here contain payment ids, which should not leak into a
#:   third party's referrer logs.
#:
#: Deliberately absent: Strict-Transport-Security. HSTS is a promise about the whole
#: origin that outlives this response, and the application does not know whether TLS
#: terminated upstream. Fly's proxy owns that.
SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
    ("referrer-policy", "no-referrer"),
    ("content-security-policy", "default-src 'none'"),
)


def route_template(scope: Scope) -> str:
    """The matched route pattern, or a placeholder.

    ``/charges/{payment_id}/refund`` rather than ``/charges/8f2a.../refund``. The
    single most important line here for anyone running Prometheus: labelling by raw
    path means one time series per payment id, which does not degrade the service --
    it degrades the monitoring system, slowly, until nobody can query anything, and
    by then the cause is weeks in the past.

    Read *after* the app has run, because that is when Starlette's router puts the
    route into the scope. Falls back to ``<unmatched>`` for 404s rather than to the
    path, so a scanner probing random URLs cannot create series either.
    """
    route = scope.get("route")
    return getattr(route, "path_format", None) or "<unmatched>"


class LedgerlineMiddleware:
    """Request id, rate limit, security headers, access log, metrics, 500 handler."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Lifespan and websocket scopes pass straight through. A middleware that
        # assumes every scope is HTTP breaks startup in a way that looks like the
        # application failing to import.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = new_request_id(_header(scope, REQUEST_ID_HEADER))
        token = request_id_var.set(request_id)
        started = time.perf_counter()

        path: str = scope.get("path", "")
        method: str = scope.get("method", "GET")

        # --- rate limit ---------------------------------------------------------
        # A mutating request is checked against BOTH windows and reports whichever
        # is tighter, so the advertised remaining budget is the one that will
        # actually refuse first rather than the more flattering of the two.
        rate_headers: list[tuple[str, str]] = []
        if settings.RATE_LIMIT_ENABLED and path not in UNLIMITED_PATHS:
            key = client_key(Request(scope))
            allowed, remaining, retry_after = limiter.check(key)
            effective_limit = limiter.limit

            if allowed and method in MUTATING_METHODS:
                w_allowed, w_remaining, w_retry = write_limiter.check(key)
                if not w_allowed:
                    allowed, remaining, retry_after = False, 0, w_retry
                elif w_remaining < remaining:
                    remaining, effective_limit = w_remaining, write_limiter.limit

            if not allowed:
                await self._refuse(
                    scope, receive, send, request_id, retry_after, started, method, path
                )
                request_id_var.reset(token)
                return

            rate_headers = [
                ("x-ratelimit-limit", str(effective_limit)),
                ("x-ratelimit-remaining", str(remaining)),
            ]

        status_code = 500
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
                for name, value in SECURITY_HEADERS:
                    if name not in headers:
                        headers[name] = value
                for name, value in rate_headers:
                    headers[name] = value
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            elapsed = time.perf_counter() - started
            route = route_template(scope)
            # Logged in full, here, once -- with the traceback and the request id.
            logger.exception(
                "unhandled exception on %s %s after %.1fms", method, path, elapsed * 1000
            )
            metrics.observe_unhandled_error(route=route)
            metrics.observe_request(route=route, method=method, status=500, seconds=elapsed)
            request_id_var.reset(token)

            if response_started:
                # The status line is already on the wire; there is no response left
                # to replace. Re-raising lets the server tear the connection down,
                # which is the only honest signal available.
                raise

            await JSONResponse(
                status_code=500,
                content={"detail": "internal server error", "request_id": request_id},
                headers={REQUEST_ID_HEADER: request_id},
            )(scope, receive, send)
            return

        elapsed = time.perf_counter() - started
        route = route_template(scope)
        metrics.observe_request(
            route=route, method=method, status=status_code, seconds=elapsed
        )

        # Health checks log at DEBUG: a platform probing /health every few seconds
        # would otherwise be the overwhelming majority of the log volume, and the
        # signal would be buried under proof that the machine is alive.
        level = logging.DEBUG if path in UNLIMITED_PATHS else logging.INFO
        logger.log(level, "%s %s -> %d in %.1fms", method, path, status_code, elapsed * 1000)

        request_id_var.reset(token)

    async def _refuse(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        request_id: str,
        retry_after: int,
        started: float,
        method: str,
        path: str,
    ) -> None:
        """Answer 429 without ever reaching the router."""
        route = route_template(scope)
        metrics.observe_rate_limited(route=route)
        metrics.observe_request(
            route=route, method=method, status=429, seconds=time.perf_counter() - started
        )
        logger.warning("rate limited %s %s (%s)", method, path, client_key(Request(scope)))

        headers = {
            REQUEST_ID_HEADER: request_id,
            "Retry-After": str(retry_after),
            "X-RateLimit-Limit": str(limiter.limit),
            "X-RateLimit-Remaining": "0",
        }
        headers.update({name: value for name, value in SECURITY_HEADERS})

        await JSONResponse(
            status_code=429,
            content={
                "detail": (
                    f"rate limit exceeded: at most {limiter.limit} requests per "
                    f"{limiter.window_seconds}s. Retry after {retry_after}s."
                ),
                "request_id": request_id,
            },
            headers=headers,
        )(scope, receive, send)


def _header(scope: Scope, name: str) -> str | None:
    """Read one request header out of a raw ASGI scope.

    Scope headers are a list of ``(bytes, bytes)`` with lowercase names, and this is
    called once per request for one header -- cheaper than building a Headers object
    to ask a single question.
    """
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key == wanted:
            return value.decode("latin-1")
    return None
