# Running locally (Windows / PowerShell)

```powershell
# 1. Start Postgres
docker compose up -d

# 2. Create + activate a venv, install deps
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 3. Run migrations
alembic upgrade head

# 4. Run the app
uvicorn app.main:app --reload

# 5. Run the reconciler (separate terminal, Phase 5a)
#    A separate process on purpose: recovery that lives inside the process being
#    recovered from is unavailable exactly when it is needed.
python -m app.reconcile

# 6. Run the outbox publisher (another terminal, Phase 5b)
#    Also separate, for a related reason: the outbox is the thing that still works
#    when the API is down, and coupling the drain to the web process would stop
#    delivery of exactly the events an outage produced.
python -m app.publisher

# 7. Check for drift whenever you like (Phase 6). Reports, never repairs.
python -m app.drift --once

# 8. Smoke checks (another terminal)
.\scripts\smoke.ps1
.\scripts\smoke_phase2.ps1
.\scripts\smoke_phase3.ps1
.\scripts\smoke_phase4.ps1
.\scripts\smoke_phase5a.ps1
.\scripts\smoke_phase5.ps1
.\scripts\smoke_phase6.ps1
.\scripts\smoke_phase7.ps1

# 9. Load test (needs the limiter out of the way; see the Load test section)
#    $env:RATE_LIMIT_ENABLED="false" before starting uvicorn
python scripts\loadtest.py --iterations 300 --concurrency 20

# 10. And against a deployment, once there is one
.\scripts\smoke_deployed.ps1 -BaseUrl https://ledgerline.fly.dev
```

Steps 5 and 6 are optional for every smoke script, including the two that exercise
those workers: both shell out to `--once` themselves, so the sweep and the publish
happen at points in the story you can watch rather than whenever a timer fires.
Leave the long-lived workers running and the scripts still pass — they assert on
counts relative to where they started, not on an empty database.

`scripts\smoke.ps1` (Phase 1) creates two accounts, posts a balanced transfer,
asserts both derived balances, fires an unbalanced posting expecting a 422, and
confirms the rejected posting changed nothing.

`scripts\smoke_phase2.ps1` charges an account and asserts 201 + `succeeded` + the
balance moved, then forces a processor failure and asserts the payment is `failed`,
its `ledger_transaction_id` is null, and **the balance is unchanged** — the ledger
was never touched. It drives the failure with `force_outcome` in the request body,
so the server does not need restarting between the two checks.

`scripts\smoke_phase3.ps1` charges with an `Idempotency-Key`, sends the identical
request again with the same key, and asserts the response is byte-identical and the
balance moved **once**. Then it reuses the key with a different amount (422), omits
the header entirely (400), proves a fresh key is a fresh charge, and replays a
declined charge to show the failure path is idempotent too.

`scripts\smoke_phase5a.ps1` crashes a charge at the fatal instant (`500`), shows the
money missing and the customer's retry refused with a `409`, runs one reconciliation
pass, and then shows the same retry replaying a `succeeded` charge with the balance
moved **once**. It repeats the whole thing for a crashed *decline*, proving the sweep
asks the processor rather than assuming every stuck payment succeeded. It shells out
to `python -m app.reconcile --once` because the reconciler is a process, not an
endpoint.

`scripts\smoke_phase5.ps1` charges an account and shows the outbox already holding an
unpublished event — written by the same commit that moved the money, with no broker
call anywhere in the request — then drains it with `python -m app.publisher --once`
and shows a second pass changing nothing. It then crashes a charge, finds the
stranded payment with `python -m app.reconcile --list`, settles it with a webhook,
and **sends the identical webhook again**: `duplicate: true`, the same recorded
outcome, and a balance that moved exactly once. Both workers are reached through
their CLIs for the same reason as Phase 5a's: they are processes, not endpoints.

`scripts\smoke_phase6.ps1` charges an account and refunds it in full, showing the
balance return to zero and the payment reach `refunded` — a status that was
unreachable for five phases. It then charges again, refunds *part* of it (balance
moves, payment stays `succeeded`), and attempts to refund more than is left,
expecting a 422 with nothing written. It double-submits one refund under a single
`Idempotency-Key` and asserts the responses are byte-identical and the balance moved
once. Finally it runs `python -m app.drift --json` and asserts the two sides agree.
Its footer shows how to inject drift with one `psql` line and watch the job find it.

`scripts\smoke_phase7.ps1` asserts the edge rather than the money: that every
response carries a request id and honours a supplied one, that the security headers
are set, that `/health` and `/ready` answer different questions, that `/metrics`
exposes the four domain gauges with `ledgerline_ledger_imbalance_minor_units` at
zero, that series are labelled by route template and never contain a payment id,
and that twenty-five health checks in a row are never rate limited. Its last section
sends `force_crash_after_processor`: against a production-configured server that is
a 422, and against a local one it is the designed 500 — which the script still
checks carries a request id and leaks no traceback and no file paths.

To force failures server-wide instead, restart under the environment variable:

```powershell
$env:PROCESSOR_OUTCOME = "failure"; uvicorn app.main:app --reload
# every charge now declines, and every one of them moves exactly zero money
```


---

**[← Back to the README](../README.md)**
