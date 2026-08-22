"""Application configuration.

Every secret arrives through the environment. Nothing here is ever logged: the
redaction filter in ``app.security.redaction`` denylists these field names.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TelegramProvider = Literal["mock", "live"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Core infrastructure ------------------------------------------------
    database_url: str = "postgresql+asyncpg://insight:insight@localhost:5432/insight"
    redis_url: str = "redis://localhost:6379/0"
    app_secret_key: str = "dev-only-change-me"  # noqa: S105 - overridden by env
    panel_origin: str = "http://localhost:8080"
    environment: Literal["local", "ci", "production"] = "local"

    # --- Envelope encryption ------------------------------------------------
    # Base64 32 bytes. Lives only in the environment, never in the database, so a
    # database backup alone cannot decrypt session material.
    encryption_kek: str = base64.b64encode(b"0" * 32).decode()
    encryption_kek_version: int = 1

    # --- Admin surface (Telegram bot + Mini App) ----------------------------
    #: The bot that *is* the control panel. Must be a DIFFERENT token from any
    #: forwarding bot: Telegram allows only one getUpdates consumer per token
    #: and a second one receives 409 Conflict.
    admin_bot_token: str | None = None
    #: Only these Telegram user ids may use the panel. Empty means nobody —
    #: failing closed, so a misconfigured deploy is locked rather than open.
    admin_telegram_ids: str = ""
    #: HTTPS origin serving the Mini App. Telegram requires HTTPS.
    miniapp_url: str = ""
    #: How long a Mini App launch payload stays valid.
    miniapp_max_age_s: int = 86_400

    # --- Telegram -----------------------------------------------------------
    # "mock" is the default so an unconfigured process can never reach Telegram.
    telegram_provider: TelegramProvider = "mock"
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None

    # --- Operational safety controls (not monetization limits) --------------
    worker_concurrency: int = 8
    per_connection_inflight: int = 2
    album_buffer_ms: int = 2000
    bot_poll_timeout_s: int = 25
    safety_pause_threshold: int = 5
    max_attempts: int = 5
    max_rule_delay_ms: int = 3_600_000
    #: Bound on how many destinations one rule may fan out to. An operational
    #: safety control, not a monetization limit: one source message creates this
    #: many durable jobs at once. Raise it if you genuinely have more groups.
    max_destinations_per_rule: int = 500
    #: Cap on how far the LAST delivery of one message may be pushed out.
    #: delay_ms multiplies by destination count, so a 1-hour delay across 500
    #: destinations would otherwise schedule the tail 21 days into the future.
    max_rule_spread_s: int = 6 * 3600
    max_sources_per_rule: int = 100
    flood_wait_pause_threshold_s: int = 300
    lease_seconds: int = 120
    event_retention_days: int = 90

    # --- Sessions -----------------------------------------------------------
    session_idle_ttl_s: int = 60 * 60 * 12
    session_absolute_ttl_s: int = 60 * 60 * 24 * 14
    cookie_name: str = "insight_session"
    cookie_secure: bool = True

    sentry_dsn: str | None = Field(default=None)

    @field_validator("encryption_kek")
    @classmethod
    def _kek_is_32_bytes(cls, v: str) -> str:
        try:
            raw = base64.b64decode(v, validate=True)
        except Exception as exc:  # pragma: no cover - config error path
            raise ValueError("ENCRYPTION_KEK must be valid base64") from exc
        if len(raw) != 32:
            raise ValueError("ENCRYPTION_KEK must decode to exactly 32 bytes")
        return v

    @property
    def kek_bytes(self) -> bytes:
        return base64.b64decode(self.encryption_kek)

    @property
    def live_telegram(self) -> bool:
        return self.telegram_provider == "live"

    @property
    def admin_ids(self) -> frozenset[int]:
        """Parsed allowlist. Anything unparseable is dropped rather than
        guessed — a typo must not widen access."""
        parsed: set[int] = set()
        for chunk in self.admin_telegram_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk.lstrip("-").isdigit():
                parsed.add(int(chunk))
        return frozenset(parsed)

    def is_admin(self, telegram_user_id: int) -> bool:
        return telegram_user_id in self.admin_ids

    def require_admin_bot_token(self) -> str:
        if not self.admin_bot_token:
            raise RuntimeError(
                "ADMIN_BOT_TOKEN is required for the Telegram control panel. "
                "Create a separate bot with @BotFather — it must not be the same "
                "token as any forwarding bot."
            )
        return self.admin_bot_token

    def require_mtproto_credentials(self) -> tuple[int, str]:
        if self.telegram_api_id is None or not self.telegram_api_hash:
            raise RuntimeError(
                "TELEGRAM_API_ID and TELEGRAM_API_HASH are required for user-account "
                "connections. Obtain them from https://my.telegram.org"
            )
        return self.telegram_api_id, self.telegram_api_hash


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
