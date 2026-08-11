from pydantic_settings import BaseSettings, SettingsConfigDict

from app.processor import ProcessorOutcome
from app.strategies import ClaimStrategy, WithdrawalGuard


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

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


settings = Settings()
