"""A fixed-window rate limiter, in process, with its limits stated.

## What this is for

`POST /webhooks` is a public endpoint with no signature verification (carried
forward from Phase 5b, and still true). Phase 5b's design keeps the blast radius
small -- the payload is not the authority, so a forged event can at worst cause a
processor lookup -- but "cannot move money" is not the same as "cannot cost
anything". Without a limit, anyone who finds the URL can write ``webhook_events``
rows until the disk fills.

The same applies, less dramatically, to every other endpoint.

## What it actually guarantees, which is less than it looks

**The window is per process.** Two Fly machines mean two independent windows and
therefore twice the configured rate; a restart empties the window entirely. This is
not a distributed rate limiter and must not be described as one.

That is a deliberate trade rather than an oversight, and the reasoning is the same
one Phase 4 used for `FOR UPDATE` over `SERIALIZABLE`: pick the mechanism whose
shape matches the problem, and write down what it does not cover. A shared limiter
needs Redis or the platform's edge, both of which are infrastructure this project
does not otherwise have, and adding a datastore to bound an endpoint nobody is
attacking yet is how a portfolio project acquires an ops burden it cannot justify.

What it does buy, honestly stated:

* a single client cannot trivially flood one machine;
* an accidental retry storm from a broken integration is capped;
* the shape of the thing is on the page, so replacing the store with Redis is a
  fifteen-line change in one file rather than a design exercise.

What it does not buy: protection from a distributed flood, or any guarantee at all
about the aggregate rate across machines. **The real answer at scale is the
platform edge** -- Fly's own limits, or a proxy in front -- and this is the
in-application backstop underneath it.

## Why fixed window rather than token bucket

A fixed window admits up to 2x the limit across a window boundary, which a token
bucket does not. It is chosen anyway because it needs one integer and one timestamp
per client instead of a float that has to be decayed on every read, and because the
2x burst is irrelevant at limits set for "stop a runaway loop" rather than for
billing. The failure mode is understood and bounded, which is the bar.
"""

import time
from dataclasses import dataclass, field

from starlette.requests import Request


@dataclass
class _Window:
    started_at: float
    count: int


@dataclass
class FixedWindowLimiter:
    """Counts requests per client per window, in memory.

    Keyed by client identity only, not by route. Per-route windows would let a
    caller multiply their budget by the number of endpoints, which for a service
    whose expensive endpoint is one POST is exactly the wrong way round.
    """

    limit: int
    window_seconds: int
    _windows: dict[str, _Window] = field(default_factory=dict)
    #: Windows are pruned lazily during checks rather than by a background task.
    #: A sweeper would be a second thing to run and supervise for a dict that is
    #: bounded by the number of clients seen in one window.
    _last_prune: float = 0.0

    def check(self, key: str, *, now: float | None = None) -> tuple[bool, int, int]:
        """Record a request. Returns ``(allowed, remaining, retry_after_seconds)``.

        Counts the request whether or not it is allowed, so a client that keeps
        hammering through a 429 does not get a fresh budget by being refused.
        """
        moment = time.monotonic() if now is None else now
        self._prune(moment)

        window = self._windows.get(key)
        if window is None or moment - window.started_at >= self.window_seconds:
            self._windows[key] = _Window(started_at=moment, count=1)
            return True, self.limit - 1, 0

        window.count += 1
        if window.count > self.limit:
            elapsed = moment - window.started_at
            retry_after = max(1, int(self.window_seconds - elapsed) + 1)
            return False, 0, retry_after

        return True, self.limit - window.count, 0

    def _prune(self, moment: float) -> None:
        """Drop expired windows, at most once per window period.

        Bounded work: without the interval this would walk every key on every
        request, turning the limiter into the slowest thing in the request path
        under exactly the load it exists to handle.
        """
        if moment - self._last_prune < self.window_seconds:
            return
        self._last_prune = moment
        cutoff = moment - self.window_seconds
        expired = [key for key, window in self._windows.items() if window.started_at < cutoff]
        for key in expired:
            del self._windows[key]

    def reset(self) -> None:
        """Clear all windows. For tests."""
        self._windows.clear()
        self._last_prune = 0.0


def client_key(request: Request) -> str:
    """Identify the caller for limiting purposes.

    Prefers the left-most entry of ``X-Forwarded-For``, because behind Fly's proxy
    ``request.client.host`` is the proxy and limiting on it would put every client
    in the world into one bucket.

    The honest caveat: ``X-Forwarded-For`` is a header, and a header is a thing a
    client can send. Trusting it means a determined caller can evade the limit by
    varying it, and *not* trusting it means the limiter does not work at all behind
    a proxy. The second failure is total and the first is partial, so this trusts
    it -- which is correct only because there is a trusted proxy in front. Exposed
    directly to the internet, this line would need to change.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    if request.client is not None:
        return request.client.host
    return "unknown"
