"""
Settings — loaded from .env via pydantic-settings.

All fields that may exist in .env are declared here.
extra = 'ignore' so unknown .env keys never cause crashes.
"""

from typing import Optional
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Facebook credentials (used by setup_session.py)
    fb_email: str = ""
    fb_password: str = ""

    # Facebook session
    session_file: str = "data/fb_session.json"

    # Browser
    headless: bool = True
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    # Proxy — set PROXY_URL in .env to route browser traffic through a proxy
    proxy_url: Optional[str] = Field(default=None, alias="PROXY_URL")

    # Scanning
    scan_interval_minutes: int = 15
    default_zip: str = "27330"
    request_delay_min: float = 2.0
    request_delay_max: float = 8.0
    max_concurrent_searches: int = 5
    health_alert_threshold_minutes: int = 90

    # Database
    db_path: str = "data/marketplace.db"

    # Dashboard
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    dashboard_secret_key: str = "change-me-to-a-random-string"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        populate_by_name = True
        extra = "ignore"  # silently ignore any .env keys not declared above


settings = Settings()
