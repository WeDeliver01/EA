"""SPEC-04 §8: env-driven settings. No secrets in the repo - values come
from `.env` (git-ignored) or the process environment."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    agent_id: str
    account_id: str
    api_key: str
    hmac_secret: str
    backend_ws_url: str

    symbols: list[str] = Field(default_factory=lambda: ["XAUUSD"])

    mt5_login: int | None = None
    mt5_password: str | None = None
    mt5_server: str | None = None
    mt5_path: str | None = None

    heartbeat_interval_ms: int = 2000
    deal_poll_interval_seconds: float = 2.0
    deal_poll_window_minutes: int = 10

    state_db_path: Path = Path("agent_state.db")

    health_host: str = "127.0.0.1"
    health_port: int = 8799

    @field_validator("symbols", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [s.strip() for s in value.split(",") if s.strip()]
        return value
