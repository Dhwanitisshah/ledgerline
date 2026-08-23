# Deploying Ledgerline for free

Phase 7 chose Fly.io and priced it honestly at **$5–7/month**, because the worker
is what costs money: the publisher and reconciler have to run for the guarantees
to hold, and Fly is the only one of the three platforms compared there where a
worker is just another $2 process group.

This document takes the other branch. It is the same reasoning under a harder
constraint — **no card, nothing paid, ever** — and it changes one decision rather
than the architecture: the workers stop being a loop on a machine and become an
hourly scheduled job. Everything else survives intact.

```
                            https://<app>.onrender.com
   visitor ────────────────────────────┐
                                       ▼
   ┌────────────────────────────────────────────────────┐
   │  Render — free web service                         │
   │  uvicorn app.main:app                              │
   │  APP_ENV=production, healthCheck=/health           │
   │  spins DOWN after 15 min idle, ~1 min to wake      │
   └───────────────────────┬────────────────────────────┘
                           │  postgresql+asyncpg://
                           ▼
   ┌────────────────────────────────────────────────────┐
   │  Neon — free Postgres                              │
   │  0.5 GB · 100 compute-hours/mo · direct endpoint   │
   │  autosuspends after 5 min of inactivity            │
   └───────────────────────▲────────────────────────────┘
                           │  same DATABASE_URL, as a repo secret
   ┌───────────────────────┴────────────────────────────┐
   │  GitHub Actions — hourly, free on public repos     │
   │  reconcile --once → publisher --once → drift --once│
   └────────────────────────────────────────────────────┘
```

## What this is, and what it is not

It demonstrates **deployed and reachable**: a real URL, a real managed Postgres,
migrations applied on deploy, and the workers genuinely running on a schedule
rather than being quietly dropped.

It does **not** demonstrate production-grade infrastructure, and the gaps are
specific rather than vague:

- **There is still no authentication.** Every endpoint is open to anyone with the
  URL. This is the project's largest gap at every phase, and deploying it does not
  change that — it just makes it reachable from the internet.
- **Events drain hourly, not in seconds.** A real trade rather than a bug (see
  below), but a payments system whose downstream consumers can be an hour behind
  is not one you would run a business on.
- **One instance, no redundancy.** A restart is an outage.
- **No backups tested.** Neon takes its own; this project has never restored one,
  and an untested restore is a hope rather than a plan.
- **Cold starts.** The first visitor after 15 idle minutes waits about a minute.

## The three seams, honestly

### 1. The workers run hourly, so the outbox drains hourly

Phase 5b's publisher normally loops every `OUTBOX_INTERVAL_SECONDS` (5). Here it
runs once an hour, because no free platform offers an always-on worker: Render's
cheapest is $7/mo, Koyeb's free instance explicitly cannot run Worker Services,
and Fly has had no free tier since 2024.

**This is safe, and the reason is the whole point of Phase 5b.** The event was
committed in the same transaction as the money. It is not at risk while it waits —
it is durable, and a delayed publish is delayed rather than lost. What you lose is
latency. What you must not do is drop the worker entirely and hope:
`ledgerline_outbox_pending` is precisely the gauge that catches that, and the
hourly run prints it at the end of every pass.

The order inside the run matters: **reconcile, then publish, then drift.** Settling
a stranded payment writes a `payment.succeeded` outbox event, so reconciling first
means that event goes out in the same pass instead of waiting another hour.

### 2. Neon autosuspends, and the cadence is a budget

Neon's free tier gives **100 compute-hours per month** and suspends the database
when they are gone. Compute suspends after 5 minutes of inactivity, so every wake
costs the run plus that timer — roughly 6 minutes.

| Worker cadence | Compute burned | Verdict |
| --- | --- | --- |
| Every 5 min | never suspends, ~730 h/mo | suspended within days |
| Every 30 min | ~144 h/mo | over budget |
| **Hourly** | **~72 h/mo** | **fits, with headroom** |

That headroom is not spare — it absorbs deploys (the build runs `alembic upgrade
head`, which wakes the database) and whatever demo traffic the link gets. **If you
speed the cron up, do this arithmetic first.**

This is also why the Render health check points at `/health` and not `/ready`.
`/health` touches no dependency; `/ready` opens a Postgres connection. A readiness
probe on a timer would hold Neon permanently awake and spend the entire monthly
budget on health checks — the database would go down while every dashboard showed
green.

### 3. Render sleeps, and waking it is not free either

A free web service spins down after 15 minutes without traffic and takes about a
minute to return. `.github/workflows/keepwarm.yml` will hold it awake by pinging
`/health` every 10 minutes, and it is **disabled by default** because Render grants
a workspace **750 free instance-hours per calendar month** and suspends all free
web services once they are spent. Awake around the clock is ~730 hours: the whole
allowance, on one service.

The default here is to accept the cold start and warm the service by hand before
showing it to anyone. That is a defensible trade for a portfolio link and a bad one
for anything real, which is the honest summary of this entire document.

## The `postgresql+asyncpg://` rewrite

**This is the single most likely thing to go wrong, and it goes wrong twice.**

Neon — like Render, Railway and Fly — hands out a plain connection string:

