from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    http_timeout: int
    port: int
    log_level: str
    price_update_interval: float = 1.0
    database_url: str | None = None
    spread_history_interval_seconds: int | None = None
    # Comma-separated exchange names to use, e.g. "bybit,binance,mexc". Empty = all.
    enabled_exchanges: str = ""
    # CORS: comma-separated origins, e.g. "https://myapp.com,http://localhost:5173". Use "*" to allow all.
    cors_origins: str = "*"
