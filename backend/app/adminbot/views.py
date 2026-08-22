"""Screen rendering for the Telegram control panel.

Pure functions: state in, ``(text, keyboard)`` out. Keeping them free of I/O
means every screen is unit-testable without a bot, a network, or a database.

Three Telegram constraints shape the design:

* ``callback_data`` is limited to **1–64 bytes**, so callbacks carry ids only —
  never titles or filter text. A UUID plus a short verb already uses most of the
  budget, which is why paging is an integer and multi-select lives in FSM state
  rather than in the button.
* Messages are *edited* rather than re-sent as you navigate, so the chat stays a
  single panel instead of an endless scroll.
* Everything interpolated into a screen is escaped for MarkdownV2. Chat titles
  are attacker-influenced — someone can name a group ``*bold*`` — and a single
  unescaped character makes Telegram reject the whole message with a 400.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db.models import (
    AutoReply,
    Broadcast,
    BroadcastStatus,
    ForwardingRule,
    JobStatus,
    RuleStatus,
    TelegramConnection,
)
from app.domain import reasons

PAGE_SIZE = 6
#: Groups per page in the picker. Smaller than PAGE_SIZE because each row also
#: carries a tick box and the titles run longer.
PICKER_PAGE_SIZE = 8

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
    "leased": "🚚",
    "sending": "📣",
    "completed": "✅",
    "cancelled": "🚫",
    "scheduled": "🕒",
}


def icon(status: str) -> str:
    return STATUS_ICON.get(status, "•")


@dataclass(frozen=True, slots=True)
class Screen:
    text: str
    keyboard: InlineKeyboardMarkup


def _rows(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


def _home_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="🏠 Home", callback_data="nav:home")]


def _back(target: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="⬅️ Back", callback_data=target)]


def _pager(prefix: str, page: int, pages: int) -> list[InlineKeyboardButton]:
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"{prefix}{page - 1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"{prefix}{page + 1}"))
    return nav


def _page_of(items: Sequence, page: int, size: int) -> tuple[Sequence, int, int]:  # type: ignore[type-arg]
    pages = max(1, -(-len(items) // size))
    page = max(0, min(page, pages - 1))
    return items[page * size : (page + 1) * size], page, pages


# --------------------------------------------------------------------------- #
# Home
# --------------------------------------------------------------------------- #
def home(
    *,
    connections: Sequence[TelegramConnection],
    rules: Sequence[ForwardingRule],
    broadcasts: Sequence[Broadcast],
    counts: dict[str, int],
) -> Screen:
    active_rules = [r for r in rules if r.status is RuleStatus.active]
    paused_rules = [r for r in rules if r.status is RuleStatus.paused]
    healthy = [c for c in connections if c.status.value == "active"]
    sending = [b for b in broadcasts if b.status is BroadcastStatus.sending]

    lines = ["📡 *InsightAdFlow*", ""]

    if not connections:
        lines += [
            "No Telegram account or bot is connected yet\\.",
            "",
            "Tap *Accounts* to add one\\. Everything else unlocks after that\\.",
        ]
    else:
        lines.append(f"*Accounts* — {len(healthy)}/{len(connections)} working")
        for connection in connections[:4]:
            lines.append(
                f"  {icon(connection.status.value)} {escape(connection.label)} "
                f"\\({connection.kind.value}\\)"
            )
        lines += [
            "",
            f"*Ads* — {len(sending)} sending, {len(broadcasts)} total",
            f"*Forwarding* — {len(active_rules)} active, {len(paused_rules)} paused",
            "",
            f"*Last 24h* — {counts.get('forwarded', 0)} sent · "
            f"{counts.get('skipped', 0)} skipped · {counts.get('failed', 0)} failed",
        ]

    if paused_rules:
        lines += ["", "⚠️ Some rules are paused and need attention\\."]

    return Screen(
        "\n".join(lines),
        _rows(
            [InlineKeyboardButton(text="📣 Ads", callback_data="nav:ads:0")],
            [
                InlineKeyboardButton(text="💬 Auto-reply", callback_data="nav:autoreply"),
                InlineKeyboardButton(text="📋 Forwarding", callback_data="nav:rules:0"),
            ],
            [
                InlineKeyboardButton(text="🔗 Accounts", callback_data="nav:conns"),
                InlineKeyboardButton(text="💭 Groups", callback_data="nav:chats:0"),
            ],
            [
                InlineKeyboardButton(text="📊 Activity", callback_data="nav:activity"),
                InlineKeyboardButton(text="🔄 Refresh", callback_data="nav:home"),
            ],
        ),
    )


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #
def connections_list(*, connections: Sequence[TelegramConnection]) -> Screen:
    if not connections:
        lines = [
            "🔗 *Accounts*",
            "",
            "Nothing connected yet\\.",
            "",
            "*Account* — your own Telegram account, added with your phone number\\. "
            "Posts as you, into groups you have already joined\\.",
            "",
            "*Bot* — a bot from @BotFather\\. It can only post where you have added "
            "it as an administrator\\.",
        ]
    else:
        lines = ["🔗 *Accounts*", ""]
        for connection in connections:
            lines.append(
                f"{icon(connection.status.value)} *{escape(connection.label)}* "
                f"— {connection.kind.value}, {connection.status.value}"
            )
            if connection.telegram_username:
                lines.append(f"   @{escape(connection.telegram_username)}")
            if connection.last_error_message_safe:
                lines.append(f"   ⚠️ {escape(connection.last_error_message_safe)}")
            lines.append("")

    buttons = [
        [
            InlineKeyboardButton(
                text=f"⚙️ {connection.label[:28]}",
                callback_data=f"conn:{connection.id}",
            )
        ]
        for connection in connections[:8]
    ]

    return Screen(
        "\n".join(lines),
        InlineKeyboardMarkup(
            inline_keyboard=[
                *buttons,
                [
                    InlineKeyboardButton(text="➕ Add account", callback_data="add:user"),
                    InlineKeyboardButton(text="➕ Add bot", callback_data="add:bot"),
                ],
                _home_row(),
            ]
        ),
    )


def connection_detail(*, connection: TelegramConnection, chat_count: int) -> Screen:
    lines = [
        f"{icon(connection.status.value)} *{escape(connection.label)}*",
        "",
        f"*Type* — {connection.kind.value}",
        f"*Status* — {connection.status.value}",
        f"*Groups known* — {chat_count}",
    ]
    if connection.telegram_username:
        lines.append(f"*Telegram* — @{escape(connection.telegram_username)}")
    if connection.last_error_message_safe:
        lines += ["", f"⚠️ {escape(connection.last_error_message_safe)}"]
    if chat_count == 0:
        lines += [
            "",
            "_No groups yet\\. Tap Sync groups — it reads the groups this account "
            "has already joined\\. It never joins anything for you\\._",
        ]

    return Screen(
        "\n".join(lines),
        _rows(
            [
                InlineKeyboardButton(
                    text="🔄 Sync groups", callback_data=f"conn:{connection.id}:sync"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🩺 Check health", callback_data=f"conn:{connection.id}:health"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Disconnect", callback_data=f"conn:{connection.id}:askdel"
                )
            ],
            _back("nav:conns"),
        ),
    )


def confirm_disconnect(*, connection: TelegramConnection) -> Screen:
    return Screen(
        f"🗑 *Disconnect {escape(connection.label)}?*\n\n"
        "This signs the connection out and deletes its stored session\\. "
        "Forwarding rules and ads that use it will stop\\.\n\n"
        "Messages already delivered stay where they are — disconnecting cannot "
        "unsend anything\\.",
        _rows(
            [
                InlineKeyboardButton(
                    text="Yes, disconnect", callback_data=f"conn:{connection.id}:delete"
                )
            ],
            [InlineKeyboardButton(text="Cancel", callback_data=f"conn:{connection.id}")],
        ),
    )


# --------------------------------------------------------------------------- #
# Ads (broadcasts)
# --------------------------------------------------------------------------- #
def ads_list(*, broadcasts: Sequence[Broadcast], page: int, can_create: bool) -> Screen:
    if not broadcasts:
        lines = [
            "📣 *Ads*",
            "",
            "Write your own message and post it to the groups you choose\\.",
            "",
            "Nothing here yet\\.",
        ]
        if not can_create:
            lines += ["", "Connect an account first — *Accounts* on the home screen\\."]
        return Screen(
            "\n".join(lines),
            _rows(
                [InlineKeyboardButton(text="➕ New ad", callback_data="ad:new")]
                if can_create
                else [],
                _home_row(),
            ),
        )

    window, page, pages = _page_of(broadcasts, page, PAGE_SIZE)
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{icon(b.status.value)} {b.name[:36]}",
                callback_data=f"ad:{b.id}",
            )
        ]
        for b in window
    ]

    return Screen(
        f"📣 *Ads* \\({len(broadcasts)}\\)",
        InlineKeyboardMarkup(
            inline_keyboard=[
                *buttons,
                _pager("nav:ads:", page, pages),
                [InlineKeyboardButton(text="➕ New ad", callback_data="ad:new")]
                if can_create
                else [],
                _home_row(),
            ]
        ),
    )


def ad_compose(*, broadcast: Broadcast, target_count: int, estimate_s: float) -> Screen:
    """The draft screen. Every field stays editable until Send is tapped."""
    has_media = broadcast.media_kind.value != "none"
    body = broadcast.body_text.strip()

    lines = [
        f"📝 *{escape(broadcast.name)}*",
        "",
        "*Message*",
        f"_{escape(body[:400])}_" if body else "_not written yet_",
    ]
    if len(body) > 400:
        lines.append(f"_…and {len(body) - 400} more characters_")

    lines += [
        "",
        f"*Image* — {'attached' if has_media else 'none'}",
        f"*Groups* — {target_count} selected",
        f"*Pause between groups* — {seconds_label(broadcast.delay_ms)}",
    ]
    if target_count:
        lines.append(f"*Takes about* — {escape(humanize(estimate_s))}")

    ready = bool(body or has_media) and target_count > 0
    if not ready:
        missing = []
        if not body and not has_media:
            missing.append("a message or an image")
        if not target_count:
            missing.append("at least one group")
        lines += ["", f"Still needed: {escape(' and '.join(missing))}\\."]

    return Screen(
        "\n".join(lines),
        _rows(
            [InlineKeyboardButton(text="✏️ Message", callback_data=f"ad:{broadcast.id}:text")],
            [
                InlineKeyboardButton(text="🖼 Image", callback_data=f"ad:{broadcast.id}:media"),
                InlineKeyboardButton(text="⏱ Pause", callback_data=f"ad:{broadcast.id}:delay"),
            ],
            [
                InlineKeyboardButton(
                    text=f"💭 Groups ({target_count})",
                    callback_data=f"ad:{broadcast.id}:pick:0",
                )
            ],
            [InlineKeyboardButton(text="🚀 Send now", callback_data=f"ad:{broadcast.id}:confirm")]
            if ready
            else [],
            [
                InlineKeyboardButton(text="🗑 Discard", callback_data=f"ad:{broadcast.id}:discard"),
                InlineKeyboardButton(text="⬅️ Ads", callback_data="nav:ads:0"),
            ],
        ),
    )


def ad_confirm(*, broadcast: Broadcast, target_count: int, estimate_s: float) -> Screen:
    has_media = broadcast.media_kind.value != "none"
    preview = broadcast.body_text.strip()[:300] or "(image only)"
    return Screen(
        "🚀 *Send this ad?*\n\n"
        f"_{escape(preview)}_\n\n"
        f"*To* — {target_count} groups\n"
        f"*Image* — {'yes' if has_media else 'no'}\n"
        f"*Pause between groups* — {seconds_label(broadcast.delay_ms)}\n"
        f"*Takes about* — {escape(humanize(estimate_s))}\n\n"
        "It posts only to groups this account has already joined\\. "
        "You can pause it once it starts, but messages already posted cannot "
        "be unsent\\.",
        _rows(
            [InlineKeyboardButton(text="Yes, send", callback_data=f"ad:{broadcast.id}:send")],
            [InlineKeyboardButton(text="Cancel", callback_data=f"ad:{broadcast.id}")],
        ),
    )


def ad_detail(*, broadcast: Broadcast, counts: dict[str, int], target_count: int) -> Screen:
    done = counts.get("succeeded", 0)
    lines = [
        f"{icon(broadcast.status.value)} *{escape(broadcast.name)}*",
        "",
        f"*Status* — {broadcast.status.value}",
    ]
    if broadcast.paused_reason_code:
        lines.append(f"*Reason* — {escape(reasons.describe(broadcast.paused_reason_code))}")

    lines += [
        f"*Progress* — {done}/{target_count} groups",
        "",
        f"_{escape(broadcast.body_text.strip()[:300] or '(image only)')}_",
    ]

    if counts:
        lines += [
            "",
            "*Deliveries* — " + " · ".join(f"{icon(s)} {c} {s}" for s, c in sorted(counts.items())),
        ]

    controls: list[InlineKeyboardButton] = []
    if broadcast.status is BroadcastStatus.sending:
        controls.append(
            InlineKeyboardButton(text="⏸ Pause", callback_data=f"ad:{broadcast.id}:pause")
        )
    elif broadcast.status is BroadcastStatus.paused:
        controls.append(
            InlineKeyboardButton(text="▶️ Resume", callback_data=f"ad:{broadcast.id}:resume")
        )

    unfinished = (
        counts.get("failed", 0) + counts.get("dead_letter", 0) + counts.get("needs_attention", 0)
    )
    if unfinished:
        controls.append(
            InlineKeyboardButton(
                text=f"🔁 Retry {unfinished}", callback_data=f"ad:{broadcast.id}:retry"
            )
        )

    stoppable = broadcast.status in (BroadcastStatus.sending, BroadcastStatus.paused)
    return Screen(
        "\n".join(lines),
        _rows(
            controls,
            [InlineKeyboardButton(text="📊 Events", callback_data=f"ad:{broadcast.id}:events")],
            [InlineKeyboardButton(text="🚫 Stop", callback_data=f"ad:{broadcast.id}:cancel")]
            if stoppable
            else [],
            [InlineKeyboardButton(text="⬅️ Ads", callback_data="nav:ads:0"), *_home_row()],
        ),
    )


# --------------------------------------------------------------------------- #
# Group picker — shared by ads and forwarding rules
# --------------------------------------------------------------------------- #
#: Callback stem for every picker button. Deliberately two characters: see the
#: note on indices below.
PICK = "pk:"


def group_picker(
    *,
    chats: Sequence,  # type: ignore[type-arg]
    selected: set[uuid.UUID],
    page: int,
    title: str,
    hint: str,
    done_callback: str,
) -> Screen:
    """A paged, tick-box list of groups.

    Buttons address a chat by its **index** in ``chats``, not by its id. A
    UUID-carrying callback like ``ad:<32 hex>:t<32 hex>`` is 69 bytes and
    Telegram rejects the whole keyboard above 64. An index keeps every callback
    under 10 bytes regardless of how many groups there are.

    That makes ``chats`` order load-bearing: the caller stores the same ordered
    id list in FSM state and resolves the index against it, so a stale keyboard
    from an earlier ordering cannot silently toggle the wrong group.
    """
    if not chats:
        return Screen(
            f"💭 *{escape(title)}*\n\n"
            "No groups available yet\\.\n\n"
            "Open *Accounts*, choose the connection, and tap *Sync groups*\\. "
            "That reads the groups the account has already joined — it never "
            "joins anything for you\\.",
            _rows(_back(done_callback), _home_row()),
        )

    window, page, pages = _page_of(chats, page, PICKER_PAGE_SIZE)
    offset = page * PICKER_PAGE_SIZE
    lines = [
        f"💭 *{escape(title)}*",
        "",
        escape(hint),
        "",
        f"*{len(selected)} selected* of {len(chats)}",
    ]

    buttons = [
        [
            InlineKeyboardButton(
                text=f"{'☑️' if chat.id in selected else '⬜️'} {chat.title[:34]}",
                callback_data=f"{PICK}t{offset + index}",
            )
        ]
        for index, chat in enumerate(window)
    ]

    return Screen(
        "\n".join(lines),
        InlineKeyboardMarkup(
            inline_keyboard=[
                *buttons,
                _pager(f"{PICK}p", page, pages),
                [
                    InlineKeyboardButton(text="Select page", callback_data=f"{PICK}a{page}"),
                    InlineKeyboardButton(text="Clear all", callback_data=f"{PICK}n{page}"),
                ],
                [InlineKeyboardButton(text="✅ Done", callback_data=done_callback)],
            ]
        ),
    )


# --------------------------------------------------------------------------- #
# Auto-reply
# --------------------------------------------------------------------------- #
def autoreply_screen(*, connection: TelegramConnection | None, reply: AutoReply | None) -> Screen:
    if connection is None:
        return Screen(
            "💬 *Auto\\-reply*\n\nConnect an account first — *Accounts* on the home screen\\.",
            _rows(_home_row()),
        )

    body = (reply.body_text.strip() if reply else "") or ""
    enabled = bool(reply and reply.enabled)
    cooldown = reply.cooldown_s if reply else 86_400

    lines = [
        "💬 *Auto\\-reply*",
        "",
        f"*Account* — {escape(connection.label)}",
        f"*Status* — {'on ✅' if enabled else 'off'}",
        "",
        "*Reply*",
        f"_{escape(body[:400])}_" if body else "_not written yet_",
        "",
        f"*Same person again after* — {escape(humanize(cooldown))}",
        "",
        "Answers people who message this account first — for example someone who "
        "saw one of your ads and wrote to you\\.",
        "",
        "It never messages anyone who has not written to you, and never posts in a group\\.",
    ]

    toggle = (
        InlineKeyboardButton(text="⏸ Turn off", callback_data="ar:off")
        if enabled
        else InlineKeyboardButton(text="▶️ Turn on", callback_data="ar:on")
    )

    return Screen(
        "\n".join(lines),
        _rows(
            [InlineKeyboardButton(text="✏️ Edit reply", callback_data="ar:text")],
            [toggle] if body else [],
            [InlineKeyboardButton(text="⏱ Change wait", callback_data="ar:cooldown")],
            _home_row(),
        ),
    )


# --------------------------------------------------------------------------- #
# Forwarding rules
# --------------------------------------------------------------------------- #
def rules_list(*, rules: Sequence[ForwardingRule], page: int, can_create: bool) -> Screen:
    if not rules:
        lines = [
            "📋 *Forwarding*",
            "",
            "Copies new messages from a chat you follow into groups you choose\\.",
            "",
            "No rules yet\\.",
        ]
        if not can_create:
            lines += ["", "Connect an account first — *Accounts* on the home screen\\."]
        return Screen(
            "\n".join(lines),
            _rows(
                [InlineKeyboardButton(text="➕ New rule", callback_data="rule:new")]
                if can_create
                else [],
                _home_row(),
            ),
        )

    window, page, pages = _page_of(rules, page, PAGE_SIZE)
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{icon(rule.status.value)} {rule.name[:36]}",
                callback_data=f"rule:{rule.id}",
            )
        ]
        for rule in window
    ]

    return Screen(
        f"📋 *Forwarding rules* \\({len(rules)}\\)",
        InlineKeyboardMarkup(
            inline_keyboard=[
                *buttons,
                _pager("nav:rules:", page, pages),
                [InlineKeyboardButton(text="➕ New rule", callback_data="rule:new")]
                if can_create
                else [],
                _home_row(),
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
) -> Screen:
    lines = [
        f"{icon(rule.status.value)} *{escape(rule.name)}*",
        "",
        f"*Status* — {rule.status.value}",
    ]
    if rule.paused_reason_code:
        lines.append(f"*Reason* — {escape(reasons.describe(rule.paused_reason_code))}")

    lines += [
        f"*From* — {escape(', '.join(source_titles) or 'none')}",
        f"*To* — {destination_count} groups",
        f"*Pause between groups* — {rule.delay_ms} ms",
        "",
        "_" + escape(preview) + "_",
    ]

    if job_counts:
        lines += [
            "",
            "*Deliveries* — "
            + " · ".join(f"{icon(s)} {c} {s}" for s, c in sorted(job_counts.items())),
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
            [
                InlineKeyboardButton(
                    text=f"💭 Groups ({destination_count})",
                    callback_data=f"rule:{rule.id}:pick",
                )
            ],
            [
                InlineKeyboardButton(text="📊 Events", callback_data=f"rule:{rule.id}:events"),
                InlineKeyboardButton(text="🗑 Delete", callback_data=f"rule:{rule.id}:askdel"),
            ],
            [InlineKeyboardButton(text="⬅️ Rules", callback_data="nav:rules:0"), *_home_row()],
        ),
    )


def confirm_delete_rule(*, rule: ForwardingRule) -> Screen:
    return Screen(
        f"🗑 *Delete {escape(rule.name)}?*\n\n"
        "The rule and its delivery history go away\\. Messages it already "
        "forwarded stay where they are\\.",
        _rows(
            [InlineKeyboardButton(text="Yes, delete", callback_data=f"rule:{rule.id}:delete")],
            [InlineKeyboardButton(text="Cancel", callback_data=f"rule:{rule.id}")],
        ),
    )


# --------------------------------------------------------------------------- #
# Groups and activity
# --------------------------------------------------------------------------- #
def chats_list(*, chats: Sequence, page: int) -> Screen:  # type: ignore[type-arg]
    if not chats:
        return Screen(
            "💭 *Groups*\n\nNone yet\\.\n\n"
            "Open *Accounts*, pick a connection, and tap *Sync groups*\\.",
            _rows(_home_row()),
        )

    window, page, pages = _page_of(chats, page, PAGE_SIZE)
    lines = [f"💭 *Groups* \\({len(chats)}\\)", ""]
    for chat in window:
        access = chat.access
        can_post = access and access.can_post_destination
        can_read = access and access.can_read_source
        lines.append(f"*{escape(chat.title[:40])}*")
        lines.append(f"   post {'✅' if can_post else '—'}   read {'✅' if can_read else '—'}")
        if access and not can_post:
            lines.append(f"   _{escape(reasons.describe(access.destination_reason_code))}_")

    return Screen(
        "\n".join(lines),
        InlineKeyboardMarkup(inline_keyboard=[_pager("nav:chats:", page, pages), _home_row()]),
    )


def activity(*, events: Sequence, back: str = "nav:home") -> Screen:  # type: ignore[type-arg]
    if not events:
        return Screen("📊 *Activity*\n\nNothing recorded yet\\.", _rows(_home_row()))

    lines = ["📊 *Recent activity*", ""]
    for event in events[:12]:
        when = event.occurred_at.strftime("%d %b %H:%M")
        lines.append(
            f"{icon(event.outcome.value)} `{when}` {escape(event.detail_safe or event.reason_code)}"
        )

    return Screen("\n".join(lines), _rows(_back(back), _home_row()))


def job_counts(statuses: Sequence[JobStatus]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for status in statuses:
        counts[status.value] = counts.get(status.value, 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def seconds_label(milliseconds: int) -> str:
    """A pause, escaped and ready to interpolate.

    "3.0s" contains a "." which MarkdownV2 requires escaped; forgetting it makes
    Telegram reject the whole message with a 400, so the screen never appears at
    all. Formatting and escaping happen together so they cannot drift apart.
    """
    return escape(f"{milliseconds / 1000:.1f}s")


def humanize(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} seconds"
    if seconds < 3600:
        return f"{int(seconds // 60)} minutes"
    if seconds < 86_400:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 86_400:.1f} days"


_MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!\\"


def escape(text: str) -> str:
    """Escape for Telegram MarkdownV2.

    Chat titles are attacker-influenced — someone can name a group `*bold*` or
    worse — so anything interpolated into a screen goes through here. An
    unescaped title makes Telegram reject the whole message with a 400.

    Public because the handlers build prompts too, and every one of them needs
    the same treatment.
    """
    out: list[str] = []
    for char in text:
        if char in _MDV2_SPECIALS:
            out.append("\\")
        out.append(char)
    return "".join(out)


def as_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def parse_callback(data: str) -> tuple[str, str | None, str | None]:
    """``"rule:<uuid>:pause"`` → ``("rule", "<uuid>", "pause")``."""
    parts = data.split(":")
    kind = parts[0]
    ident = parts[1] if len(parts) > 1 else None
    action = parts[2] if len(parts) > 2 else None
    return kind, ident, action
