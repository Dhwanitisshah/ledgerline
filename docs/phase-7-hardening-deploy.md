# Phase 7: hardening, observability, and deploy

Phases 1–6 asked *is it correct?*. This one asks two different questions:

- **Is it safe to expose?** A system can be perfectly correct and still hand a
  stranger the ability to strand payments.
- **Can you tell what it is doing from outside?** Without attaching a debugger, at
  3am, from a phone.

### The knob that had to be closed

Six phases of test affordances were reachable over HTTP:

```
POST /charges  { "force_crash_after_processor": true }
```

That field abandons a charge at the exact instant Phase 5a exists to survive,
leaving a payment stranded in `processing`. Indispensable locally — it is how the
entire durability guarantee is demonstrated. On a public URL it is an
**unauthenticated denial of correctness**: anyone with `curl` can manufacture the
state the reconciler exists to clean up, as fast as they can send requests.

It is **gated, not deleted**. Deleting it would take the crash reproduction and
four of the eight smoke scripts with it, and this project's whole method is keeping
the broken and the awkward runnable on demand.

```python
@property
def test_affordances_allowed(self) -> bool:
    if self.ALLOW_TEST_AFFORDANCES is not None:
        return self.ALLOW_TEST_AFFORDANCES
    return not self.is_production          # derived, so forgetting is safe
```

The direction matters. A deployment that sets **nothing** still refuses, because
the default falls out of `APP_ENV` rather than out of a boolean somebody had to
remember. The opposite arrangement ships an open crash endpoint the day someone
forgets a line of config.

### The broken paths refuse to boot

Phases 4 and 5a keep their naive implementations runnable so the before/after stays
a measurement rather than a memory. In production, selecting one means running code
this project documents as wrong — a double-charge under concurrency, or a charge
that loses a charged card. So `Settings` refuses to construct:

```powershell
docker run --rm -e IDEMPOTENCY_CLAIM_STRATEGY=naive ledgerline:latest python -c "import app.config"
# ValueError: refusing to start: IDEMPOTENCY_CLAIM_STRATEGY selects a deliberately
# broken implementation, and APP_ENV=production.
```

At **startup**, not at the first request. A misconfigured deployment must never come
up healthy and then be wrong under load, because by the time anyone notices, the
money has already moved.

### One middleware, and what four cost

The obvious shape is four small `BaseHTTPMiddleware` classes — an id, a limiter, an
access log, some headers. That is how it was written first, and it was measurably
wrong. `BaseHTTPMiddleware` runs the rest of the app inside an anyio task group with
memory object streams between the halves; that is what buys the tidy
`dispatch(request, call_next)` signature, and it costs a task group, two streams and
several context switches **per layer, per request**.

Phase 4's concurrency harness caught it — a test written three phases earlier, to
prove something else entirely:

| Twelve concurrent charges, 300 ms processor call | elapsed |
| --- | --- |
| Phases 4–6 (no middleware) | ~0.4 s |
| Phase 7, four `BaseHTTPMiddleware` layers | **1.5–2.3 s** (failed its 1.8 s bound ~40% of runs) |
| Phase 7, one pure-ASGI middleware | **~0.35 s**, 6/6 stable |

The whole test suite went from ~110 s to ~70 s at the same time. So it is **one**
pure-ASGI middleware that wraps `send` to stamp response headers and adds no task
groups at all. The four concerns are still four clearly separated blocks — they are
just not four trips through the ASGI stack.

> A concurrency harness that is really measuring the middleware is a harness that
> has stopped measuring concurrency. That is why the regression was visible at all.

### Errors say nothing and identify everything

An unhandled exception returns exactly this:

```json
{ "detail": "internal server error", "request_id": "9f3a1c2e-..." }
```

No exception class, no message, no traceback. The full detail goes to the log under
that same id. Not paranoia about tracebacks being *interesting* — a traceback tells
the caller about your file layout while telling them nothing they can act on, and
"quote request 9f3a1c" is something a user can put in an email and an engineer can
grep. Two audiences, two different pieces of information.

