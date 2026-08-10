from pydantic_settings import BaseSettings, SettingsConfigDict

from app.processor import ProcessorOutcome


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    APP_ENV: str = "local"
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5433/ledgerline"

    # --- Fake processor (Phase 2) ----------------------------------------------
    # The default outcome of every charge, and how long the adapter pretends to
    # take. Typed as the enum rather than as `str` so PROCESSOR_OUTCOME=banana
    # fails at startup instead of at the first charge.
    #
    # A single request can override both via `force_outcome` / `force_latency_ms`
    # in the body; see app/processor.py.
    PROCESSOR_OUTCOME: ProcessorOutcome = ProcessorOutcome.SUCCESS
    PROCESSOR_LATENCY_MS: int = 0


settings = Settings()
