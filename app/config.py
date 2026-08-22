from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.processor import ProcessorOutcome
from app.strategies import ChargeDurability, ClaimStrategy, WithdrawalGuard


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # 'local' | 'ci' | 'staging' | 'production'. Phase 7 gives this teeth: it is
    # what decides whether the fake processor's test knobs are reachable over HTTP
    # and whether the deliberately-broken strategy paths may be selected at all.
    APP_ENV: str = "local"
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5433/ledgerline"

    # --- Connection pool (Phase 4) ---------------------------------------------
    # Sized so the concurrency harness can actually be concurrent. With the
    # SQLAlchemy defaults (5 + 10 overflow) a 50-request burst would queue 35 of
    # them on the pool, and a test that queues is a test that proves nothing about
    # the database. 25 + 35 gives 60 simultaneous backends, comfortably inside
    # Postgres's default max_connections of 100.
    DB_POOL_SIZE: int = 25
    DB_MAX_OVERFLOW: int = 35

    # --- Fake processor (Phase 2) ----------------------------------------------
    # The default outcome of every charge, and how long the adapter pretends to
    # take. Typed as the enum rather than as `str` so PROCESSOR_OUTCOME=banana
    # fails at startup instead of at the first charge.
    #
    # A single request can override both via `force_outcome` / `force_latency_ms`
    # in the body; see app/processor.py.
    PROCESSOR_OUTCOME: ProcessorOutcome = ProcessorOutcome.SUCCESS
    PROCESSOR_LATENCY_MS: int = 0

    # --- Concurrency strategies (Phase 4) ---------------------------------------
    # Which implementation of each race-prone path to run. The naive values are
    # deliberately preserved broken code; see app/strategies.py. Defaults are the
    # fixed paths, and nothing but the harness should ever change them.
    IDEMPOTENCY_CLAIM_STRATEGY: ClaimStrategy = ClaimStrategy.LOCKED
    WITHDRAWAL_GUARD: WithdrawalGuard = WithdrawalGuard.ROW_LOCK

    # How far a withdrawal may take an account below zero. 0 means no overdraft.
    # This is the first balance floor in the project -- Phases 1-3 deliberately let
    # accounts go arbitrarily negative, because a ledger without credit limits
    # should.
    BALANCE_FLOOR_MINOR_UNITS: int = 0

    # Widens the check-then-act window on the *naive* paths only, so the race is
    # reproducible on demand instead of whenever the scheduler feels like it. It
    # does not create either bug: both windows exist at zero, since a SELECT and a
    # subsequent INSERT are two statements with a round trip between them. This
    # only makes an existing window wide enough to hit reliably.
    NAIVE_RACE_WINDOW_MS: int = 0

    # --- Durability and reconciliation (Phase 5a) --------------------------------
    # When the charge attempt is committed relative to the processor call. The
    # naive value is the Phases 2-4 flow, preserved so the crash gap can be
    # reproduced; see app/strategies.py.
    CHARGE_DURABILITY: ChargeDurability = ChargeDurability.DURABLE_INTENT

    # How long a payment may sit in 'processing' before the sweep treats it as
    # abandoned by the request that created it. This is a *timeout on our own
    # crash recovery*, not a timeout on the processor: a healthy charge passes
    # through 'processing' in the time one authorisation takes. 60s is comfortably
    # longer than any processor call the fake makes, and short enough that a
    # customer is not left unresolved for long. Tests drive it to 0.
    RECONCILE_STUCK_AFTER_SECONDS: int = 60

    # Most payments one sweep will settle. A bound rather than "all of them" so a
    # backlog is worked through in steady batches instead of one transaction that
    # holds locks on thousands of rows.
    RECONCILE_BATCH_SIZE: int = 100

    # Seconds between passes when the sweep runs as a loop (python -m app.reconcile).
    RECONCILE_INTERVAL_SECONDS: int = 10

    # --- The outbox publisher (Phase 5b) -----------------------------------------
    # Most events one publish pass will deliver. A bound rather than "drain it" so
    # that a pass terminates under sustained load -- which `--once` and anyone
    # trying to stop the worker both depend on.
    OUTBOX_BATCH_SIZE: int = 100

    # Seconds between passes when the publisher runs as a loop
    # (python -m app.publisher). This is the upper bound on how stale a downstream
    # consumer's view can be, and nothing more: the event itself was committed with
    # the money and is not at risk while it waits.
    OUTBOX_INTERVAL_SECONDS: int = 5

    # --- Drift detection (Phase 6) ------------------------------------------------
    # How settled a payment must be before the drift job will compare it against the
    # processor's books. This is not a performance knob -- it is what stops the job
    # reporting normal in-flight work as a discrepancy. A charge mid-settlement
    # legitimately exists on one side and not yet the other, and a job that shouted
    # about that would be a job nobody reads.
    DRIFT_GRACE_SECONDS: int = 60

    # Most payments one drift pass will examine. Bounded like every other pass in
    # this project, so `--once` terminates and an operator can predict what a run
    # costs.
    DRIFT_BATCH_SIZE: int = 500

    # --- Hardening (Phase 7) --------------------------------------------------------
    # Whether `force_outcome`, `force_latency_ms` and `force_crash_after_processor`
    # are honoured on POST /charges and POST /charges/{id}/refund.
    #
    # None means "derive from APP_ENV", and deriving is the safe direction: a
    # deployment that forgets to set this still refuses them, because the default
    # falls out of APP_ENV=production rather than out of a boolean somebody had to
    # remember. A stray `force_crash_after_processor: true` from a stranger would
    # otherwise strand a payment in 'processing' on demand -- the exact state Phase
    # 5a built a sweep to recover from, reachable by anyone with curl.
    #
    # Set it to true explicitly on a staging box where the smoke scripts should run
    # against a real deployment. Setting it true in production is possible and is
    # meant to feel like a decision.
    ALLOW_TEST_AFFORDANCES: bool | None = None

    # Requests per window per client. In-process and therefore per-machine; see
    # app/ratelimit.py for the honest limits of that.
    #
    # 600/60s is 10 requests a second from one client. This started at 120 (2/s) and
    # was raised after running the eight smoke scripts back to back tripped it --
    # which is the useful kind of accident, because it showed the number was set for
    # a threat model nobody has rather than for the traffic that actually exists. A
    # limiter that refuses ordinary scripted use is one that gets disabled entirely,
    # and a disabled limiter protects nothing. This still stops a flood dead while
    # leaving legitimate integration work far below the ceiling.
    RATE_LIMIT_REQUESTS: int = 600
    RATE_LIMIT_WINDOW_SECONDS: int = 60
    RATE_LIMIT_ENABLED: bool = True

    # Comma-separated origins for CORS, or "" for none. Empty by default: this is an
    # API with no browser client, and an allow-list that starts permissive is one
    # nobody ever tightens.
    CORS_ALLOW_ORIGINS: str = ""

    # 'json' | 'text'. JSON in a deployment, where logs are ingested by a machine;
    # text locally, where they are read by a person.
    LOG_FORMAT: str = "text"
    LOG_LEVEL: str = "INFO"

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.strip().lower() in {"production", "prod"}

    @property
    def test_affordances_allowed(self) -> bool:
        """Whether the fake processor's knobs may be driven from a request body."""
        if self.ALLOW_TEST_AFFORDANCES is not None:
            return self.ALLOW_TEST_AFFORDANCES
        return not self.is_production

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ALLOW_ORIGINS.split(",") if origin.strip()]

    @model_validator(mode="after")
    def _refuse_broken_paths_in_production(self) -> "Settings":
        """The preserved-broken paths are not deployable, and this says so at startup.

        Phases 4 and 5a keep their naive implementations in the tree on purpose, so
        the before/after stays a measurement rather than a memory. That is worth a
        lot in a repository and worth nothing in production, where selecting one
        means running code the project itself documents as wrong -- a double-charge
        under concurrency, or a charge that loses a charged card.

        Failing at startup rather than at the first request is deliberate: a
        misconfigured deployment should never come up healthy and then be wrong
        under load. This is the one setting whose mistake is unrecoverable, since by
        the time you notice, the money has moved.
        """
        if not self.is_production:
            return self

        broken = {
            "IDEMPOTENCY_CLAIM_STRATEGY": (
                self.IDEMPOTENCY_CLAIM_STRATEGY, ClaimStrategy.NAIVE
            ),
            "WITHDRAWAL_GUARD": (self.WITHDRAWAL_GUARD, WithdrawalGuard.NAIVE),
            "CHARGE_DURABILITY": (self.CHARGE_DURABILITY, ChargeDurability.SINGLE_TXN),
        }
        selected = [name for name, (value, naive) in broken.items() if value is naive]
        if selected:
            raise ValueError(
                f"refusing to start: {', '.join(selected)} selects a deliberately "
                "broken implementation, and APP_ENV=production. These paths exist so "
                "the bug can be reproduced on demand (see app/strategies.py); they "
                "are not production paths and never were."
            )

        if self.NAIVE_RACE_WINDOW_MS > 0:
            raise ValueError(
                "refusing to start: NAIVE_RACE_WINDOW_MS widens a race window on "
                "purpose and APP_ENV=production"
            )

        return self


settings = Settings()
