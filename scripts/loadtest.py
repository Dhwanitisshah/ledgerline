"""A load test for the charge -> replay -> refund flow.

Run against a server that is already up:

    python scripts/loadtest.py --url http://localhost:8000 --iterations 200 --concurrency 20

## Why a script and not locust or k6

Both are better tools for a real load test and both are a new dependency for a
project whose whole point is that its dependency list is short and deliberate.
This needs an async HTTP client and a percentile function; ``httpx`` is already
here for the test suite and ``statistics`` is in the standard library. Nothing was
added to ``requirements.txt`` to produce the numbers in the README, which also
means anyone who clones the repo can reproduce them with no setup beyond the venv
they already made.

## What one iteration does

Three requests that exercise the phases in order, against one fresh account:

1. ``POST /charges`` with an ``Idempotency-Key`` -- Phases 2, 3, 5a, 5b: a
   two-transaction charge, a processor call, a balanced posting and an outbox row.
2. ``POST /charges`` again, **same key, same body** -- Phase 3: must replay the
   stored response without charging anything. Timed separately, because the
   difference between it and the first is the cost of the work rather than of the
   framework.
3. ``POST /charges/{id}/refund`` -- Phase 6: a reversing posting, under the
   payment's row lock, with the over-refund trigger firing on the insert.

Accounts are created up front and excluded from the timings. Creating one inside
the measured section would put an INSERT nobody cares about into every percentile.

## Reading the output honestly

These numbers describe **this laptop talking to a Postgres container on the same
laptop**, with an in-process fake processor configured for zero latency. They are
a floor for the application's own overhead, not a capacity estimate for a
deployment: a real processor adds a network round trip to every charge, and a
managed database adds one to every query. What they are good for is *relative*
comparison -- the replay against the charge, and this commit against the next one.

The rate limiter will refuse a serious run at its default of 600/minute. Either
raise it or disable it for the run; the script says so rather than reporting a
throughput number that is really a measure of how fast 429s can be served.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
import uuid
from dataclasses import dataclass, field

import httpx

AMOUNT = 250000


@dataclass
class Timings:
    """Durations in milliseconds for one kind of request."""

    name: str
    samples: list[float] = field(default_factory=list)
    errors: int = 0

    def record(self, seconds: float) -> None:
        self.samples.append(seconds * 1000)

    def percentile(self, p: float) -> float:
        """The p-th percentile, nearest-rank.

        Nearest-rank rather than an interpolating method because it always returns
        an observation that actually happened. At these sample sizes an interpolated
        p99 is arithmetic between two points rather than a measurement.
        """
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        index = max(0, min(len(ordered) - 1, round(p / 100 * len(ordered) + 0.5) - 1))
        return ordered[index]

    def summary(self) -> str:
        if not self.samples:
            return f"{self.name:<10} no samples ({self.errors} errors)"
        return (
            f"{self.name:<10} n={len(self.samples):<5} "
            f"p50={self.percentile(50):7.1f}ms  "
            f"p95={self.percentile(95):7.1f}ms  "
            f"p99={self.percentile(99):7.1f}ms  "
            f"max={max(self.samples):7.1f}ms  "
            f"mean={statistics.fmean(self.samples):7.1f}ms"
            + (f"  errors={self.errors}" if self.errors else "")
        )


async def create_accounts(client: httpx.AsyncClient, count: int) -> list[str]:
    """Create the accounts up front, outside the measured section."""
    async def one(index: int) -> str:
        response = await client.post(
            "/accounts", json={"name": f"loadtest-{index}", "currency": "INR"}
        )
        response.raise_for_status()
        return response.json()["id"]

    return list(await asyncio.gather(*(one(i) for i in range(count))))


async def one_iteration(
    client: httpx.AsyncClient,
    account_id: str,
    charge_t: Timings,
    replay_t: Timings,
    refund_t: Timings,
) -> None:
    """charge -> replay -> refund, timing each leg separately."""
    key = str(uuid.uuid4())
    body = {"account_id": account_id, "amount": AMOUNT}

    started = time.perf_counter()
    charge = await client.post("/charges", json=body, headers={"Idempotency-Key": key})
    charge_t.record(time.perf_counter() - started)
    if charge.status_code != 201:
        charge_t.errors += 1
        return
    payment_id = charge.json()["id"]

    # The same key and the same body. This must NOT charge again -- it replays the
    # stored response, so the gap between this and the leg above is the cost of the
    # actual work rather than of FastAPI, the pool, or the network.
    started = time.perf_counter()
    replay = await client.post("/charges", json=body, headers={"Idempotency-Key": key})
    replay_t.record(time.perf_counter() - started)
    if replay.status_code != 201 or replay.json()["id"] != payment_id:
        replay_t.errors += 1

    started = time.perf_counter()
    refund = await client.post(
        f"/charges/{payment_id}/refund",
        json={},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    refund_t.record(time.perf_counter() - started)
    if refund.status_code != 201:
        refund_t.errors += 1


async def run(url: str, iterations: int, concurrency: int) -> int:
    limits = httpx.Limits(
        max_connections=concurrency * 2, max_keepalive_connections=concurrency * 2
    )
    charge_t, replay_t, refund_t = Timings("charge"), Timings("replay"), Timings("refund")

    async with httpx.AsyncClient(base_url=url, timeout=30.0, limits=limits) as client:
        health = await client.get("/health")
        if health.status_code != 200:
            print(f"server at {url} is not healthy ({health.status_code})")
            return 1

        print(f"creating {concurrency} accounts...")
        accounts = await create_accounts(client, concurrency)

        # A semaphore rather than chunked batches: batches make the run only as fast
        # as the slowest request in each batch, which shows up as a bimodal latency
        # distribution that is an artifact of the harness rather than of the server.
        gate = asyncio.Semaphore(concurrency)

        async def worker(index: int) -> None:
            async with gate:
                await one_iteration(
                    client, accounts[index % len(accounts)], charge_t, replay_t, refund_t
                )

        print(f"running {iterations} iterations at concurrency {concurrency}...")
        started = time.perf_counter()
        await asyncio.gather(*(worker(i) for i in range(iterations)))
        elapsed = time.perf_counter() - started

    requests = len(charge_t.samples) + len(replay_t.samples) + len(refund_t.samples)
    errors = charge_t.errors + replay_t.errors + refund_t.errors

    print()
    print(f"  iterations   {iterations} at concurrency {concurrency}")
    print(f"  wall time    {elapsed:.2f}s")
    print(f"  throughput   {iterations / elapsed:.1f} flows/s  ({requests / elapsed:.1f} req/s)")
    print()
    for timing in (charge_t, replay_t, refund_t):
        print("  " + timing.summary())
    print()

    if errors:
        print(f"  {errors} ERRORS -- if these are 429s, the rate limiter refused the run.")
        print("  Re-run with a higher limit:  $env:RATE_LIMIT_REQUESTS=100000")
        return 1

    print("  no errors: every charge replayed to the same payment and every refund posted.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/loadtest.py",
        description="Load test the charge -> replay -> refund flow.",
    )
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=20)
    args = parser.parse_args()

    return asyncio.run(run(args.url, args.iterations, args.concurrency))


if __name__ == "__main__":
    raise SystemExit(main())
