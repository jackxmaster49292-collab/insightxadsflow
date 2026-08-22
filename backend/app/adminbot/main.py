"""The Telegram control panel process.

Long-polls its **own** bot token. That token must differ from any forwarding
bot's: Telegram permits a single ``getUpdates`` consumer per token, and a second
one receives 409 Conflict — the admin bot and a forwarding listener would fight
over the same update stream.

Refuses to start without an allowlist, so a deployment cannot come up with an
open control panel.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.storage.redis import RedisStorage

from app import preflight
from app.adminbot import notifier
from app.adminbot.auth import AdminOnlyMiddleware
from app.adminbot.handlers import router
from app.config import get_settings
from app.db.session import dispose_engine
from app.logging_setup import configure_logging
from app.security.ratelimit import close_redis

log = structlog.get_logger(__name__)

#: The panel needs nothing else; narrowing this avoids pulling irrelevant traffic.
ALLOWED_UPDATES = ["message", "callback_query"]

_stop = asyncio.Event()


def build_dispatcher() -> Dispatcher:
    settings = get_settings()
    try:
        storage = RedisStorage.from_url(settings.redis_url)
    except Exception as exc:  # pragma: no cover - infrastructure path
        log.warning("fsm_storage_fallback", error=exc)
        from aiogram.fsm.storage.memory import MemoryStorage

        storage = MemoryStorage()  # type: ignore[assignment]

    dispatcher = Dispatcher(storage=storage)

    # Registered on both observers: a callback_query does not pass through the
    # message middleware, and missing it would leave every button unguarded.
    guard = AdminOnlyMiddleware()
    dispatcher.message.middleware(guard)
    dispatcher.callback_query.middleware(guard)

    dispatcher.include_router(router)
    return dispatcher


async def run() -> None:
    settings = get_settings()
    configure_logging(json_output=settings.environment != "local")

    await preflight.run()

    token = settings.require_admin_bot_token()

    if not settings.admin_ids:
        raise SystemExit(
            "ADMIN_TELEGRAM_IDS is empty, so nobody could use the panel and "
            "starting would only expose a bot that rejects everyone. Set it to "
            "your numeric Telegram user id (ask @userinfobot) and restart."
        )

    bot = Bot(token=token, default=DefaultBotProperties())
    alerts: asyncio.Task[None] | None = None

    # Everything after the Bot exists goes in the try, so a failure during
    # startup still closes the HTTP session instead of leaking it.
    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            raise SystemExit(
                "Telegram rejected ADMIN_BOT_TOKEN. Check the token from "
                "@BotFather, and make sure it has not been revoked."
            ) from None

        log.info(
            "adminbot_starting",
            username=me.username,
            admins=len(settings.admin_ids),
            provider=settings.telegram_provider,
        )

        alerts = asyncio.create_task(notifier.run(bot, _stop))
        await build_dispatcher().start_polling(bot, allowed_updates=ALLOWED_UPDATES)
    finally:
        _stop.set()
        if alerts is not None:
            alerts.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await alerts
        await bot.session.close()
        await close_redis()
        await dispose_engine()
        log.info("adminbot_stopped")


def main() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _stop.set)
    try:
        loop.run_until_complete(run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
