"""A small Prometheus registry, written out rather than depended on.

``prometheus_client`` would do this and is a perfectly good library. It is not used
here for the same reason ``app/ledger.py`` writes its SQL out longhand: the
exposition format is about thirty lines of string building, and having it on the
page means the thing being exported is legible instead of being a side effect of
decorators. A dependency is also a thing to keep current forever, and this one
would earn its keep only if the metrics got complicated -- at which point the right
move is to adopt it deliberately rather than to have started there.

The format, in full, is:

    # HELP metric_name some help text
    # TYPE metric_name counter
    metric_name{label="value"} 42

Histograms add cumulative ``_bucket`` series with an ``le`` label, plus ``_sum`` and
``_count``. Buckets must be cumulative and must include ``+Inf``; getting that
wrong produces a scrape that parses and quantiles that lie, which is the one part
of this worth being careful about.

## What is deliberately not here

No labels with unbounded cardinality. Request metrics are labelled by the **route
template** (``/charges/{payment_id}/refund``) and never by the path, because
labelling by path means one series per payment id -- a cardinality explosion that
takes the monitoring system down rather than the service, and does so slowly enough
that nobody connects the two.
"""

import threading
from dataclasses import dataclass, field

#: Seconds. Chosen around what this service actually does: most requests are a
#: handful of indexed queries and land under 25ms, while anything involving the
#: fake processor's injected latency lands in the hundreds. The top buckets exist to
#: make a stall visible rather than to measure it precisely.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)


