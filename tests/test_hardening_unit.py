"""Phase 7, the parts that need no database and no event loop.

The second file in the suite that touches no Postgres, after
``tests/test_payment_state.py`` -- and split out for the same reason that one is:
whether a config refuses to load, whether a fixed window counts correctly, and
whether a label value is escaped are decided by pure functions, and testing them
should not require standing up a charge to reach them.

There is also a mechanical reason, learned the hard way while writing this: mixing
synchronous tests into an otherwise-async file corrupts pytest-asyncio's
loop-per-test arrangement, because the shared engine's pooled connections outlive
the loop that created them. The suite's existing convention -- DB-free tests live
in their own file -- is not stylistic.
"""

import json
import logging
import sys

import pytest

from app import metrics
from app.config import LOCAL_DATABASE_URL, Settings
from app.observability import JsonFormatter, RequestIdFilter, new_request_id, request_id_var
from app.ratelimit import FixedWindowLimiter
from app.strategies import ChargeDurability, ClaimStrategy, WithdrawalGuard

#: A production-shaped database URL. Needed because Phase 7 makes production
#: refuse the local compose default, so every APP_ENV=production test must supply
#: one or it fails on a check it was not written to exercise.
PROD_DB = "postgresql+asyncpg://u:p@db.internal:5432/ledgerline"

# --- Configuration refuses to be unsafe ---------------------------------------------


def test_production_defaults_the_knobs_off_without_being_told() -> None:
    """Fail-safe by derivation, not by a flag somebody has to remember.

    An operator who deploys with APP_ENV=production and sets nothing else still gets
    the fake processor's knobs disabled, because the default falls out of the
    environment rather than out of a boolean that defaults to permissive. The
    opposite arrangement ships an open crash endpoint the day someone forgets a line
    of config.
    """
    assert Settings(APP_ENV="production", DATABASE_URL=PROD_DB).test_affordances_allowed is False
    assert Settings(APP_ENV="prod", DATABASE_URL=PROD_DB).test_affordances_allowed is False
    assert Settings(APP_ENV="local").test_affordances_allowed is True

    # And enabling them on a staging box stays possible, and stays explicit.
    assert Settings(
        APP_ENV="production", DATABASE_URL=PROD_DB, ALLOW_TEST_AFFORDANCES=True
    ).test_affordances_allowed


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("IDEMPOTENCY_CLAIM_STRATEGY", ClaimStrategy.NAIVE),
        ("WITHDRAWAL_GUARD", WithdrawalGuard.NAIVE),
        ("CHARGE_DURABILITY", ChargeDurability.SINGLE_TXN),
        ("NAIVE_RACE_WINDOW_MS", 300),
    ],
)
def test_production_refuses_to_start_on_a_deliberately_broken_path(
    field: str, value: object
) -> None:
    """The preserved reproductions are not deployable, and startup says so.

    Phases 4 and 5a keep their naive implementations runnable so the before/after
    stays a measurement rather than a memory. Selecting one in production means
    running code this project documents as wrong -- a double-charge under
    concurrency, or a charge that loses a charged card.

    Failing at startup rather than at the first request is the point: a
    misconfigured deployment must never come up healthy and then be wrong under
    load, because by the time anyone notices, the money has already moved.
    """
    with pytest.raises(Exception) as caught:
        Settings(APP_ENV="production", DATABASE_URL=PROD_DB, **{field: value})

    message = str(caught.value)
    assert "refusing to start" in message
    # Asserted specifically, because Phase 7 added a SECOND "refusing to start"
    # error for a missing DATABASE_URL -- and without a production URL above, this
    # test tripped over that one instead and passed without ever reaching the
    # strategy check it is named for.
    assert field in message or "NAIVE_RACE_WINDOW_MS" in message


def test_the_broken_paths_remain_selectable_outside_production() -> None:
    """The guard must not break the reproductions it is protecting production from."""
    local = Settings(APP_ENV="local", IDEMPOTENCY_CLAIM_STRATEGY=ClaimStrategy.NAIVE)
    assert local.IDEMPOTENCY_CLAIM_STRATEGY is ClaimStrategy.NAIVE


# --- Request ids ---------------------------------------------------------------------


def test_a_hostile_request_id_is_sanitised() -> None:
    """A caller controls this value and it lands in every log line.

    A newline in the middle of one is how a single request forges log entries that
    never happened; an unbounded one is how it writes megabytes into your logs for
    free. Both are cheap to prevent and expensive to discover afterwards.
    """
    forged = new_request_id("real-id\nERROR fake log line claiming something untrue")
    assert "\n" not in forged
    assert "\r" not in forged
    # Kept rather than silently dropped -- just flattened onto one line.
    assert "fake log line" in forged

    assert len(new_request_id("x" * 5000)) <= 128
    assert new_request_id("   ")  # whitespace-only falls back to a generated id
    assert new_request_id(None)
    assert new_request_id("")