Every response carries `X-Request-ID`, and a caller's own id is honoured so a trace
can span two services — after being truncated and stripped of anything unprintable.
That value lands in every log line, and a newline in the middle of one is how a
single request forges log entries that never happened.

### The one place `default-src 'none'` is wrong

Every response carries `Content-Security-Policy: default-src 'none'`, which for a
JSON API is exactly right: the body has no legitimate subresources, so nothing is
allowed to load. It is also exactly wrong for the two HTML pages this service
serves. Swagger UI is a CDN script bundle, a CDN stylesheet, a favicon and one
inline `<script>`, and `'none'` blocked all of them — `/docs` and `/redoc` rendered
as a blank page with six refusals in the console, which looks precisely like an
application that is down.

The relaxation is **per route, not per origin**: `/docs`, `/redoc` and
`/openapi.json` get a policy that allows `cdn.jsdelivr.net` and the fonts and
favicon the pages reference; every other path keeps the strict one. That second
half is the half worth testing, and it is tested — over live requests *and*
exhaustively over the route table, so a route added later is covered by a test
nobody has to remember to update.

The inline script is allowed by the **sha256 of its own contents**, computed in the
handler from the bytes about to be served (`app/docs.py`). `'unsafe-inline'` would
have fixed the blank page in one word and given up the only property the header was
buying; a hardcoded hash would break the page silently on the next FastAPI bump.
Inline *styles* are still allowed wholesale, because both bundles style themselves
through inline `style` attributes, which no hash can cover — a stated limit rather
than an oversight.

### Rate limiting, and what it does not do

`/webhooks` is a public endpoint with no signature verification (carried forward
from Phase 5b). The Phase 5b design keeps the blast radius small — the payload is
not the authority, so a forged event can at worst cause a lookup — but *cannot move
money* is not *cannot cost anything*. Without a limit, anyone who finds the URL can
write `webhook_events` rows until the disk fills.

**The window is per process.** Two machines mean two independent windows and
therefore twice the configured rate; a restart empties it. This is not a distributed
rate limiter and is not described as one. It is the in-application backstop
underneath the platform edge, and replacing the store with Redis is a fifteen-line
change in one file precisely because the shape is on the page.

Health, readiness and metrics are never limited. **A 429 on a liveness probe is a
platform concluding the machine is unhealthy and restarting it** — a rate limiter
turned into an outage.

### Liveness and readiness are different questions

| Endpoint | Checks | Failure means |
| --- | --- | --- |
| `/health` | nothing — no dependency at all | restart me |
| `/ready` | `SELECT 1` against Postgres | stop sending me traffic |

Conflating them is how a database blip becomes a restart loop: if `/health` touched
Postgres, one failover would fail liveness on **every machine simultaneously** and
the platform would respond by restarting all of them — the single action most likely
to make a database problem worse. Fly's health check points at `/ready`.

### The metric worth putting on a dashboard

`/metrics` serves Prometheus text: request counts, a latency histogram, and four
gauges read from Postgres on scrape. One of them is the point of the whole project:

```
# HELP ledgerline_ledger_imbalance_minor_units Credits minus debits over every
# ledger entry, in minor units. MUST be 0 -- a non-zero value means money was
# written outside the double-entry flows.
ledgerline_ledger_imbalance_minor_units 0
```

**The entire double-entry invariant, as one number you can alert on.** Not "usually
near zero" — Phase 1 has enforced per-posting balance since the beginning, so the
total across the whole ledger is exactly zero or something wrote money outside every
flow that is supposed to be the only way to write money. If that gauge is ever
non-zero, nothing else on the dashboard matters.

The other three are the operational counterparts of Phases 5a, 5b and 6:

| Gauge | Steady state | Non-zero means |
| --- | --- | --- |
| `ledgerline_outbox_pending` | near 0 | the publisher is behind, or dead |
| `ledgerline_payments_stuck` | 0 | the reconciler is not running |
| `ledgerline_refunds_over_limit` | 0 | migration 0007's trigger was bypassed |

The last one should be structurally impossible, which is exactly why it is exported:
a number that cannot happen is a cheap and very loud check that the structure is
still there.