def _escape(value: str) -> str:
    """Escape a label value. Backslash, quote and newline, per the exposition spec.

    Not optional politeness: a label value containing a newline produces a scrape
    body that parses as different metrics than intended, and label values here
    include things like exception class names.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    rendered = ",".join(f'{name}="{_escape(value)}"' for name, value in labels)
    return "{" + rendered + "}"


@dataclass
class Counter:
    """A number that only goes up, per label set."""

    name: str
    help: str
    label_names: tuple[str, ...] = ()
    _values: dict[tuple[tuple[str, str], ...], float] = field(default_factory=dict)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = tuple(sorted((name, str(labels[name])) for name in self.label_names))
        self._values[key] = self._values.get(key, 0.0) + amount

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        if not self._values:
            # A counter with no observations still declares itself, so a dashboard
            # built against it shows zero rather than "no such metric" -- the two
            # look very different during an incident and only one of them is true.
            lines.append(f"{self.name} 0")
        for key, value in sorted(self._values.items()):
            lines.append(f"{self.name}{_format_labels(key)} {value:g}")
        return lines


@dataclass
class Histogram:
    """Cumulative buckets, a sum and a count, per label set."""

    name: str
    help: str
    label_names: tuple[str, ...] = ()
    buckets: tuple[float, ...] = DEFAULT_BUCKETS
    _counts: dict[tuple[tuple[str, str], ...], list[int]] = field(default_factory=dict)
    _sums: dict[tuple[tuple[str, str], ...], float] = field(default_factory=dict)
    _totals: dict[tuple[tuple[str, str], ...], int] = field(default_factory=dict)

    def observe(self, value: float, **labels: str) -> None:
        key = tuple(sorted((name, str(labels[name])) for name in self.label_names))
        counts = self._counts.setdefault(key, [0] * len(self.buckets))
        for index, bound in enumerate(self.buckets):
            if value <= bound:
                counts[index] += 1
        self._sums[key] = self._sums.get(key, 0.0) + value
        self._totals[key] = self._totals.get(key, 0) + 1

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        for key in sorted(self._counts):
            counts = self._counts[key]
            # Buckets are CUMULATIVE: each one counts every observation at or below
            # its bound, so they must be emitted as a running total. observe()
            # already increments every bucket a value falls under, so these are
            # cumulative by construction.
            for bound, count in zip(self.buckets, counts, strict=True):
                labelled = key + (("le", _format_float(bound)),)
                lines.append(f"{self.name}_bucket{_format_labels(labelled)} {count}")
            total = self._totals[key]
            lines.append(f"{self.name}_bucket{_format_labels(key + (('le', '+Inf'),))} {total}")
            lines.append(f"{self.name}_sum{_format_labels(key)} {self._sums[key]:g}")
            lines.append(f"{self.name}_count{_format_labels(key)} {total}")
        return lines


@dataclass
class Gauge:
    """A number that goes up and down, set rather than accumulated."""

    name: str
    help: str
    _value: float = 0.0

    def set(self, value: float) -> None:
        self._value = value

    def render(self) -> list[str]:
        return [
            f"# HELP {self.name} {self.help}",
            f"# TYPE {self.name} gauge",
            f"{self.name} {self._value:g}",
        ]


def _format_float(value: float) -> str:
    """Render a bucket bound the way Prometheus expects (0.005, not 0.005000)."""
    return f"{value:g}"


# --- The registry -------------------------------------------------------------------

# Module-level and shared, guarded by a lock. The lock is not theatre: metrics are
# mutated from every request, uvicorn runs the event loop on one thread but
# anyio's threadpool does not, and a dict resized during iteration raises in the
# middle of a scrape -- which fails the scrape and looks like the service being
# down.
_lock = threading.Lock()

http_requests_total = Counter(
    name="ledgerline_http_requests_total",
    help="HTTP requests handled, by route template, method and status class.",
    label_names=("route", "method", "status"),
)

http_request_duration_seconds = Histogram(
    name="ledgerline_http_request_duration_seconds",
    help="HTTP request latency by route template and method.",
    label_names=("route", "method"),
)

http_errors_total = Counter(
    name="ledgerline_http_unhandled_errors_total",
    help="Requests that raised an exception the application did not handle.",
    label_names=("route",),
)

rate_limited_total = Counter(
    name="ledgerline_rate_limited_total",
    help="Requests refused with 429 by the in-process rate limiter.",
    label_names=("route",),
)

# The domain gauges. Set from Postgres on each scrape; see app/observability.py.
ledger_imbalance = Gauge(
    name="ledgerline_ledger_imbalance_minor_units",
    help=(
        "Credits minus debits over every ledger entry, in minor units. MUST be 0 -- "
        "a non-zero value means money was written outside the double-entry flows."
    ),
)

outbox_pending = Gauge(
    name="ledgerline_outbox_pending",
    help=(
        "Outbox events written but not yet delivered. Sustained growth means the "
        "publisher is down."
    ),
)

payments_stuck = Gauge(
    name="ledgerline_payments_stuck",
    help=(
        "Payments in 'processing' past the reconcile threshold. Steady state is 0; "
        "a persistent value means the reconciler is not running."
    ),
)

refunds_over_limit = Gauge(
    name="ledgerline_refunds_over_limit",
    help=(
        "Payments whose succeeded refunds exceed the amount charged. Structurally "
        "impossible (migration 0007's trigger), and exported because it is."
    ),
)

_ALL = (
    http_requests_total,
    http_request_duration_seconds,
    http_errors_total,
    rate_limited_total,
    ledger_imbalance,
    outbox_pending,
    payments_stuck,
    refunds_over_limit,
)


def observe_request(*, route: str, method: str, status: int, seconds: float) -> None:
    """Record one handled request."""
    with _lock:
        http_requests_total.inc(route=route, method=method, status=str(status))
        http_request_duration_seconds.observe(seconds, route=route, method=method)


def observe_unhandled_error(*, route: str) -> None:
    with _lock:
        http_errors_total.inc(route=route)


def observe_rate_limited(*, route: str) -> None:
    with _lock:
        rate_limited_total.inc(route=route)


def set_domain_gauges(values: dict[str, int]) -> None:
    """Apply the gauges read out of Postgres."""
    with _lock:
        ledger_imbalance.set(values["ledger_imbalance_minor_units"])
        outbox_pending.set(values["outbox_pending"])
        payments_stuck.set(values["payments_stuck"])
        refunds_over_limit.set(values["refunds_over_limit"])


def render() -> str:
    """The whole registry in Prometheus text exposition format."""
    with _lock:
        lines: list[str] = []
        for metric in _ALL:
            lines.extend(metric.render())
    # A trailing newline is required by the format. Scrapers are forgiving about it
    # and the spec is not.
    return "\n".join(lines) + "\n"


def reset() -> None:
    """Clear every series. For tests only -- a shared registry across tests would
    make each one's assertions depend on which tests ran before it."""
    with _lock:
        for metric in _ALL:
            if isinstance(metric, Counter):
                metric._values.clear()
            elif isinstance(metric, Histogram):
                metric._counts.clear()
                metric._sums.clear()
                metric._totals.clear()
            elif isinstance(metric, Gauge):
                metric.set(0)