def test_log_records_carry_the_current_request_id() -> None:
    """The contextvar reaches the formatter, which is what makes logs greppable."""
    token = request_id_var.set("req-42")
    try:
        record = logging.LogRecord("t", logging.INFO, "f", 1, "hello", None, None)
        RequestIdFilter().filter(record)
        assert record.request_id == "req-42"

        rendered = JsonFormatter().format(record)
        assert json.loads(rendered)["request_id"] == "req-42"
        assert json.loads(rendered)["message"] == "hello"
    finally:
        request_id_var.reset(token)


def test_outside_a_request_the_id_is_a_placeholder() -> None:
    """Workers and CLIs log too, and they have no request to be part of."""
    record = logging.LogRecord("t", logging.INFO, "f", 1, "from a worker", None, None)
    RequestIdFilter().filter(record)
    assert record.request_id == "-"


def test_json_logs_are_one_object_per_line_including_tracebacks() -> None:
    """A traceback split across lines reaches an ingester as N unrelated events."""
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "t", logging.ERROR, "f", 1, "failed", None, sys.exc_info()
        )

    RequestIdFilter().filter(record)
    rendered = JsonFormatter().format(record)

    assert "\n" not in rendered
    parsed = json.loads(rendered)
    assert parsed["level"] == "ERROR"
    assert "ValueError: boom" in parsed["exception"]


# --- The rate limiter -------------------------------------------------------------


def test_the_limiter_refuses_past_the_limit_and_recovers_after_the_window() -> None:
    """Driven with an explicit clock, so the test does not depend on wall time."""
    window = FixedWindowLimiter(limit=3, window_seconds=60)

    assert [window.check("client", now=0.0)[0] for _ in range(3)] == [True, True, True]

    allowed, remaining, retry_after = window.check("client", now=0.0)
    assert allowed is False
    assert remaining == 0
    assert retry_after > 0

    # A different client has its own budget.
    assert window.check("other", now=0.0)[0] is True
    # And the original recovers once the window rolls over.
    assert window.check("client", now=61.0)[0] is True


def test_a_client_hammering_through_a_429_does_not_earn_a_fresh_budget() -> None:
    """Refused requests still count, or the limit is defeated by ignoring it."""
    window = FixedWindowLimiter(limit=2, window_seconds=60)

    for _ in range(2):
        assert window.check("c", now=0.0)[0] is True
    for _ in range(10):
        assert window.check("c", now=0.0)[0] is False

    # Still refused near the end of the window, not reset by the volume of refusals.
    assert window.check("c", now=59.0)[0] is False


def test_remaining_counts_down_and_never_goes_negative() -> None:
    window = FixedWindowLimiter(limit=3, window_seconds=60)
    assert [window.check("c", now=0.0)[1] for _ in range(4)] == [2, 1, 0, 0]


def test_expired_windows_are_pruned() -> None:
    """The dict is bounded by clients seen in one window, not by clients ever seen.

    Pruned lazily during checks rather than by a background task: a sweeper would be
    a second thing to run and supervise, for a dict that is already bounded.
    """
    window = FixedWindowLimiter(limit=5, window_seconds=10)
    for index in range(50):
        window.check(f"client-{index}", now=0.0)
    assert len(window._windows) == 50

    # A check well past the window prunes everything stale.
    window.check("late", now=100.0)
    assert len(window._windows) == 1


# --- The metrics registry -----------------------------------------------------------


def test_histogram_buckets_are_cumulative_and_counted_once() -> None:
    """Cumulative is the part that is easy to get wrong and impossible to spot later.

    A bucket counts every observation at or below its bound, so the series must be
    non-decreasing and ``+Inf`` must equal the total. Get it wrong and the scrape
    still parses -- it just produces quantiles that lie, which is worse than no
    metric at all.
    """
    metrics.reset()
    for value in (0.001, 0.03, 0.4, 30.0):
        metrics.http_request_duration_seconds.observe(value, route="/x", method="GET")

    rendered = "\n".join(metrics.http_request_duration_seconds.render())

    def bucket(bound: str) -> int:
        marker = f'route="/x",le="{bound}"}} '
        line = next(line for line in rendered.split("\n") if marker in line)
        return int(line.rsplit(" ", 1)[1])

    assert bucket("0.005") == 1        # 0.001
    assert bucket("0.05") == 2         # + 0.03
    assert bucket("0.5") == 3          # + 0.4
    assert bucket("10") == 3           # 30.0 exceeds every finite bucket
    assert bucket("+Inf") == 4         # but is still counted in the total

    # Non-decreasing, which is what "cumulative" means.
    counts = [bucket(b) for b in ("0.005", "0.01", "0.025", "0.05", "0.1", "0.5", "10")]
    assert counts == sorted(counts)

    assert 'ledgerline_http_request_duration_seconds_count{method="GET",route="/x"} 4' in rendered
    metrics.reset()


