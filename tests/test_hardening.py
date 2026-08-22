"""Phase 7: the things that only matter once this is reachable from the internet.

Phases 1-6 asked "is it correct?". These ask "is it safe to expose, and can you
tell what it is doing from outside?" -- which are different questions with
different failure modes. A system can be perfectly correct and still hand a
stranger the ability to strand payments, or leak its own file paths in an error, or
be impossible to diagnose because every log line is anonymous.

The first section is the one that would matter most if it were wrong: the fake
processor's knobs must not be reachable on a production deployment.
"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app import metrics
from app.config import settings
from app.middleware import limiter
from app.observability import REQUEST_ID_HEADER
from tests.conftest import create_account, create_charge, post_charge, refund_charge

AMOUNT = 250000


# --- The knobs are not reachable in production ---------------------------------------


async def test_the_crash_knob_is_refused_when_affordances_are_disabled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE ONE THAT MATTERS: a stranger cannot strand a payment on demand.

    ``force_crash_after_processor`` abandons a charge at the fatal instant, leaving
    a payment in 'processing' for the sweep. That is indispensable locally -- it is
    how Phase 5a's entire guarantee is demonstrated -- and on a public URL it is an
    unauthenticated denial of correctness: anyone with curl can manufacture the
    exact state the reconciler exists to clean up, as fast as they can send requests.

    Gated rather than deleted, so the reproduction survives. See
    ``Settings.test_affordances_allowed``, which derives from APP_ENV so that a
    deployment which forgets to set anything still refuses.
    """
    monkeypatch.setattr(settings, "ALLOW_TEST_AFFORDANCES", False)

    account = await create_account(client, "Customer")
    response = await post_charge(client, account, AMOUNT, force_crash_after_processor=True)

    assert response.status_code == 422, response.text
    assert "force_crash_after_processor" in response.json()["detail"]

    # Refused before anything was written -- including the idempotency key, so the
    # caller has not burned it on a request that never ran.
    from tests.conftest import count_rows

    assert await count_rows("payments") == 0
    assert await count_rows("idempotency_keys") == 0