They are **queried on scrape rather than counted in process**. A counter tells you
what this machine thinks it did since it restarted; every one of these asks what is
*true right now*, and after a deploy those are very different numbers.

Metrics are labelled by **route template** (`/charges/{payment_id}`), never by path.
Labelling by path means one time series per payment id — which does not degrade the
service, it degrades the monitoring system, slowly, until nobody can query anything
and the cause is weeks in the past.

### Choosing the platform, honestly

The constraint that decides this is not the web service — all three host a FastAPI
app for free or nearly free. It is the **worker**. Phase 5b's publisher and Phase
5a's reconciler have to be running for the guarantees to hold, and a deployment
that quietly drops them is a deployment where the outbox fills up forever and
stranded payments are never settled.

| | Web | Managed Postgres | **Background worker** | The honest catch |
| --- | --- | --- | --- | --- |
| **Fly.io** *(chosen)* | ~$2/mo, scale-to-zero | Fly Postgres, ~$2–3/mo smallest | **Just another process group.** Same price as any machine, ~$2/mo | No real free tier since 2024. Fly Postgres is an app *you* operate, not a fully managed service — backups need setting up |
| **Render** | Free (sleeps after 15 min idle) | Free tier **expires after 30 days and is deleted** | **Requires a paid instance, $7/mo minimum.** No free workers, and cron jobs are paid too | The 30-day database expiry makes the free path useless for a portfolio link meant to survive |
| **Railway** | $5 trial credit, then usage-based | Included, usage-based | Just another service, cheap | No perpetual free tier, and usage billing is hard to predict for something left running |

**Fly wins on the only axis that matters here**: the worker is a first-class,
$2/month process group rather than a $7/month upsell. Render's free tier looks
better until you notice that the free database deletes itself after a month and the
worker was never free at all.

**The cheapest path that actually runs the worker** is roughly $5–7/month on Fly:
one `shared-cpu-1x` web machine with `min_machines_running = 0`, one 256 MB worker
machine, and the smallest Postgres. If even that is too much, the honest cheaper
option is to make the publisher a **scheduled machine** rather than a loop:

```powershell
# Drain the outbox every 5 minutes instead of every 5 seconds, on no always-on VM.
fly machine run --schedule=hourly --command "python -m app.publisher --once"
```

That trades latency for cost and is a real trade rather than a workaround: the
outbox is durable, so a delayed publish is delayed and not lost. What you must not
do is drop the worker entirely and hope — the events accumulate and nothing tells
you until a downstream consumer asks why it is a day behind. `ledgerline_outbox_pending`
is exactly the gauge that catches it.

### Deploying to Fly.io

**Three process groups from one image.** The API, the outbox publisher and the
reconciler are the same build with different commands, so a worker can never run
different code than the API that writes the rows it consumes:

```toml
[processes]
  app        = "uvicorn app.main:app --host 0.0.0.0 --port 8000"
  publisher  = "python -m app.publisher"
  reconciler = "python -m app.reconcile"
```

The drift detector is deliberately **not** a process group. It is a report, not a
loop: it terminates, writes nothing, and a permanent machine would burn a VM doing
nothing between passes.

**Migrations run once, as a release command**, before any new machine takes traffic:

```toml
[deploy]
  release_command = "alembic upgrade head"
```

This is the whole reason not to migrate from an entrypoint script: with three
process groups, an entrypoint runs the migration three times concurrently on boot.
Alembic takes a lock and would mostly survive that, but "mostly survives a race we
introduced on purpose" is not a deployment story. A failure here aborts the deploy
with the old version still serving.

**The workers are not auto-stopped.** A stopped reconciler is a stopped recovery
mechanism, and Phase 5a is explicit that durability buys the *ability* to recover
while something still has to perform it. A payment stranded at 3am should not wait
for someone to send an HTTP request before anyone notices.

```powershell
# One-time setup
fly launch --no-deploy --copy-config      # fly.toml is already in the repo
fly postgres create --name ledgerline-db
fly postgres attach ledgerline-db         # sets DATABASE_URL as a secret

# asyncpg needs the +asyncpg driver; Fly's attach writes a plain postgres:// URL
fly secrets set DATABASE_URL="postgresql+asyncpg://<user>:<pass>@<host>:5432/<db>"

fly deploy

fly logs                                  # JSON, one object per line
fly status
curl.exe https://ledgerline.fly.dev/ready
curl.exe https://ledgerline.fly.dev/metrics

# The drift report, on demand rather than on a machine
fly ssh console -C "python -m app.drift --once"
```

