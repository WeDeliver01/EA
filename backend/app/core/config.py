"""Application settings.

Fails fast: an invalid or missing security-relevant variable raises at import
time (via ``get_settings()``), listing every problem at once rather than
crashing on the first one a caller happens to touch.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- core
    environment: str = Field(default="development")
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="json")
    git_sha: str = Field(default="unknown")

    # ---- database
    database_url: str = Field(default="postgresql+asyncpg://dt:dt@localhost:5432/delicate_trader")
    database_pool_size: int = Field(default=10)
    database_max_overflow: int = Field(default=5)
    database_echo: bool = Field(default=False)

    # ---- redis
    redis_url: str = Field(default="redis://localhost:6379/0")
    redis_stream_maxlen: int = Field(default=100_000)

    # ---- security
    jwt_secret_key: str = Field(default="")
    jwt_access_ttl_seconds: int = Field(default=900)
    jwt_refresh_ttl_days: int = Field(default=30)
    argon2_time_cost: int = Field(default=3)
    argon2_memory_cost: int = Field(default=65536)
    argon2_parallelism: int = Field(default=4)
    agent_secret_encryption_key: str = Field(default="")
    cors_origins: str = Field(default="http://localhost:3000")
    require_totp_for_live: bool = Field(default=True)

    # ---- trading safety
    quote_stale_seconds: int = Field(default=5)
    account_stale_seconds: int = Field(default=10)
    agent_heartbeat_timeout_seconds: int = Field(default=10)
    agent_disconnect_kill_seconds: int = Field(default=300)
    margin_safety_factor: Decimal = Field(default=Decimal("0.30"))
    reconciliation_interval_seconds: int = Field(default=30)
    position_monitor_interval_seconds: int = Field(default=2)
    candle_close_grace_ms: int = Field(default=1500)
    execution_lock_ttl_seconds: int = Field(default=60)
    global_trading_enabled: bool = Field(default=False)

    # ---- research
    backtest_snapshot_sample_rate: float = Field(default=0.005)
    celery_broker_url: str = Field(default="redis://localhost:6379/1")
    celery_result_backend: str = Field(default="redis://localhost:6379/2")
    celery_worker_concurrency: int = Field(default=2)

    # ---- observability
    sentry_dsn: str = Field(default="")
    sentry_traces_sample_rate: float = Field(default=0.05)
    prometheus_enabled: bool = Field(default=True)

    # ---- notifications
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")
    alert_min_severity: str = Field(default="warning")

    @field_validator("environment")
    @classmethod
    def _validate_environment(cls, v: str) -> str:
        allowed = {"development", "staging", "production"}
        if v not in allowed:
            raise ValueError(f"environment must be one of {allowed}, got {v!r}")
        return v

    def validate_for_production(self) -> None:
        """Security-relevant fields have no safe default. Call this explicitly
        before serving traffic in staging/production; kept separate from
        model validation so `development` settings can boot without secrets."""
        problems: list[str] = []
        if self.environment in {"staging", "production"}:
            if not self.jwt_secret_key:
                problems.append("JWT_SECRET_KEY is required")
            if not self.agent_secret_encryption_key:
                problems.append("AGENT_SECRET_ENCRYPTION_KEY is required")
        if problems:
            raise ValueError("Invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()
