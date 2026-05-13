from __future__ import annotations

import logging

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8001, alias="PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    scraper_url: str = Field(
        default="http://web-scraper:8000",
        alias="SCRAPER_URL",
        description="URL of the open-scraper service. Inside docker-compose use the service name.",
    )
    scraper_job_poll_interval_ms: int = Field(default=250, alias="SCRAPER_JOB_POLL_INTERVAL_MS")
    scraper_job_timeout_ms: int = Field(default=120_000, alias="SCRAPER_JOB_TIMEOUT_MS")

    workers: int = Field(
        default=2,
        alias="WORKERS",
        description="Number of parallel crawl jobs. Mirrors the scraper's WORKERS knob in semantics.",
    )
    queue_maxsize: int = Field(default=200, alias="QUEUE_MAXSIZE")
    job_timeout_ms: int = Field(default=3_600_000, alias="JOB_TIMEOUT_MS")

    max_retries: int = Field(default=3, alias="MAX_RETRIES")
    retry_http_codes: list[int] = Field(
        default_factory=lambda: [408, 429, 500, 502, 503, 504],
        alias="RETRY_HTTP_CODES",
    )
    retry_backoff_base: float = Field(default=2.0, alias="RETRY_BACKOFF_BASE")
    retry_backoff_max: float = Field(default=30.0, alias="RETRY_BACKOFF_MAX")

    session_max_error_score: float = Field(default=3.0, alias="SESSION_MAX_ERROR_SCORE")
    session_max_usage: int = Field(default=50, alias="SESSION_MAX_USAGE")
    session_blocked_codes: list[int] = Field(
        default_factory=lambda: [401, 403, 429],
        alias="SESSION_BLOCKED_CODES",
    )

    cors_allow_origins: list[str] = Field(
        default_factory=lambda: ["*"],
        alias="CORS_ALLOW_ORIGINS",
    )


settings = Settings()


class LogTagFilter(logging.Filter):
    def __init__(self, tag: str) -> None:
        super().__init__()
        self.tag = tag

    def filter(self, record: logging.LogRecord) -> bool:
        record.tag = self.tag
        return True


def setup_logging(level: str = "INFO", tag: str | None = None) -> None:
    tag = tag or "C"
    level_value = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level_value)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setLevel(level_value)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | [%(tag)s] | %(name)s | %(message)s"
    ))
    handler.addFilter(LogTagFilter(tag))
    root.addHandler(handler)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
        lg.setLevel(level_value)

    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