```
postgresql://ledgerline_owner:npg_XXXX@ep-cool-name-123456.us-west-2.aws.neon.tech/ledgerline?sslmode=require
```

SQLAlchemy needs the driver named in the scheme. Phase 7 added a startup guard that
refuses the plain form rather than letting it fail at the first query, so a URL
pasted verbatim aborts the boot with one sentence instead of breaking every request
on a machine nobody is watching.

Rewrite the scheme, keeping everything after it byte for byte:

```
postgresql+asyncpg://ledgerline_owner:npg_XXXX@ep-cool-name-123456.us-west-2.aws.neon.tech/ledgerline?sslmode=require
```

You must do this in **both** places the URL is set — the Render environment
variable and the GitHub Actions secret. They are two separate copies and nothing
keeps them in step.

### Use the direct endpoint, not the `-pooler` one

Neon offers a pooled endpoint whose host contains `-pooler`. **Do not use it here.**
It is PgBouncer in transaction mode, which does not support prepared statements, and
asyncpg uses them by default — a combination that fails intermittently under
SQLAlchemy in ways that `statement_cache_size=0` does not reliably fix.

It is also unnecessary: this application already pools in-process (`DB_POOL_SIZE`),
and `render.yaml` shrinks that pool to 5+5 for exactly this deployment. The stock
25+35 was sized for Phase 4's concurrency harness against a local Postgres; a 0.25
CU Neon compute offers about 97 connections in total, and the hourly workers need
some of them too.

---

## Ordered setup

### 1. Neon — create the database

1. Sign up at [neon.tech](https://neon.tech) (no card).
2. **Create a project.** Name it `ledgerline`. Set the region to **AWS US West
   (Oregon)** so it matches the Render region in `render.yaml` — a free web service
   in Oregon talking to a database in Frankfurt pays that round trip on every one of
   the several queries a charge makes.
3. On the project dashboard open **Connection Details** and copy the connection
   string. Make sure the **"Pooled connection" toggle is OFF** — you want the host
   *without* `-pooler`.
4. Rewrite the scheme from `postgresql://` to `postgresql+asyncpg://`, leaving the
   rest untouched. Keep `?sslmode=require`. **Save this string; you need it twice.**

### 2. Render — deploy the web service

1. Sign up at [render.com](https://render.com) (no card for free instances) and
   connect your GitHub account.
2. **New → Blueprint**, select the `ledgerline` repo. Render reads `render.yaml` and
   proposes one free web service.
3. It will prompt for the value of `DATABASE_URL`, which is declared `sync: false`
   precisely so it is never committed. Paste the **rewritten**
   `postgresql+asyncpg://…` string.
4. **Apply.** The build runs `pip install -r requirements.txt && alembic upgrade
   head`, so the first deploy also creates the schema.

   If the build fails with *"DATABASE_URL must use the asyncpg driver"*, the scheme
   was not rewritten. If it fails with *"refusing to start: DATABASE_URL is still
   the local compose default"*, the variable never reached the build at all.

5. Note the URL Render assigns: `https://<app>.onrender.com`.

### 3. GitHub — give the workers the same database

1. Repo → **Settings → Secrets and variables → Actions → New repository secret**.
2. Name it exactly `DATABASE_URL`. Paste the **same rewritten** string.
3. *(Optional, only if you enable the keep-warm)* Under the **Variables** tab, add
   `RENDER_BASE_URL` = `https://<app>.onrender.com`. A variable rather than a
   secret, because it is a public URL and secrets are redacted from logs.
4. **Actions → Workers → Run workflow** to prove it end to end without waiting an
   hour. The first step verifies the secret is present and correctly prefixed before
   any worker connects.

### 4. Verify the deployment actually moves money

```powershell
# Wake it first -- a cold start can exceed the smoke script's 60s per-request
# timeout, and a timeout would fail the run for the wrong reason.
curl.exe https://<app>.onrender.com/health

.\scripts\smoke_deployed.ps1 -BaseUrl https://<app>.onrender.com
```

That script uses **only the real API surface** — no fake-processor knobs, since
those are refused when `APP_ENV=production`. It creates an account, charges it,
replays the same idempotency key, refunds in full, and checks that the live ledger
imbalance gauge is zero, so the run leaves the database where it found it.

It finishes by pointing at `ledgerline_outbox_pending`, which will be **2** (one
charge event, one refund event). Under this deployment those drain at the next
hourly run rather than within seconds — trigger **Actions → Workers → Run workflow**
to watch it fall to 0 immediately.

---

## Cost, stated plainly

| | Free tier | Real limit that bites first |
| --- | --- | --- |
| **Neon** | permanent, no card | 100 compute-hours/mo · 0.5 GB |
| **Render** | permanent, no card | 750 instance-hours/mo · sleeps at 15 min |
| **GitHub Actions** | free, unmetered on public repos | 5-minute minimum cron interval |

**$0/month, indefinitely**, provided the worker cron stays hourly and the keep-warm
stays off. Both are one-line changes that quietly break the budget, which is why
each carries the arithmetic in a comment next to it.

---

**[← Back to the README](../README.md)** · **[Phase 7: hardening & deploy](phase-7-hardening-deploy.md)**