async def test_the_processor_knobs_are_refused_on_charges_and_refunds(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both endpoints that accept them, gated by the same helper."""
    account = await create_account(client, "Customer")
    charge = await create_charge(client, account, AMOUNT)

    monkeypatch.setattr(settings, "ALLOW_TEST_AFFORDANCES", False)

    declined = await post_charge(client, account, AMOUNT, force_outcome="failure")
    assert declined.status_code == 422, declined.text
    assert "force_outcome" in declined.json()["detail"]

    slow = await post_charge(client, account, AMOUNT, force_latency_ms=50)
    assert slow.status_code == 422, slow.text

    refund = await refund_charge(client, charge["id"], 1000, force_outcome="failure")
    assert refund.status_code == 422, refund.text
    assert "force_outcome" in refund.json()["detail"]


async def test_ordinary_requests_are_unaffected_by_the_gate(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate fires on the fields, not on the endpoint.

    A charge that does not ask for anything unusual behaves identically whether the
    affordances are enabled or not -- which is the whole point. A gate that changed
    the normal path would be a gate that makes production behave differently from
    every test written against it.
    """
    monkeypatch.setattr(settings, "ALLOW_TEST_AFFORDANCES", False)

    account = await create_account(client, "Customer")
    charge = await create_charge(client, account, AMOUNT)
    assert charge["status"] == "succeeded"

    refund = await refund_charge(client, charge["id"])
    assert refund.status_code == 201, refund.text


# --- Request ids ------------------------------------------------------------------


async def test_every_response_carries_a_request_id(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.headers.get(REQUEST_ID_HEADER)


async def test_a_supplied_request_id_is_propagated(client: AsyncClient) -> None:
    """A caller's id is honoured, so a trace can span two services."""
    response = await client.get("/health", headers={REQUEST_ID_HEADER: "trace-abc-123"})
    assert response.headers[REQUEST_ID_HEADER] == "trace-abc-123"


# --- The 500 handler ----------------------------------------------------------------


async def test_an_unhandled_error_returns_an_id_and_no_internals(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A traceback tells an attacker about your file layout and the user nothing.

    The response carries the request id and a generic message; the traceback goes to
    the log under that same id. Two audiences, two different pieces of information.
    """

    async def explode(*args: object, **kwargs: object):
        raise RuntimeError("a secret internal detail nobody should see")

    monkeypatch.setattr("app.routers.charges._complete", explode)

    account = await create_account(client, "Customer")
    response = await post_charge(client, account, AMOUNT)

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "internal server error"
    assert body["request_id"]
    assert "secret internal detail" not in response.text
    assert "Traceback" not in response.text
    assert "app/routers" not in response.text


# --- Rate limiting -------------------------------------------------------------------


async def test_the_endpoint_returns_429_with_retry_after(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wire behaviour: 429, Retry-After, and a counted metric."""
    monkeypatch.setattr(limiter, "limit", 2)
    limiter.reset()

    await client.get("/accounts/00000000-0000-0000-0000-000000000000/balance")
    await client.get("/accounts/00000000-0000-0000-0000-000000000000/balance")
    limited = await client.get("/accounts/00000000-0000-0000-0000-000000000000/balance")

    assert limited.status_code == 429, limited.text
    assert limited.headers["Retry-After"]
    assert limited.headers["X-RateLimit-Limit"] == "2"
    assert "rate limit exceeded" in limited.json()["detail"]
    assert "ledgerline_rate_limited_total" in metrics.render()


async def test_health_and_metrics_are_never_rate_limited(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 on a health check is a platform restarting a machine that was fine.

    Rate limiting the liveness probe turns a rate limiter into an outage, which is a
    genuinely spectacular own goal and an easy one to score.
    """
    monkeypatch.setattr(limiter, "limit", 1)
    limiter.reset()

    for _ in range(5):
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/ready")).status_code == 200


# --- Security headers -----------------------------------------------------------------


async def test_security_headers_are_present(client: AsyncClient) -> None:
    """nosniff is the one that matters for a JSON API: a browser must never decide
    an error body is really HTML and execute it."""
    response = await client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]


# --- Liveness, readiness, metrics --------------------------------------------------------


async def test_health_touches_no_dependency_and_ready_does(client: AsyncClient) -> None:
    """Two endpoints because they answer two questions with different consequences.

    Liveness failing means "restart me". Readiness failing means "stop sending me
    traffic". If /health checked Postgres, a database blip would fail liveness on
    every machine simultaneously and the platform would restart all of them -- the
    one response guaranteed to make a database problem worse.
    """
    health = await client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    ready = await client.get("/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "database": "ok"}


async def test_metrics_exposes_http_series_in_prometheus_format(
    client: AsyncClient,
) -> None:
    await create_account(client, "Customer")

    body = (await client.get("/metrics")).text

    assert "# HELP ledgerline_http_requests_total" in body
    assert "# TYPE ledgerline_http_requests_total counter" in body
    assert "# TYPE ledgerline_http_request_duration_seconds histogram" in body
    # Cumulative buckets must include +Inf, or the quantiles lie.
    assert 'le="+Inf"' in body
    assert body.endswith("\n")


async def test_metrics_are_labelled_by_route_template_not_by_path(
    client: AsyncClient,
) -> None:
    """The single most important line for anyone running Prometheus against this.

    Labelling by raw path means one time series per payment id. That does not
    degrade the service -- it degrades the monitoring system, slowly, until nobody
    can query anything, and by then the cause is weeks in the past.
    """
    account = await create_account(client, "Customer")
    charge = await create_charge(client, account, AMOUNT)
    await client.get(f"/charges/{charge['id']}")

    body = (await client.get("/metrics")).text

    assert 'route="/charges/{payment_id}"' in body
    assert charge["id"] not in body


async def test_the_ledger_imbalance_gauge_is_zero_and_is_exported(
    client: AsyncClient,
) -> None:
    """THE CAPSTONE METRIC: the whole double-entry invariant as one number.

    Credits minus debits over every entry in the database. It must be zero. Not
    "usually near zero" -- Phase 1 has enforced per-posting balance since the
    beginning, so the total is exactly zero or something wrote money outside every
    flow that was supposed to be the only way to write money.

    Exported so it can be alerted on, which is the difference between an invariant
    that holds and an invariant you would find out had stopped holding.
    """
    account = await create_account(client, "Customer")
    charge = await create_charge(client, account, AMOUNT)
    await refund_charge(client, charge["id"], 100000)

    body = (await client.get("/metrics")).text

    assert "ledgerline_ledger_imbalance_minor_units 0" in body
    assert "MUST be 0" in body  # the HELP text says so to whoever reads the scrape


async def test_the_domain_gauges_report_real_state(client: AsyncClient) -> None:
    """Outbox depth and refund breaches, read from Postgres rather than counted."""
    account = await create_account(client, "Customer")
    await create_charge(client, account, AMOUNT)

    body = (await client.get("/metrics")).text

    # One unpublished event from the charge above.
    assert "ledgerline_outbox_pending 1" in body
    # Structurally impossible, and exported precisely because it is.
    assert "ledgerline_refunds_over_limit 0" in body
    assert "ledgerline_payments_stuck 0" in body


async def test_metrics_still_serve_http_series_when_the_gauges_cannot_be_read(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Losing the request rate because the database is briefly busy would blind you
    at exactly the moment the graphs matter."""

    async def unavailable(*args: object, **kwargs: object):
        raise RuntimeError("database is busy")

    monkeypatch.setattr("app.main.collect_domain_gauges", unavailable)

    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "ledgerline_http_requests_total" in response.text


# --- The metrics registry itself ----------------------------------------------------


async def test_concurrent_requests_do_not_corrupt_the_registry(
    client: AsyncClient,
) -> None:
    """The scrape reads a dict that every request writes to.

    Without the lock, a dict resized during iteration raises inside the scrape --
    which fails the scrape and looks exactly like the service being down, at the
    moment you are looking at graphs to find out whether it is.
    """
    from app.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as parallel:
        await asyncio.gather(*[parallel.get("/health") for _ in range(40)])

    body = (await client.get("/metrics")).text
    assert "ledgerline_http_requests_total" in body


# --- The stricter write tier (Phase 7) ---------------------------------------------


async def test_mutating_requests_have_a_tighter_budget_than_reads(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two tiers, because a POST /charges and a GET of a balance are not equally
    expensive or equally dangerous.

    A read is one indexed SUM. A charge takes an advisory lock, commits twice,
    calls the processor and writes an outbox row. One shared budget means either
    the read limit is too tight or the write limit is far too loose, and the second
    mistake is the one that costs money.
    """
    from app.middleware import write_limiter

    account = await create_account(client, "Customer")

    # Reset AFTER the setup write, so the budget below is spent only by the
    # requests this test is actually about.
    monkeypatch.setattr(limiter, "limit", 1000)
    monkeypatch.setattr(write_limiter, "limit", 1)
    limiter.reset()
    write_limiter.reset()

    first = await post_charge(client, account, AMOUNT)
    assert first.status_code == 201, first.text

    second = await post_charge(client, account, AMOUNT)
    assert second.status_code == 429, second.text
    assert second.headers["Retry-After"]

    # Reads are untouched: the write budget is exhausted, the read budget is not.
    balance = await client.get(f"/accounts/{account}/balance")
    assert balance.status_code == 200, balance.text


async def test_the_advertised_remaining_budget_is_the_tighter_one(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A header that reports the more flattering of two limits is a header that
    lies to a client trying to pace itself."""
    from app.middleware import write_limiter

    monkeypatch.setattr(limiter, "limit", 1000)
    monkeypatch.setattr(write_limiter, "limit", 10)
    limiter.reset()
    write_limiter.reset()

    created = await client.post("/accounts", json={"name": "x", "currency": "INR"})
    assert created.status_code == 201
    # The write tier is the binding constraint, so that is what is advertised.
    assert created.headers["X-RateLimit-Limit"] == "10"


async def test_business_counters_move_on_real_charges_and_refunds(
    client: AsyncClient,
) -> None:
    """End to end: the counters reflect the processor's answers, not HTTP status.

    The declined charge is a 201 and counts as a FAILURE; the replay is a 201 and
    counts as nothing at all, because no card was touched.
    """
    account = await create_account(client, "Customer")
    charge = await create_charge(client, account, AMOUNT, key="counters")
    await post_charge(client, account, AMOUNT, key="counters")          # replay
    await create_charge(client, account, AMOUNT, force_outcome="failure")
    await refund_charge(client, charge["id"], 1000)

    body = (await client.get("/metrics")).text

    # Two charges reached the processor: one succeeded, one was declined. The
    # replay reached nothing.
    assert 'ledgerline_charges_total{outcome="succeeded"} 1' in body
    assert 'ledgerline_charges_total{outcome="failed"} 1' in body
    assert 'ledgerline_refunds_total{outcome="succeeded"} 1' in body