def test_label_values_are_escaped() -> None:
    """A newline in a label value produces a scrape that parses as something else.

    The attack, if it deserves the word: get a newline into a label and the exposed
    text contains what looks like the start of a different metric. The escaping is
    what keeps one series on one line.
    """
    metrics.reset()
    metrics.http_requests_total.inc(
        route='/x"\n# TYPE forged counter', method="GET", status="200"
    )
    rendered = "\n".join(metrics.http_requests_total.render())

    series = [line for line in rendered.split("\n") if not line.startswith("#")]
    # One observation, one line -- the newline did not split it into two.
    assert len(series) == 1
    assert "\\n" in series[0]      # escaped, present, on this line
    assert '\\"' in series[0]      # the quote is escaped too
    metrics.reset()


def test_a_counter_with_no_observations_still_declares_itself() -> None:
    """"No such metric" and "zero" look very different during an incident, and only
    one of them is true of a service that has simply not errored yet."""
    metrics.reset()
    rendered = "\n".join(metrics.http_errors_total.render())
    assert "# TYPE ledgerline_http_unhandled_errors_total counter" in rendered
    assert "ledgerline_http_unhandled_errors_total 0" in rendered


def test_the_exposition_ends_with_a_newline() -> None:
    """Required by the format. Scrapers are forgiving about it and the spec is not."""
    assert metrics.render().endswith("\n")


# --- Fail-fast configuration (Phase 7) -------------------------------------------


def test_a_plain_postgres_url_is_refused_at_startup() -> None:
    """The trap every platform sets, caught at boot instead of at the first query.

    `fly postgres attach` -- and Render, and Railway -- write a plain
    `postgres://...` connection string. SQLAlchemy needs the driver named in the
    scheme, so a deployment that takes the secret verbatim starts cleanly, passes
    liveness, and then fails every query with an error that mentions neither the
    platform nor the missing prefix.
    """
    with pytest.raises(Exception) as caught:
        Settings(DATABASE_URL="postgres://u:p@host:5432/db")

    message = str(caught.value)
    assert "asyncpg driver" in message
    # The message has to say what to DO, not just what is wrong.
    assert "postgresql+asyncpg://" in message


def test_production_refuses_the_local_database_default() -> None:
    """A missing secret must not silently become 'connect to localhost'.

    Inside a container that is not a fallback, it is a connection refused on every
    request, discovered by a customer rather than by the deploy.

    The local default is passed *explicitly*. Letting it arrive via the field
    default made this test depend on the ambient environment: pydantic-settings
    reads DATABASE_URL from the environment ahead of the default, and CI exports it
    on port 5432 (the service mapping) while the compose default is 5433. The guard
    then correctly did not fire, and the test failed in CI while passing on every
    developer machine -- where `.env` happens to hold the compose default and so
    reproduced it by accident. The check under test compares a *value*, so supplying
    that value is what actually exercises it.
    """
    with pytest.raises(Exception) as caught:
        Settings(APP_ENV="production", DATABASE_URL=LOCAL_DATABASE_URL)
    assert "still the local compose default" in str(caught.value)

    # And the default really is that value -- which is what makes the guard above
    # reachable when a deployment supplies no database URL at all. Asserted here so
    # that decoupling the test from the environment does not also stop it covering
    # the "secret was never set" case it is named for.
    assert Settings.model_fields["DATABASE_URL"].default == LOCAL_DATABASE_URL

    # An explicitly configured production URL is fine.
    ok = Settings(
        APP_ENV="production",
        DATABASE_URL="postgresql+asyncpg://u:p@db.internal:5432/ledgerline",
    )
    assert ok.is_production


def test_an_unknown_log_format_is_refused() -> None:
    """A typo here means logs in a shape the ingester drops, discovered during an
    incident when the logs are the only thing you have."""
    with pytest.raises(Exception) as caught:
        Settings(LOG_FORMAT="yaml")
    assert "LOG_FORMAT must be" in str(caught.value)

    assert Settings(LOG_FORMAT="json").LOG_FORMAT == "json"
    assert Settings(LOG_FORMAT="text").LOG_FORMAT == "text"


# --- Business counters -------------------------------------------------------------


def test_charge_and_refund_counters_separate_outcomes() -> None:
    """These count what the SERVICE did, which is not what HTTP did.

    A declined card is a 201 to HTTP and a failure to the business; a replayed
    retry is an HTTP request and not a charge at all. Keeping them apart is the
    reason these exist alongside `http_requests_total`.
    """
    metrics.reset()
    metrics.observe_charge(succeeded=True)
    metrics.observe_charge(succeeded=True)
    metrics.observe_charge(succeeded=False)
    metrics.observe_refund(succeeded=True)

    rendered = metrics.render()
    assert 'ledgerline_charges_total{outcome="succeeded"} 2' in rendered
    assert 'ledgerline_charges_total{outcome="failed"} 1' in rendered
    assert 'ledgerline_refunds_total{outcome="succeeded"} 1' in rendered
    metrics.reset()


def test_the_reconciler_counter_exists_even_before_the_sweep_runs() -> None:
    """Declared at zero rather than absent: "no such metric" and "nothing stranded"
    look identical on a dashboard and only one of them is good news."""
    metrics.reset()
    assert "ledgerline_reconciler_stuck_found_total 0" in metrics.render()
