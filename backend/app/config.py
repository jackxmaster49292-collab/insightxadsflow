"""Application configuration.

Every secret arrives through the environment. Nothing here is ever logged: the
redaction filter in ``app.security.redaction`` denylists these field names.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TelegramProvider = Literal["mock", "live"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Core infrastructure ------------------------------------------------
    # Explicit DSN. Only for running outside Docker, where you control the whole
    # string. Left empty, the DSN is built from the parts below — which is safer,
    # because a password is *data* and must be percent-encoded before it can go
    # into a URL. A password containing "@" silently truncates the host: the URL
    # spec splits userinfo from host at the last "@", but libpq and asyncpg split
    # at the first, so the two disagree and the failure looks like a DNS problem.
    database_dsn_override: str = Field(
        default="",
        # DATABASE_URL stays the name people know and the escape hatch for
        # running outside Docker.
        validation_alias=AliasChoices("DATABASE_URL", "DATABASE_DSN_OVERRIDE"),
    )

    postgres_user: str = "insight"
    postgres_password: str = "insight"  # noqa: S105 - local default, overridden by env
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "insight"
    redis_url: str = "redis://localhost:6379/0"
    app_secret_key: str = "dev-only-change-me"  # noqa: S105 - overridden by env
    panel_origin: str = "http://localhost:8080"
    environment: Literal["local", "ci", "production"] = "local"

    # --- Envelope encryption ------------------------------------------------
    # Base64 32 bytes. Lives only in the environment, never in the database, so a
    # database backup alone cannot decrypt session material.
    encryption_kek: str = base64.b64encode(b"0" * 32).decode()
    encryption_kek_version: int = 1

    # --- Admin surface (the Telegram bot) -----------------------------------
    #: The bot that *is* the control panel. Must be a DIFFERENT token from any
    #: forwarding bot: Telegram allows only one getUpdates consumer per token
    #: and a second one receives 409 Conflict.
    admin_bot_token: str | None = None
    #: Telegram user ids of the **operators** — the people who run this
    #: deployment. They can see the user list and suspend an account. They
    #: cannot read anyone's ads, rules or connections; suspension does not need
    #: that and reading it would be a privacy breach.
    #:
    #: When access_mode is "closed" this doubles as the allowlist. Empty means
    #: nobody, so a misconfigured deploy is locked rather than open.
    admin_telegram_ids: str = ""

    #: Who may use the bot.
    #:
    #: "closed" — only the ids above. "open" — anyone who messages the bot gets
    #: an account, after accepting the terms.
    #:
    #: Defaults to "closed" deliberately: an existing deployment pulling this
    #: version must not silently become open to everyone who finds the bot.
    #: Opening it is a decision, so it is an explicit line in .env.
    access_mode: Literal["open", "closed"] = "closed"

    #: How many Telegram connections one person may hold.
    #:
    #: An operational control, not a product limit: every MTProto connection is
    #: a live Telethon client in the listener process, holding a socket and its
    #: own update state. This bounds what one account can pin down, the same way
    #: worker_concurrency bounds the worker.
    max_connections_per_user: int = 3

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

    # --- Broadcasts ---------------------------------------------------------
    #: Same fan-out bound as a rule, for the same reason: one broadcast creates
    #: this many durable rows at once.
    max_broadcast_targets: int = 500
    #: Default pacing between two group deliveries of one broadcast. Telegram
    #: documents ~20 messages per minute to the same group and ~30 messages per
    #: second overall; 3s keeps a single broadcast well inside both.
    broadcast_default_delay_ms: int = 3_000
    #: Longest message body a broadcast may carry. Telegram rejects a text
    #: message over 4096 characters, and a caption over 1024.
    max_broadcast_text_len: int = 4_096
    max_broadcast_caption_len: int = 1_024
    #: Ceiling on a broadcast image. The admin bot fetches it with getFile, and
    #: the Bot API refuses to serve a file larger than 20 MB.
    max_broadcast_media_bytes: int = 5 * 1024 * 1024

    #: Shortest gap between two rounds of a repeating ad.
    #:
    #: An operational floor, not a product limit. The same message arriving in
    #: the same group more often than this is what group admins and Telegram
    #: both read as spam — and the account that gets banned for it is the
    #: customer's, so the floor protects them rather than us.
    min_broadcast_repeat_s: int = 3600

    # --- Auto-reply ---------------------------------------------------------
    #: How long before the same person may receive another automatic reply.
    #: Not a throttle for our benefit — it is what keeps a reply from becoming
    #: repeat messaging to someone who did not ask for it.
    auto_reply_cooldown_s: int = 86_400

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
    def database_url(self) -> str:
        """The DSN the application actually connects with.

        Every component is percent-encoded, so a password may contain any
        character — ``@``, ``/``, ``:``, ``#`` — without corrupting the URL.
        """
        if self.database_dsn_override:
            return self.database_dsn_override
        user = quote(self.postgres_user, safe="")
        password = quote(self.postgres_password, safe="")
        return (
            f"postgresql+asyncpg://{user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

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
        """Is this person an operator of this deployment?

        Read from the environment on every call rather than stored on the user
        row, so removing an id from ``ADMIN_TELEGRAM_IDS`` revokes it at the
        next restart instead of leaving a stale flag in the database.
        """
        return telegram_user_id in self.admin_ids

    @property
    def open_access(self) -> bool:
        return self.access_mode == "open"

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
