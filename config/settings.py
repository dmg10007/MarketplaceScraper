"""
Settings — loaded from .env via pydantic-settings.
"""

from typing import Optional
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Facebook session
    session_file: str = "data/fb_session.json"

    # Browser
    headless: bool = True
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    # Proxy — Phase 5: set PROXY_URL in .env to route traffic through a proxy
    # e.g. PROXY_URL=http://user:pass@proxyhost:8080
    # Leave blank to disable.
    proxy_url: Optional[str] = Field(default=None, alias="PROXY_URL")

    # Scanning
    scan_interval_minutes: int = 15
    default_zip: str = "27330"

    # Database
    db_path: str = "data/marketplace.db"

    # Dashboard
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        populate_by_name = True


settings = Settings()
