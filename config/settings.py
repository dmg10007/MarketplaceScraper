"""
Settings — loaded from .env via pydantic-settings.
All secrets and tunables live here. Never hardcode values elsewhere.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Facebook credentials ---
    fb_email: str = Field(..., description="Facebook account email for the bot account")
    fb_password: str = Field(..., description="Facebook account password")

    # --- Telegram ---
    telegram_bot_token: str = Field(..., description="Token from BotFather")
    telegram_chat_id: str = Field(..., description="Your personal chat ID to receive alerts")

    # --- Scraper behaviour ---
    scan_interval_minutes: int = Field(default=15, ge=5, le=60,
                                       description="How often to run all searches (minutes)")
    request_delay_min: float = Field(default=2.0, description="Min seconds between page requests")
    request_delay_max: float = Field(default=8.0, description="Max seconds between page requests")
    max_concurrent_searches: int = Field(default=5, ge=1, le=10,
                                          description="Max searches running in parallel")
    headless: bool = Field(default=False,
                           description="Run browser headless. False = stealthier on Windows.")
    session_file: str = Field(default="data/fb_session.json",
                              description="Path to persisted browser cookies/storage")

    # --- Database ---
    db_path: str = Field(default="data/marketplace.db",
                         description="Path to the SQLite database file")

    # --- Proxy (optional — leave blank to use raw IP) ---
    proxy_url: Optional[str] = Field(default=None,
                                     description="e.g. http://user:pass@host:port — leave blank to skip")

    # --- Web dashboard ---
    dashboard_host: str = Field(default="127.0.0.1")
    dashboard_port: int = Field(default=8080)
    dashboard_secret_key: str = Field(default="change-me-in-production",
                                      description="Used to sign session cookies")

    # --- Health monitoring ---
    health_alert_threshold_minutes: int = Field(
        default=90,
        description="Alert via Telegram if no successful run in this many minutes"
    )


# Singleton — import this everywhere
settings = Settings()