Then verify the live deployment actually moves money and puts it back:

```powershell
.\scripts\smoke_deployed.ps1 -BaseUrl https://ledgerline.fly.dev
```

That script deliberately uses **only the real API surface** — no fake-processor
knobs, since those are refused in production. A deployed smoke that relied on them
would fail against a correctly-configured deployment and pass against a dangerously
configured one, which is exactly backwards. It charges, replays the same key,
refunds in full, and checks that the live ledger imbalance gauge is still zero — so
the run leaves the database where it found it.

The image was rehearsed locally against the real Postgres before any of this: it
builds, runs the migration, serves `/ready`, refuses the crash knob with a 422, and
emits JSON logs — all as an unprivileged uid on Python 3.13.

**That last part was a real bug.** The Dockerfile pinned `python:3.12-slim` while CI
and ruff targeted 3.13, so the container had never run the interpreter the 168 tests
were green against. CI now builds the image and asserts its Python version, its uid,
and its production defaults, because a build that only ever happens on Fly is a build
that breaks on Fly.

### The guarantee, stated precisely

**What you get:**

- **The test affordances are unreachable in production**, by derivation rather than
  by remembering, and the deliberately-broken paths abort startup rather than
  serving wrong answers.
- **Every response is traceable** to a log line, and no response carries internals.
- **The double-entry invariant is observable**, continuously, as a single number.
- **One image, three processes**, with migrations applied exactly once per release.

**What you do not get:**

- **No authentication.** There is none, anywhere. Every endpoint is open to anyone
  who has the URL. That is the largest single gap in the project and it is stated
  plainly rather than implied by the absence of a section.
- **No distributed rate limiting.** Per process, per machine. Above.
- **No webhook signature verification.** Carried forward from Phase 5b.
- **No dead-letter queue** for events that fail forever, and no alerting on any
  gauge — the numbers are exported, and nothing is watching them.
- **No secret rotation, no backups, no restore drill.** Fly Postgres has its own
  snapshots; this project has never tested restoring one, and an untested restore is
  a hope rather than a plan.
- **No tracing spans.** Request ids correlate logs, which is not the same as a trace.

### Endpoints added

| Method | Path | Behaviour |
| --- | --- | --- |
| `GET` | `/health` | Liveness. Touches no dependency. |
| `GET` | `/ready` | Readiness. 503 when Postgres is unreachable. |
| `GET` | `/metrics` | Prometheus text, including the four domain gauges. |

### Not in Phase 7 (on purpose)

No authentication or API keys, no tracing, no dead-letter queue, no alerting rules,
no backup/restore automation, no autoscaling policy, no CDN, no staging environment
in the repo. Several of those are one commit each; none of them is what this project
set out to demonstrate, and a capstone that quietly grew four more phases would be a
worse ending than one that says where it stopped.

## Smoke acceptance criteria

- The fake processor's knobs are refused when `APP_ENV=production`, by derivation
  rather than by a flag somebody had to set
- A deliberately-broken strategy path aborts startup instead of serving wrong answers
- Every response carries `X-Request-ID`; a 500 carries an id and nothing else — no
  traceback, no exception message, no file paths
- Security headers on every response, with the CSP relaxed on the three docs
  routes and nowhere else; liveness and readiness are separate endpoints
- `/metrics` serves valid Prometheus text with `ledgerline_ledger_imbalance_minor_units`
  at 0, labelled by route template and never by path
- Rate limiting refuses with `Retry-After`, and never refuses a health check
- The image builds, runs Python 3.13 unprivileged, applies migrations, serves
  `/ready`, and emits JSON logs
- `pytest` + `ruff` green locally and in CI


---

**← Previous:** [Phase 6: refunds and reconciliation](phase-6-refunds-reconciliation.md) · **[All phases](../README.md#the-phases)**
