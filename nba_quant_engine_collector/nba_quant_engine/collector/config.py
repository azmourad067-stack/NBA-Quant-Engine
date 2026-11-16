from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    dunkapi_key: str
    supabase_db_url: str
    dunkapi_base_url: str = "https://mcp.dunkapi.com"
    request_timeout_seconds: int = 60
    max_credits_per_run: int = 350
    request_interval_seconds: float = 1.05
    database_schema: str = "nba"

    @classmethod
    def from_env(cls, env_file: str | None = None) -> "Settings":
        if env_file:
            load_dotenv(Path(env_file), override=False)
        else:
            load_dotenv(override=False)

        key = os.getenv("DUNKAPI_API_KEY", "").strip()
        db_url = os.getenv("SUPABASE_DB_URL", "").strip()
        if not key:
            raise ValueError("DUNKAPI_API_KEY is missing. Add it to .env.")
        if not db_url:
            raise ValueError("SUPABASE_DB_URL is missing. Add the PostgreSQL URI from Supabase Connect.")

        return cls(
            dunkapi_key=key,
            supabase_db_url=db_url,
            dunkapi_base_url=os.getenv("DUNKAPI_BASE_URL", "https://mcp.dunkapi.com").rstrip("/"),
            request_timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "60")),
            max_credits_per_run=int(os.getenv("MAX_CREDITS_PER_RUN", "350")),
            request_interval_seconds=float(os.getenv("REQUEST_INTERVAL_SECONDS", "1.05")),
            database_schema=os.getenv("NBA_DB_SCHEMA", "nba").strip() or "nba",
        )
