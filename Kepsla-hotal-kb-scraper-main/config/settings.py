"""Application settings loaded from environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SQLITE_FILE_URL_PREFIX = "sqlite+aiosqlite:///"


def _resolve_project_path(path_value: str) -> str:
    """Resolve relative filesystem paths against the repository root."""
    path = Path(path_value)
    if path.is_absolute():
        return str(path)
    return str((PROJECT_ROOT / path).resolve())


def _resolve_sqlite_database_url(database_url: str) -> str:
    """Anchor local SQLite files to the repository root instead of process cwd."""
    if not database_url.startswith(_SQLITE_FILE_URL_PREFIX):
        return database_url

    sqlite_path = database_url[len(_SQLITE_FILE_URL_PREFIX) :]
    if sqlite_path in {"", ":memory:"}:
        return database_url

    path = Path(sqlite_path)
    if path.is_absolute():
        return database_url

    resolved_path = (PROJECT_ROOT / path).resolve()
    return f"{_SQLITE_FILE_URL_PREFIX}{resolved_path.as_posix()}"


class Settings(BaseSettings):
    """Central configuration for Hotel KB Scraper."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8-sig",
        extra="ignore",
    )

    # -- App --
    app_name: str = "Hotel KB Scraper"
    app_env: str = "development"
    app_port: int = 8501

    # -- Database --
    database_url: str = "sqlite+aiosqlite:///./scraper.db"

    # -- OpenAI (for LLM structuring) --
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    llm_max_tokens: int = 4000
    llm_temperature: float = 0.0
    max_concurrent_kb_generations: int = 4
    max_concurrent_property_expansions: int = 4
    max_concurrent_property_image_downloads: int = 3
    max_concurrent_property_packaging: int = 4

    # -- Background Worker --
    worker_poll_interval_seconds: float = 1.0
    worker_heartbeat_interval_seconds: float = 5.0
    worker_stale_after_seconds: int = 30
    worker_auto_start_enabled: bool = True
    worker_shutdown_timeout_seconds: int = 10
    phase1_job_max_attempts: int = 2
    publish_job_max_attempts: int = 2
    phase1_job_retry_backoff_seconds: tuple[int, ...] = (5, 15, 30)
    publish_job_retry_backoff_seconds: tuple[int, ...] = (5, 15, 30)

    # -- Scraper Limits --
    max_pages_per_site: int = 3000
    max_concurrent_crawls: int = 10
    crawl_delay_seconds: float = 0.5
    request_timeout_seconds: int = 30
    max_depth: int = 3
    enable_completeness_gate: bool = True
    completeness_min_score: float = 0.72
    completeness_max_recrawls_per_batch: int = 12
    completeness_recrawl_concurrency: int = 3

    # -- API Security --
    api_basic_auth_enabled: bool = False
    api_basic_auth_username: str = "admin"
    api_basic_auth_password: str = ""
    api_rate_limit_enabled: bool = True
    api_rate_limit_requests: int = 120
    api_rate_limit_window_seconds: int = 60

    # -- Metrics and Alerts --
    metrics_alert_window_seconds: int = 300
    metrics_alert_cooldown_seconds: int = 300
    metrics_alert_auth_failures_threshold: int = 5
    metrics_alert_rate_limit_threshold: int = 5
    metrics_alert_job_failures_threshold: int = 3

    # -- Per-site Retry Policies --
    site_retry_policies: dict[str, dict[str, object]] = {
        "*": {
            "attempts": 2,
            "backoff_seconds": (0.5, 1.5),
            "retry_statuses": (429, 500, 502, 503, 504),
        },
        "khil.com": {
            "attempts": 3,
            "backoff_seconds": (0.5, 1.0, 2.0),
            "retry_statuses": (403, 429, 500, 502, 503, 504),
        },
        "sarovarhotels.com": {
            "attempts": 3,
            "backoff_seconds": (0.5, 1.5, 3.0),
            "retry_statuses": (429, 500, 502, 503, 504),
        },
        "orchidhotel.com": {
            "attempts": 4,
            "backoff_seconds": (0.5, 1.0, 2.0, 4.0),
            "retry_statuses": (403, 429, 500, 502, 503, 504),
        },
    }

    # -- Proxy / IP Reputation Bypass --
    # Set a residential proxy URL to avoid Akamai/PerimeterX datacenter-IP blocks.
    # Supported formats:
    #   HTTP/HTTPS proxy:  http://user:pass@host:port
    #   SOCKS5 proxy:      socks5://user:pass@host:port
    # Applied to Playwright (Step 6) and Camoufox (Step 7) automatically.
    residential_proxy_url: str = ""

    # Bright Data Scraping Browser — CDP WebSocket URL.
    # When set, an extra Step 8 connects via Playwright CDP to Bright Data's
    # managed browser running on a residential IP with built-in unblocking.
    # Get this from the Bright Data dashboard → Scraping Browser → Access Parameters.
    # Format: wss://brd-customer-<id>-zone-<zone>:<pass>@brd.superproxy.io:9222
    bright_data_ws_url: str = ""

    # -- Output --
    output_dir: str = "./output"

settings = Settings()
settings.database_url = _resolve_sqlite_database_url(settings.database_url)
settings.output_dir = _resolve_project_path(settings.output_dir)
