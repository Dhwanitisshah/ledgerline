# Tests

```powershell
docker compose up -d
alembic upgrade head
pytest
```

Tests run against a **real Postgres**, not SQLite and not a mock. The behaviour
under test — CHECK constraints, the append-only triggers, transactional rollback,
`SUM` semantics — is behaviour the database provides, and a stand-in would test
none of it. CI runs a `postgres:16` service for the same reason.

The exception is [tests/test_payment_state.py](../tests/test_payment_state.py), the
only file in the suite that needs no database. That is deliberate: whether a state
change is legal is decided by the transition table and by nothing else, so testing
it should not require standing up a charge to reach each state.

The suite runs both background workers **in-process** rather than spawning them:
`sweep_once()` and `publish_once()` are coroutines, so the tests call them directly
and assert on the `SweepReport` / `PublishReport` they return. CI therefore never
has a worker to start, wait for, or kill — nothing hangs, and there is no polling
loop deciding how long to give a background process before failing. "Killing the
worker mid-publish" is a sink that raises `SimulatedWorkerCrash` at the fatal
instant, which to Postgres is the same event as the backend dying: a transaction
that ends without `COMMIT`. Restarting it is a second call.

The CLIs are covered separately, by four CI steps that each run one short-lived
command (`--once`, `--status`, `--list`) under a timeout. That splits the two claims
apart: the suite asserts what the workers *do*, and CI asserts that their entry
points start, connect, do one pass, and **exit**. A worker that looped forever would
be a red build rather than a stuck runner.

CI also round-trips the newest migration — `alembic downgrade <previous> && alembic
upgrade head` — because a down-migration nobody runs is a down-migration that does
not work. Phase 6's is the one that most needed it: migration 0007 *replaces* a
constraint rather than adding a table, so its `downgrade()` has to put the Phase 2
form back, and it will correctly fail if any payment is currently `refunded` —
restoring a rule the data violates is not something to do quietly.

Five tests are marked `race` (`pytest -m "not race"` to skip them). They assert that
deliberately preserved broken code is *still broken* — the two Phase 4 races and the
Phase 5a lost charge. They are not xfails: an xfail passes when the thing quietly
stops happening, and a reproduction that stopped reproducing is precisely what you
want to be told about.


---

**[← Back to the README](../README.md)**
