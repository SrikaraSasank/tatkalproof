from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration. Every field can be overridden by an env var
    of the same name (case-insensitive) or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "TatkalProof"

    # Infrastructure
    database_url: str = "postgresql+asyncpg://tatkal:tatkal@localhost:5432/tatkal"
    # Use "fake://" to run with an in-process fake Redis (tests / quick demos).
    redis_url: str = "redis://localhost:6379/0"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    auto_create_tables: bool = True

    # Auth
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 180
    bcrypt_rounds: int = 12
    admin_token: str = "admin-secret"

    # Booking rules
    hold_ttl_seconds: int = 300
    max_active_holds_per_user: int = 4
    # Simulated "real work" (pricing, fraud checks...) inside the naive flow.
    # This is the window in which the race condition happens.
    naive_processing_delay_ms: int = 50
    # Simulated payment gateway latency.
    payment_delay_ms: int = 100

    # Traffic controls (defaults; can be flipped at runtime via /admin/flags)
    rate_limit_enabled: bool = True
    rate_limit_per_second: int = 5
    waiting_room_enabled: bool = False
    waiting_room_admit_per_second: int = 200
    waiting_room_pass_ttl_seconds: int = 600

    # Background workers (expired-hold sweeper + waiting-room admitter)
    run_background_workers: bool = True
    sweeper_interval_seconds: float = 2.0
    idempotency_key_ttl_hours: int = 24


@lru_cache
def get_settings() -> Settings:
    return Settings()
