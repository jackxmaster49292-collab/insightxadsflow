"""Test fixtures.

``TELEGRAM_PROVIDER`` is forced to ``mock`` before anything imports the app, and
a guard test asserts it. No automated test reaches real Telegram servers.
"""

from __future__ import annotations

import base64
import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from urllib.parse import quote

import pytest

os.environ.setdefault("TELEGRAM_PROVIDER", "mock")
os.environ.setdefault("ENCRYPTION_KEK", base64.b64encode(b"k" * 32).decode())
os.environ.setdefault("ENCRYPTION_KEK_VERSION", "1")
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-not-used-in-production")
os.environ.setdefault("COOKIE_SECURE", "false")
# Honour POSTGRES_PASSWORD so the suite still runs against a stack started with
# a custom password. Hardcoding "insight" here meant the tests broke for anyone
# who followed the deployment guide and set one.
# Percent-encode, for the same reason the application does: a password is data,
# and one containing "@" silently truncates the host.
_PW = quote(os.environ.get("POSTGRES_PASSWORD", "insight"), safe="")
os.environ.setdefault(
    "DATABASE_URL", f"postgresql+asyncpg://insight:{_PW}@localhost:5433/insight_test"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/1")

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.adapters.base import AccessReport, ChatRef, DiscoveredChat, PeerKind  # noqa: E402
from app.adapters.factory import mock_script_for, reset_mock_registry  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import models  # noqa: E402,F401
from app.db.base import Base  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator:
    """A dedicated test database, created and dropped around the session."""
    admin_url = get_settings().database_url.rsplit("/", 1)[0] + "/insight"
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.exec_driver_sql("DROP DATABASE IF EXISTS insight_test")
        await conn.exec_driver_sql("CREATE DATABASE insight_test")
    await admin.dispose()

    test_engine = create_async_engine(get_settings().database_url)
    async with test_engine.begin() as conn:
        await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS citext")
        await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        await conn.run_sync(Base.metadata.create_all)
    yield test_engine
    await test_engine.dispose()


@pytest.fixture(autouse=True)
async def clean_state(engine) -> AsyncIterator[None]:
    """Every test starts from an empty database, empty Redis, and a fresh mock.

    Redis is flushed because the rate limiter is real: without this, the
    register/login limits throttle the suite itself. Test Redis is db 1, kept
    separate from the development db.
    """
    async with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.exec_driver_sql(f'TRUNCATE TABLE "{table.name}" CASCADE')

    from app.security.ratelimit import get_redis

    # Redis is optional for pure-unit tests.
    with contextlib.suppress(Exception):
        await get_redis().flushdb()

    reset_mock_registry()
    yield


@pytest.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        yield db
        await db.rollback()


@pytest.fixture
async def app(engine):
    application = create_app()
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override() -> AsyncIterator[AsyncSession]:
        async with maker() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    application.dependency_overrides[get_session] = override
    return application


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test/api/v1") as http:
        yield http


class Actor:
    """A registered, signed-in user with CSRF wired up."""

    def __init__(self, http: AsyncClient, email: str, user_id: str) -> None:
        self.http = http
        self.email = email
        self.id = user_id

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"X-CSRF-Token": self.http.cookies.get("insight_csrf", "")}
        headers.update(extra or {})
        return headers

    async def post(self, url: str, json: dict | None = None, *, idem: str | None = None, **kw):
        extra = {"Idempotency-Key": idem} if idem else {}
        return await self.http.post(url, json=json, headers=self._headers(extra), **kw)

    async def patch(self, url: str, json: dict | None = None, **kw):
        return await self.http.patch(url, json=json, headers=self._headers(), **kw)

    async def delete(self, url: str, **kw):
        return await self.http.delete(url, headers=self._headers(), **kw)

    async def get(self, url: str, **kw):
        return await self.http.get(url, **kw)


async def register(http: AsyncClient, email: str | None = None) -> Actor:
    email = email or f"user-{uuid.uuid4().hex[:8]}@example.com"
    response = await http.post(
        "/auth/register", json={"email": email, "password": "correct-horse-battery"}
    )
    assert response.status_code == 201, response.text
    return Actor(http, email, response.json()["id"])


@pytest.fixture
async def actor(client) -> Actor:
    return await register(client)


@pytest.fixture
async def other_actor(app) -> AsyncIterator[Actor]:
    """A second user with an independent cookie jar, for isolation tests."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test/api/v1") as http:
        yield await register(http)


# --------------------------------------------------------------------------- #
# Mock Telegram helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def state():
    """A fresh conversation state for driving a bot flow directly.

    In conftest rather than in one test module, because three files now need
    it and the third copy is where they start to differ.
    """
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from tests.integration.test_bot_flows import ADMIN_CHAT

    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=ADMIN_CHAT, user_id=ADMIN_CHAT),
    )


def fake_broadcast(**overrides):
    """A stand-in for a Broadcast row, for testing screens without a database.

    Shared rather than rebuilt per test file: every one of these is a list of
    attributes a screen happens to read, so adding a column used to break four
    files at once with an AttributeError that said nothing about the change.
    """
    from types import SimpleNamespace

    fields = {
        "id": uuid.uuid4(),
        "name": "Ad",
        "status": models.BroadcastStatus.draft,
        "body_text": "hello",
        "body_entities": [],
        "media_kind": SimpleNamespace(value="none"),
        "delay_ms": 3000,
        "paused_reason_code": None,
        "repeat_every_s": None,
        "repeat_count": 0,
        "next_run_at": None,
        "scheduled_for": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def chat_ref(peer_id: int, kind: PeerKind = PeerKind.channel) -> ChatRef:
    return ChatRef(kind, peer_id)


def discovered(
    peer_id: int,
    title: str,
    *,
    chat_kind: str = "channel",
    protected: bool = False,
    posting: AccessReport | None = None,
    reading: AccessReport | None = None,
) -> DiscoveredChat:
    """One chat as a listing would return it.

    ``posting``/``reading`` left unset is the adapter saying it could not tell
    from the listing — a bot's, which learns chats from updates. A real account
    listing does tell, and passing them here is what exercises that path.
    """
    return DiscoveredChat(
        ref=chat_ref(peer_id),
        title=title,
        chat_kind=chat_kind,
        username=None,
        is_public=False,
        has_protected_content=protected,
        posting=posting,
        reading=reading,
    )


def script_for(connection_id: str | uuid.UUID):
    return mock_script_for(uuid.UUID(str(connection_id)))


async def connect_bot(actor: Actor, label: str = "Test bot") -> str:
    response = await actor.post(
        "/telegram/connections/bot",
        {"label": label, "bot_token": "123456789:AAEtestTokenValueThatIsLongEnough00"},
        idem=uuid.uuid4().hex,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def sync_with_chats(actor: Actor, connection_id: str, chats: list[DiscoveredChat]) -> None:
    """Populate the mock, enqueue a sync, and run it through the real worker path."""
    from sqlalchemy import select

    from app.db.models import ControlTask
    from app.db.session import session_scope
    from app.worker import process_control_task

    script = script_for(connection_id)
    script.chats = chats

    response = await actor.post(f"/telegram/connections/{connection_id}/sync")
    assert response.status_code == 202, response.text

    async with session_scope() as db:
        result = await db.execute(select(ControlTask).order_by(ControlTask.created_at.desc()))
        task = result.scalars().first()
    assert task is not None
    await process_control_task(task)
