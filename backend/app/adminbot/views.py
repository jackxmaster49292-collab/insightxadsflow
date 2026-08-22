"""Screen rendering for the Telegram control panel.

Pure functions: state in, ``(text, keyboard)`` out. Keeping them free of I/O
means every screen is unit-testable without a bot, a network, or a database.

Two Telegram constraints shape the design:

* ``callback_data`` is limited to **1–64 bytes**, so callbacks carry ids only —
  never titles or filter text.
* Messages are *edited* rather than re-sent as you navigate, so the chat stays a
  single panel instead of an endless scroll.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from app.db.models import ForwardingRule, JobStatus, RuleStatus, TelegramConnection
from app.domain import reasons

PAGE_SIZE = 6

STATUS_ICON = {
    "active": "✅",
    "paused": "⏸",
    "draft": "📝",
    "error": "❌",
    "disconnected": "🔌",
    "paused_safety": "🛑",
    "pending": "⏳",
    "awaiting_code": "⏳",
    "awaiting_2fa": "🔐",
    "succeeded": "✅",
    "failed": "❌",
    "skipped": "⏭",
    "needs_attention": "⚠️",
    "dead_letter": "💀",
    "forwarded": "✅",
    "retry_scheduled": "🔁",
}


def icon(status: str) -> str:
    return STATUS_ICON.get(status, "•")


@dataclass(frozen=True, slots=True)
class Screen:
    text: str
    keyboard: InlineKeyboardMarkup


def _rows(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


def _panel_button(miniapp_url: str, label: str = "⚙️ Open full panel") -> list[InlineKeyboardButton]:
    """Telegram only accepts an HTTPS URL for a web_app button, so the button is
    simply omitted when the Mini App is not configured — better than showing a
    control that fails when tapped."""
    if not miniapp_url.startswith("https://"):
        return []
    return [InlineKeyboardButton(text=label, web_app=WebAppInfo(url=miniapp_url))]


def home(
    *,
    connections: Sequence[TelegramConnection],
    rules: Sequence[ForwardingRule],
    counts: dict[str, int],
    miniapp_url: str,
) -> Screen:
    active = [r for r in rules if r.status is RuleStatus.active]
    paused = [r for r in rules if r.status is RuleStatus.paused]
    healthy = [c for c in connections if c.status.value == "active"]

    lines = ["📡 *Insight Store — Control Panel*", ""]

    if not connections:
        lines += [
            "No Telegram connection yet\\.",
            "",
            "Open the full panel to connect a bot or an account\\.",
        ]
    else:
        lines.append(f"*Connections* — {len(healthy)}/{len(connections)} healthy")
        for connection in connections[:4]:
            lines.append(
                f"  {icon(connection.status.value)} {_esc(connection.label)} "
                f"\\({connection.kind.value}\\)"
            )
        lines.append("")
        lines.append(f"*Rules* — {len(active)} active, {len(paused)} paused")
        lines.append("")
        lines.append(
            f"*Last 24h* — {counts.get('forwarded', 0)} forwarded · "
            f"{counts.get('skipped', 0)} skipped · {counts.get('failed', 0)} failed"
        )

    if paused:
        lines += ["", "⚠️ Some rules are paused and need attention\\."]

    return Screen(
        "\n".join(lines),
        _rows(
            [
                InlineKeyboardButton(text="📋 Rules", callback_data="nav:rules:0"),
                InlineKeyboardButton(text="🔗 Connections", callback_data="nav:conns"),
            ],
            [
                InlineKeyboardButton(text="💬 Chats", callback_data="nav:chats:0"),
                InlineKeyboardButton(text="📊 Activity", callback_data="nav:activity"),
            ],
            _panel_button(miniapp_url),
            [InlineKeyboardButton(text="🔄 Refresh", callback_data="nav:home")],
        ),
    )


def rules_list(*, rules: Sequence[ForwardingRule], page: int, miniapp_url: str) -> Screen:
    if not rules:
        return Screen(
            "📋 *Forwarding rules*\n\nNo rules yet\\. Create one in the full panel\\.",
            _rows(
                _panel_button(miniapp_url, "➕ Create a rule"),
                [InlineKeyboardButton(text="⬅️ Back", callback_data="nav:home")],
            ),
        )

    pages = max(1, -(-len(rules) // PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    window = rules[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    text = f"📋 *Forwarding rules* \\({len(rules)}\\)\n\nPage {page + 1} of {pages}"
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{icon(rule.status.value)} {rule.name[:40]}",
                callback_data=f"rule:{rule.id}",
            )
        ]
        for rule in window
    ]

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"nav:rules:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"nav:rules:{page + 1}"))

    return Screen(
        text,
        InlineKeyboardMarkup(
            inline_keyboard=[
                *buttons,
                *([nav] if nav else []),
                [InlineKeyboardButton(text="🏠 Home", callback_data="nav:home")],
            ]
        ),
    )


def rule_detail(
    *,
    rule: ForwardingRule,
    source_titles: Sequence[str],
    destination_count: int,
    job_counts: dict[str, int],
    preview: str,
    miniapp_url: str,
) -> Screen:
    lines = [
        f"{icon(rule.status.value)} *{_esc(rule.name)}*",
        "",
        f"*Status* — {rule.status.value}",
    ]
    if rule.paused_reason_code:
        lines.append(f"*Reason* — {_esc(reasons.describe(rule.paused_reason_code))}")

    lines += [
        f"*Sources* — {_esc(', '.join(source_titles) or 'none')}",
        f"*Destinations* — {destination_count}",
        f"*Delay* — {rule.delay_ms} ms",
        "",
        "_" + _esc(preview) + "_",
    ]

    if job_counts:
        lines += [
            "",
            "*Deliveries* — "
            + " · ".join(
                f"{icon(status)} {count} {status}" for status, count in sorted(job_counts.items())
            ),
        ]

    controls: list[InlineKeyboardButton] = []
    if rule.status is RuleStatus.active:
        controls.append(InlineKeyboardButton(text="⏸ Pause", callback_data=f"rule:{rule.id}:pause"))
    else:
        controls.append(
            InlineKeyboardButton(text="▶️ Resume", callback_data=f"rule:{rule.id}:resume")
        )
    controls.append(
        InlineKeyboardButton(text="🔁 Retry failed", callback_data=f"rule:{rule.id}:retry")
    )

    return Screen(
        "\n".join(lines),
        _rows(
            controls,
            [InlineKeyboardButton(text="📊 Recent events", callback_data=f"rule:{rule.id}:events")],
            _panel_button(miniapp_url, "✏️ Edit in full panel"),
            [
                InlineKeyboardButton(text="⬅️ Rules", callback_data="nav:rules:0"),
                InlineKeyboardButton(text="🏠 Home", callback_data="nav:home"),
            ],
        ),
    )


def connections_list(*, connections: Sequence[TelegramConnection], miniapp_url: str) -> Screen:
    if not connections:
        lines = [
            "🔗 *Telegram connections*",
            "",
            "None yet\\.",
            "",
            "Connect a bot or an account in the full panel\\. Credentials are "
            "entered there over HTTPS, never typed into this chat\\.",
        ]
    else:
        lines = ["🔗 *Telegram connections*", ""]
        for connection in connections:
            lines.append(
                f"{icon(connection.status.value)} *{_esc(connection.label)}* "
                f"— {connection.kind.value}, {connection.status.value}"
            )
            if connection.last_error_message_safe:
                lines.append(f"   ⚠️ {_esc(connection.last_error_message_safe)}")

    buttons = [
        [
            InlineKeyboardButton(
                text=f"🔄 Sync {connection.label[:24]}",
                callback_data=f"conn:{connection.id}:sync",
            )
        ]
        for connection in connections[:5]
    ]

    return Screen(
        "\n".join(lines),
        InlineKeyboardMarkup(
            inline_keyboard=[
                *buttons,
                _panel_button(miniapp_url, "⚙️ Manage connections"),
                [InlineKeyboardButton(text="🏠 Home", callback_data="nav:home")],
            ]
        ),
    )


def chats_list(*, chats: Sequence, page: int) -> Screen:  # type: ignore[type-arg]
    if not chats:
        return Screen(
            "💬 *Chats*\n\nNo chats yet\\. Synchronize a connection first\\.",
            _rows([InlineKeyboardButton(text="🏠 Home", callback_data="nav:home")]),
        )

    pages = max(1, -(-len(chats) // PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    window = chats[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    lines = [f"💬 *Chats* \\({len(chats)}\\)", "", f"Page {page + 1} of {pages}", ""]
    for chat in window:
        access = chat.access
        src = "✅" if access and access.can_read_source else "—"
        dst = "✅" if access and access.can_post_destination else "—"
        lines.append(f"*{_esc(chat.title[:40])}*")
        lines.append(f"   source {src}   destination {dst}")
        if access and not access.can_read_source:
            lines.append(f"   _{_esc(reasons.describe(access.source_reason_code))}_")

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"nav:chats:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"nav:chats:{page + 1}"))

    return Screen(
        "\n".join(lines),
        InlineKeyboardMarkup(
            inline_keyboard=[
                *([nav] if nav else []),
                [InlineKeyboardButton(text="🏠 Home", callback_data="nav:home")],
            ]
        ),
    )


def activity(*, events: Sequence, back: str = "nav:home") -> Screen:  # type: ignore[type-arg]
    if not events:
        return Screen(
            "📊 *Activity*\n\nNothing recorded yet\\.",
            _rows([InlineKeyboardButton(text="🏠 Home", callback_data="nav:home")]),
        )

    lines = ["📊 *Recent activity*", ""]
    for event in events[:12]:
        when = event.occurred_at.strftime("%d %b %H:%M")
        lines.append(
            f"{icon(event.outcome.value)} `{when}` {_esc(event.detail_safe or event.reason_code)}"
        )

    return Screen(
        "\n".join(lines),
        _rows(
            [InlineKeyboardButton(text="⬅️ Back", callback_data=back)],
            [InlineKeyboardButton(text="🏠 Home", callback_data="nav:home")],
        ),
    )


def job_counts(statuses: Sequence[JobStatus]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for status in statuses:
        counts[status.value] = counts.get(status.value, 0) + 1
    return counts


_MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!\\"


def _esc(text: str) -> str:
    """Escape for Telegram MarkdownV2.

    Chat titles are attacker-influenced — someone can name a channel `*bold*` or
    worse — so anything interpolated into a screen goes through here. An
    unescaped title makes Telegram reject the whole message with a 400.
    """
    out: list[str] = []
    for char in text:
        if char in _MDV2_SPECIALS:
            out.append("\\")
        out.append(char)
    return "".join(out)


def parse_callback(data: str) -> tuple[str, str | None, str | None]:
    """``"rule:<uuid>:pause"`` → ``("rule", "<uuid>", "pause")``."""
    parts = data.split(":")
    kind = parts[0]
    ident = parts[1] if len(parts) > 1 else None
    action = parts[2] if len(parts) > 2 else None
    return kind, ident, action


def as_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None
