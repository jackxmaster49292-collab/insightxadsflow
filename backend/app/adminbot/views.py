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
from datetime import UTC, datetime

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

#: How much of an ad a screen shows, measured **after** escaping. Telegram caps
#: a message at 4096 characters, and escaping can nearly double a length before
#: it is counted, so budgeting on the raw body is how a long ad silently turns
#: into a 400 and a blank screen. The rest of these screens is a few hundred
#: characters, so most ads now show whole — the old 400 was an arbitrary clip
#: that hid the end of every one.
PREVIEW_CHARS = 2800

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
# Terms
# --------------------------------------------------------------------------- #
def terms() -> Screen:
    """What a new account sees, and the only screen it sees until it accepts.

    Written as a plain statement of what the tool does and where the
    responsibility sits, not as legal cover. The honest points are the ones
    people actually need: this posts from *your* Telegram account, Telegram can
    restrict that account, and nothing here will help you get around it.
    """
    return Screen(
        "\n".join(
            [
                "📡 *InsightAdFlow*",
                "",
                "Post your own message to Telegram groups you have already "
                "joined, and answer people who message you first\\.",
                "",
                "*Before you start, the honest version:*",
                "",
                "• It posts from *your* Telegram account, to groups *you* have "
                "already joined\\. It never joins a group for you and never reads "
                "a member list\\.",
                "",
                "• *Telegram can restrict or ban your account* if people report "
                "your messages as spam\\. That risk is yours, and this tool will "
                "not help you get around it — it obeys every rate limit and wait "
                "Telegram asks for\\.",
                "",
                "• Auto\\-reply only ever answers someone who messaged you "
                "first\\. There is no way to message people who did not\\.",
                "",
                "• You are responsible for what you send\\. The operator of this "
                "bot can suspend your access\\.",
                "",
                "• Your bot token, phone number and login code are typed into "
                "this chat\\. Each message is deleted the moment it is read, but "
                "Telegram's servers held it for a moment\\.",
                "",
                "Tap below if that is all fine\\.",
            ]
        ),
        _rows(
            [InlineKeyboardButton(text="✅ I understand, continue", callback_data="terms:accept")],
        ),
    )


# --------------------------------------------------------------------------- #
# Home
# --------------------------------------------------------------------------- #
def home(
    *,
    connections: Sequence[TelegramConnection],
    rules: Sequence[ForwardingRule],
    broadcasts: Sequence[Broadcast],
    counts: dict[str, int],
    is_operator: bool = False,
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
                f"\\({escape(connection.kind.value)}\\)"
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
            # Only operators see this, and only they can reach the handler —
            # hiding the button is presentation, the middleware is the gate.
            [
                InlineKeyboardButton(text="👥 Users", callback_data="nav:users:0"),
                InlineKeyboardButton(text="✨ Icons", callback_data="op:emoji"),
                InlineKeyboardButton(text="🔤 Buttons", callback_data="op:btn:0"),
            ]
            if is_operator
            else [],
        ),
    )


#: Every unicode emoji the panel draws in message text. The union of the status
#: icons and the screen furniture, deduplicated in place. Extraction walks this
#: list; anything Telegram has no premium version of simply stays plain.
PANEL_EMOJI: tuple[str, ...] = tuple(
    dict.fromkeys(
        [
            *STATUS_ICON.values(),
            "📡",
            "📣",
            "💬",
            "🧾",
            "📊",
            "🚀",
            "⚠️",
            "🔁",
            "⏱",
            "🖼",
            "✏️",
            "🗑",
            "🏠",
            "⬅️",
            "➡️",
            "➕",
            "💭",
            "👥",
            "✨",
            "🔄",
            "▶️",
            "🔥",
        ]
    )
)


def premium_icons_status(
    *,
    extracted: dict[str, str],
    live: bool,
    suspended: bool,
    has_user_connection: bool,
) -> Screen:
    """The operator's premium-icon screen: what is extracted, and the truth
    about whether Telegram will draw it.

    The Fragment sentence is not small print. Without that username Telegram
    rejects every custom-emoji message a bot sends, and an operator who was not
    told would read the plain icons as this feature being broken.
    """
    lines = ["✨ *Premium icons*", ""]
    if extracted:
        lines.append(f"*Extracted* — {len(extracted)} of {len(PANEL_EMOJI)} icons")
        if suspended:
            lines += [
                "",
                "⚠️ *Telegram refused them\\.* Custom emoji from a bot only "
                "render when the bot owns a *Fragment username* — that is "
                "Telegram's rule for every bot, and until then the panel shows "
                "plain icons\\. The extracted ids are kept; extraction again, or "
                "a restart, retries\\.",
            ]
        elif live:
            lines += ["", "Live — the panel is sending its icons as custom emoji\\."]
    else:
        lines += [
            "The panel currently draws plain unicode icons\\.",
            "",
            "Extraction asks Telegram, through your connected account, which "
            "custom emoji match each icon the panel uses, and stores their "
            "ids\\. Nothing is hardcoded — the ids are Telegram documents\\.",
        ]
    lines += [
        "",
        "When do they actually render? Telegram's rule, for text and buttons "
        "both: the bot owns a *Fragment username*, *or* the bot's owner has "
        "*Telegram Premium* — the panel's own screens qualify for the second, "
        "because the bot sends them directly\\. If Telegram refuses, the panel "
        "quietly stays plain rather than breaking\\.",
    ]
    lines += [
        "",
        "*The simplest way needs no login at all*: tap *Send emojis* and send "
        "me the premium emoji from your own keyboard — one message, as many as "
        "you like\\. Each one you send replaces the matching plain icon\\. "
        "Your own account cannot be *connected* from this chat \\(Telegram "
        "burns any login code it sees an account send\\), but sending emoji "
        "is just a message — nothing to connect\\.",
    ]

    return Screen(
        "\n".join(lines),
        _rows(
            [InlineKeyboardButton(text="📥 Send emojis", callback_data="op:emoji:send")],
            [
                InlineKeyboardButton(
                    text="🔁 Extract again" if extracted else "✨ Extract via account",
                    callback_data="op:emoji:run",
                )
            ]
            if has_user_connection
            else [],
            [InlineKeyboardButton(text="🚫 Turn off", callback_data="op:emoji:off")]
            if extracted
            else [],
            _back("nav:home"),
        ),
    )


#: Every static button label an operator may rename. The order is the order
#: shown; the *index* is what callbacks carry, so this list is append-only —
#: reordering it would re-point saved callbacks at the wrong button.
RENAMEABLE_BUTTONS: tuple[str, ...] = (
    "📣 Ads",
    "💬 Auto-reply",
    "📋 Forwarding",
    "🔗 Accounts",
    "💭 Groups",
    "📊 Activity",
    "🔄 Refresh",
    "🏠 Home",
    "➕ New ad",
    "➕ New rule",
    "➕ Add account",
    "➕ Add bot",
    "✏️ Message",
    "🖼 Image",
    "⏱ Pause",
    "🔁 Repeat",
    "🚀 Send now",
    "⏸ Pause",
    "▶️ Resume",
    "🚫 Stop",
    "🗑 Discard",
    "🧾 Groups",
    "📊 Events",
    "✏️ Edit",
    "⬅️ Back",
    "✅ Done",
)

BUTTONS_PAGE_SIZE = 8


def panel_buttons_list(*, custom: dict[str, str], page: int) -> Screen:
    """Every renameable button, with its current label beside the default."""
    window, page, pages = _page_of(RENAMEABLE_BUTTONS, page, BUTTONS_PAGE_SIZE)
    offset = page * BUTTONS_PAGE_SIZE

    lines = [
        "🔤 *Button labels*",
        "",
        "Tap a button to rename it everywhere it appears\\. The label is "
        "stored in the database and survives restarts\\. Send `-` while "
        "renaming to go back to the built\\-in label\\.",
        "",
    ]
    rows = []
    for i, default in enumerate(window):
        current = custom.get(default)
        shown = f"{default} → {current}" if current else default
        rows.append(
            [InlineKeyboardButton(text=shown[:56], callback_data=f"op:btn:pick:{offset + i}")]
        )
    if custom:
        lines.append(f"*Renamed* — {len(custom)}")

    return Screen(
        "\n".join(lines),
        _rows(
            *rows,
            _pager("op:btn:", page, pages),
            _back("nav:home"),
        ),
    )


# --------------------------------------------------------------------------- #
# Users (operators only)
# --------------------------------------------------------------------------- #
def users_list(*, users: Sequence, page: int, totals: dict[str, int]) -> Screen:  # type: ignore[type-arg]
    """Who is using this deployment. Counts only — never anyone's content."""
    window, page, pages = _page_of(users, page, PAGE_SIZE)
    lines = [
        "👥 *Users*",
        "",
        f"{totals.get('total', 0)} total · {totals.get('suspended', 0)} suspended",
        "",
    ]

    buttons = [
        [
            InlineKeyboardButton(
                text=("🚫 " if not user.is_active else "") + _user_label(user),
                callback_data=f"usr:{user.id}",
            )
        ]
        for user in window
    ]

    return Screen(
        "\n".join(lines),
        InlineKeyboardMarkup(
            inline_keyboard=[
                *buttons,
                _pager("nav:users:", page, pages),
                _home_row(),
            ]
        ),
    )


def user_detail(*, user, activity: dict[str, int]) -> Screen:  # type: ignore[no-untyped-def]
    """One account, as counts.

    Deliberately shows nothing about *what* they send. Suspending does not need
    it, and reading someone's ads would be a privacy breach the product does not
    make.
    """
    lines = [
        f"{'🚫' if not user.is_active else '✅'} *{escape(_user_label(user))}*",
        "",
        f"*Telegram id* — `{user.telegram_user_id}`",
        f"*Joined* — {user.created_at.strftime('%d %b %Y')}",
        f"*Status* — {'suspended' if not user.is_active else 'active'}",
    ]
    if not user.is_active and user.suspended_reason:
        lines.append(f"*Reason* — {escape(user.suspended_reason)}")
    if user.terms_accepted_at is None:
        lines.append("*Terms* — not accepted yet")

    lines += [
        "",
        f"*Accounts connected* — {activity.get('connections', 0)}",
        f"*Ads created* — {activity.get('broadcasts', 0)}",
        f"*Forwarding rules* — {activity.get('rules', 0)}",
        "",
        "_Counts only\\. What they send is not visible here\\._",
    ]

    action = (
        InlineKeyboardButton(text="✅ Reinstate", callback_data=f"usr:{user.id}:allow")
        if not user.is_active
        else InlineKeyboardButton(text="🚫 Suspend", callback_data=f"usr:{user.id}:asksus")
    )

    return Screen(
        "\n".join(lines),
        _rows([action], _back("nav:users:0")),
    )


def confirm_suspend(*, user) -> Screen:  # type: ignore[no-untyped-def]
    return Screen(
        f"🚫 *Suspend {escape(_user_label(user))}?*\n\n"
        "They lose access to the bot immediately\\. Their forwarding rules pause "
        "and anything still queued is cancelled\\.\n\n"
        "Messages already delivered stay where they are — suspending cannot "
        "unsend anything\\.",
        _rows(
            [InlineKeyboardButton(text="Yes, suspend", callback_data=f"usr:{user.id}:sus")],
            [InlineKeyboardButton(text="Cancel", callback_data=f"usr:{user.id}")],
        ),
    )


def _user_label(user) -> str:  # type: ignore[no-untyped-def]
    if user.telegram_username:
        return f"@{user.telegram_username}"
    return f"id {user.telegram_user_id}"


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
            "*Account* — a Telegram account, added with its phone number\\. Posts "
            "as that account, into groups it has already joined\\.\n"
            "_Telegram cancels a login code it sees an account send in a chat, so "
            "this works only for an account other than the one you are messaging "
            "me from\\._",
            "",
            "*Bot* — a bot from @BotFather\\. It can only post where you have added "
            "it as an administrator\\.",
        ]
    else:
        lines = ["🔗 *Accounts*", ""]
        for connection in connections:
            lines.append(
                f"{icon(connection.status.value)} *{escape(connection.label)}* "
                f"— {escape(connection.kind.value)}, {escape(connection.status.value)}"
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


#: Statuses meaning "this sign-in never finished". Sync and health checks are
#: meaningless here; the only useful action is to clear it and start again.
UNFINISHED = ("pending", "awaiting_code", "awaiting_2fa")


def connection_detail(
    *, connection: TelegramConnection, chat_count: int, syncing: bool = False
) -> Screen:
    lines = [
        f"{icon(connection.status.value)} *{escape(connection.label)}*",
        "",
        f"*Type* — {escape(connection.kind.value)}",
        f"*Status* — {escape(connection.status.value)}",
        f"*Groups known* — {chat_count}",
    ]
    if connection.telegram_username:
        lines.append(f"*Telegram* — @{escape(connection.telegram_username)}")
    if connection.last_error_message_safe:
        lines += ["", f"⚠️ {escape(connection.last_error_message_safe)}"]
    if syncing:
        # A running sync is the difference between "nothing happened" and "wait
        # a moment", and the screen is the only place that can say which.
        lines += [
            "",
            "\u23f3 *Reading your groups now\\.\\.\\.*",
            "",
            "_This takes a few seconds\\. Tap Refresh to see the result, or wait "
            "— I will message you when it finishes\\._",
        ]
    elif chat_count == 0:
        lines += [
            "",
            "_No groups yet\\. Tap Sync groups — it reads the groups this account "
            "has already joined\\. It never joins anything for you\\._",
        ]

    if connection.status.value in UNFINISHED:
        lines += [
            "",
            "_This sign\\-in never finished, so nothing works on it yet\\. Clear it "
            "and start again\\._",
        ]
        return Screen(
            "\n".join(lines),
            _rows(
                [
                    InlineKeyboardButton(
                        text="✖️ Cancel sign-in", callback_data=f"conn:{connection.id}:abandon"
                    )
                ],
                _back("nav:conns"),
            ),
        )

    return Screen(
        "\n".join(lines),
        _rows(
            [
                InlineKeyboardButton(
                    text="⏳ Syncing — tap to refresh" if syncing else "🔄 Sync groups",
                    callback_data=f"conn:{connection.id}"
                    if syncing
                    else f"conn:{connection.id}:sync",
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
def ads_list(
    *,
    broadcasts: Sequence[Broadcast],
    page: int,
    can_create: bool,
    counts_by_id: dict[uuid.UUID, dict[str, int]] | None = None,
) -> Screen:
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
    counts_by_id = counts_by_id or {}
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{delivery_icon(b, counts_by_id.get(b.id, {}))} {b.name[:30]}"
                + _delivered_suffix(counts_by_id.get(b.id, {})),
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


def premium_emoji_warning(
    *, has_premium_emoji: bool, account_is_premium: bool, checked: bool = True
) -> list[str]:
    """Said before sending, not discovered afterwards.

    A custom emoji is an ordinary emoji character in the text plus an entity
    naming the premium one to draw. Telegram honours that entity only for a
    Telegram Premium account, so without it the ad arrives showing the fallback
    characters — which looks like a bug in this tool and is not one.

    Three states, not two. ``checked=False`` means Telegram has never told us
    either way, and a stored default is not a finding: reporting one as "not
    Premium" is exactly how a Premium account came to be told it was not.
    """
    if not has_premium_emoji or (account_is_premium and checked):
        return []
    if not checked:
        return [
            "",
            "_This ad uses premium emoji\\. I have not checked yet whether this "
            "account is Telegram Premium — tap *Check health* on the connection, "
            "then reopen this ad\\._",
        ]
    return [
        "",
        "\u26a0\ufe0f *This ad uses premium emoji, and this account is not Telegram Premium\\.*",
        "",
        "_They will arrive as ordinary emoji\\. Subscribe on the posting "
        "account, or replace them — everything else posts exactly as written\\._",
    ]


def formatting_summary(entities: Sequence[dict]) -> str:  # type: ignore[type-arg]
    """What was captured from the message, in plain words.

    The preview cannot show any of it — it is escaped plain text, and a bot may
    not render a custom emoji at all — so this is the only way to confirm the
    formatting survived without posting an ad and inspecting the result.
    """
    if not entities:
        return ""

    premium = sum(1 for e in entities if e.get("type") == "custom_emoji")
    links = sum(1 for e in entities if e.get("type") in {"text_link", "url"})
    styles = {
        e.get("type")
        for e in entities
        if e.get("type") in {"bold", "italic", "underline", "strikethrough", "spoiler", "code"}
    }

    parts: list[str] = []
    if styles:
        parts.append(", ".join(sorted(str(s) for s in styles)))
    if premium:
        parts.append(f"{premium} premium emoji")
    if links:
        parts.append(f"{links} link" + ("s" if links > 1 else ""))
    return escape(" \u00b7 ".join(parts)) if parts else ""


#: Pacing presets, in milliseconds between two group deliveries. Indexed by
#: the callback, so this tuple is append-only.
SPEED_PRESETS: tuple[tuple[str, int], ...] = (
    ("⚡ Fast", 250),
    ("🚶 Normal", 3_000),
    ("🐢 Careful", 10_000),
)


def ad_speed(*, broadcast: Broadcast, target_count: int) -> Screen:
    """How fast to work through the groups, with the arithmetic shown.

    Presets rather than a bare number, because the number only means something
    once multiplied by the group count — and that multiplication is what
    decides whether a round takes forty seconds or half an hour.
    """
    current_round = estimated_round_s(broadcast.delay_ms, target_count)
    now_line = f"*Now* — {seconds_label(broadcast.delay_ms)} between groups"
    if target_count:
        now_line += f", about {escape(humanize(current_round))} a round"

    lines = ["⚡ *Speed*", "", f"*Groups* — {target_count}", now_line, ""]
    rows = []
    for index, (label, delay_ms) in enumerate(SPEED_PRESETS):
        estimate = estimated_round_s(delay_ms, target_count)
        suffix = f" — {humanize(estimate)}" if target_count else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{label}{suffix}",
                    callback_data=f"ad:{broadcast.id}:spd:{index}",
                )
            ]
        )

    lines += [
        "They all go out from *one* account over *one* connection, so they "
        "leave one after another rather than truly at once — but on *Fast* "
        "several are in the air together and 150 groups finish in well under "
        "a minute\\.",
        "",
        "Fast is roughly 4 messages a second, an order of magnitude under "
        "Telegram's documented rate\\. Going faster than this would buy "
        "seconds and risk *your* account being read as a flood, so the dial "
        "stops here\\. Every wait Telegram asks for is still obeyed in full\\.",
    ]

    return Screen(
        "\n".join(lines),
        _rows(
            *rows,
            [InlineKeyboardButton(text="✏️ Custom", callback_data=f"ad:{broadcast.id}:delay")],
            _back(f"ad:{broadcast.id}:back"),
        ),
    )


def estimated_round_s(delay_ms: int, target_count: int) -> float:
    """How long one pass over the groups takes.

    The same arithmetic the scheduler uses, so the number on screen is the
    number that happens.
    """
    return (delay_ms / 1000) * max(0, target_count - 1)


def ad_compose(
    *,
    broadcast: Broadcast,
    target_count: int,
    estimate_s: float,
    account_is_premium: bool = True,
    premium_checked: bool = True,
) -> Screen:
    """The compose screen, for a draft and for editing an ad already running.

    The same screen either way, because the fields are the same and a second
    near-identical screen is how two of them drift apart. What changes is the
    button at the bottom and the sentence above it: sending a draft starts an
    ad, saving an edit resumes one that was paused to be edited.
    """
    has_media = broadcast.media_kind.value != "none"
    body = broadcast.body_text.strip()
    editing = broadcast.status is not BroadcastStatus.draft

    shown, clipped = preview(body)
    lines = [
        f"{'✏️' if editing else '📝'} *{escape(broadcast.name)}*",
        "",
        "*Message*",
        f"_{shown}_" if body else "_not written yet_",
    ]
    if clipped:
        lines.append(f"_…and {clipped} more characters_")
    captured = formatting_summary(broadcast.body_entities or [])
    if captured:
        # The preview cannot show any of this — it is plain text, and a bot may
        # not render a custom emoji at all. Naming what was captured is the only
        # way to confirm it survived without posting an ad to find out.
        lines += [
            "",
            f"*Formatting kept* — {captured}",
            "_The preview above is plain text, so it cannot show them\\. The posted ad does\\._",
        ]
    elif body:
        # An absent line reads as "not applicable". A line saying none is what
        # answers "why are my premium emoji missing?" — most often because the
        # ad was written before the panel could keep formatting at all.
        lines += [
            "",
            "*Formatting kept* — none",
            "_If you sent bold text or premium emoji, tap *Message* and send it "
            "again\\. An ad written before this panel kept formatting has none "
            "stored\\._",
        ]

    lines += [
        "",
        f"*Image* — {'attached' if has_media else 'none'}",
        f"*Groups* — {target_count} selected",
        f"*Pause between groups* — {seconds_label(broadcast.delay_ms)}",
        f"*Repeat* — {escape(repeat_label(broadcast.repeat_every_s))}",
    ]
    if target_count:
        lines.append(f"*Takes about* — {escape(humanize(estimate_s))} per round")

    lines += premium_emoji_warning(
        has_premium_emoji=_has_premium_emoji(broadcast),
        account_is_premium=account_is_premium,
        checked=premium_checked,
    )

    ready = bool(body or has_media) and target_count > 0
    if editing:
        # Not conditional on readiness: what state the ad is in is the first
        # thing to say, and it is most needed exactly when something is missing.
        lines += [
            "",
            "_Paused while you edit\\. Groups this round has already posted to "
            "will not be posted to again — the changes take effect from where it "
            "left off\\._",
        ]
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
                InlineKeyboardButton(text="⚡ Speed", callback_data=f"ad:{broadcast.id}:speed"),
            ],
            [InlineKeyboardButton(text="🔁 Repeat", callback_data=f"ad:{broadcast.id}:repeat")],
            [
                InlineKeyboardButton(
                    text=f"💭 Groups ({target_count})",
                    callback_data=f"ad:{broadcast.id}:pick:0",
                )
            ],
            [
                InlineKeyboardButton(
                    text="▶️ Save and resume" if editing else "🚀 Send now",
                    callback_data=f"ad:{broadcast.id}:confirm",
                )
            ]
            if ready
            else [],
            [
                InlineKeyboardButton(text="🗑 Discard", callback_data=f"ad:{broadcast.id}:discard")
                if not editing
                else InlineKeyboardButton(text="⬅️ Back to ad", callback_data=f"ad:{broadcast.id}"),
                InlineKeyboardButton(text="⬅️ Ads", callback_data="nav:ads:0"),
            ],
        ),
    )


def delivery_icon(broadcast: Broadcast, counts: dict[str, int]) -> str:
    """The status icon, corrected by what actually arrived.

    ``completed`` means the round stopped having work to do — not that anyone
    received anything. An ad whose only group refused it finished as a green
    tick, which reads as "sent" and is the opposite of what happened.
    """
    delivered = counts.get("succeeded", 0)
    attempted = sum(counts.values())
    finished = broadcast.status in (BroadcastStatus.completed, BroadcastStatus.cancelled)
    if finished and attempted and not delivered:
        return "⚠️"
    return icon(broadcast.status.value)


def _delivered_suffix(counts: dict[str, int]) -> str:
    attempted = sum(counts.values())
    return f"  {counts.get('succeeded', 0)}/{attempted}" if attempted else ""


def _has_premium_emoji(broadcast: Broadcast) -> bool:
    return any(e.get("type") == "custom_emoji" for e in broadcast.body_entities or [])


def ad_confirm(
    *,
    broadcast: Broadcast,
    target_count: int,
    estimate_s: float,
    account_is_premium: bool = True,
    premium_checked: bool = True,
) -> Screen:
    has_media = broadcast.media_kind.value != "none"
    # Three moods: a fresh send, resuming an ad paused mid-round, and running a
    # finished one again. Each promises something different, and the promise
    # about groups already posted to is only true for the middle one.
    finished = broadcast.status in (BroadcastStatus.completed, BroadcastStatus.cancelled)
    editing = broadcast.status is not BroadcastStatus.draft and not finished
    # Room left for the summary and the warning below it.
    shown, clipped = preview(broadcast.body_text.strip(), PREVIEW_CHARS - 600)
    body = shown or escape("(image only)")
    if clipped:
        body += f"_\n\n_…and {clipped} more characters"
    warning = "\n".join(
        premium_emoji_warning(
            has_premium_emoji=_has_premium_emoji(broadcast),
            account_is_premium=account_is_premium,
            checked=premium_checked,
        )
    )
    if finished:
        header = "🚀 *Run this ad again?*"
        promise = (
            "Every selected group is posted to again, including ones that already received it\\."
        )
    elif editing:
        header = "▶️ *Save and resume?*"
        promise = "Groups this round already posted to are not posted to again\\."
    else:
        header = "🚀 *Send this ad?*"
        promise = (
            "It posts only to groups this account has already joined\\. "
            "You can pause it once it starts, but messages already posted cannot "
            "be unsent\\."
        )
    return Screen(
        f"{header}\n\n_{body}_\n\n"
        f"*To* — {target_count} groups\n"
        f"*Image* — {'yes' if has_media else 'no'}\n"
        f"*Pause between groups* — {seconds_label(broadcast.delay_ms)}\n"
        f"*Takes about* — {escape(humanize(estimate_s))} per round\n"
        f"*Repeat* — {escape(repeat_label(broadcast.repeat_every_s))}\n"
        f"{warning}\n\n" + promise,
        _rows(
            [
                InlineKeyboardButton(
                    text="Yes, resume" if editing else "Yes, send",
                    callback_data=f"ad:{broadcast.id}:send",
                )
            ],
            [InlineKeyboardButton(text="Cancel", callback_data=f"ad:{broadcast.id}")],
        ),
    )


def ad_detail(*, broadcast: Broadcast, counts: dict[str, int], target_count: int) -> Screen:
    done = counts.get("succeeded", 0)
    lines = [
        f"{delivery_icon(broadcast, counts)} *{escape(broadcast.name)}*",
        "",
        f"*Status* — {escape(broadcast.status.value)}",
    ]
    if broadcast.paused_reason_code:
        lines.append(f"*Reason* — {escape(reasons.describe(broadcast.paused_reason_code))}")

    lines += [
        f"*Progress* — {done}/{target_count} groups",
    ]

    # A finished ad that reached nobody, or only some, says so in words. The
    # arithmetic is there in the counts either way, but nobody reads a status
    # line as a subtraction problem.
    missed = sum(counts.get(s, 0) for s in ("skipped", "failed", "dead_letter", "needs_attention"))
    if missed:
        lines.append(
            f"⚠️ *{missed} of {target_count} did not receive it\\.* "
            "Tap *Events* for the group and the reason\\."
        )
    if broadcast.repeat_every_s:
        lines.append(f"*Repeat* — {escape(repeat_label(broadcast.repeat_every_s))}")
        if broadcast.repeat_count:
            lines.append(f"*Rounds sent* — {broadcast.repeat_count}")
        if broadcast.next_run_at and broadcast.status is BroadcastStatus.sending:
            lines.append(f"*Next round* — {escape(when(broadcast.next_run_at))}")
    # The running ad shows as much of itself as the compose screen did, minus
    # room for the deliveries breakdown below it.
    shown, clipped = preview(broadcast.body_text.strip(), PREVIEW_CHARS - 400)
    lines += [
        "",
        f"_{shown or escape('(image only)')}_",
    ]
    if clipped:
        lines.append(f"_…and {clipped} more characters_")

    if counts:
        lines += [
            "",
            "*Deliveries* — "
            + " · ".join(f"{icon(s)} {c} {escape(s)}" for s, c in sorted(counts.items())),
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
            # Editing is available at every stage after draft. An ad that repeats
            # for weeks will need its wording, its groups or its interval changed
            # at some point, and the alternative — build a new one and re-pick 500
            # groups — is not one.
            [InlineKeyboardButton(text="✏️ Edit", callback_data=f"ad:{broadcast.id}:edit")],
            [
                InlineKeyboardButton(text="🧾 Groups", callback_data=f"ad:{broadcast.id}:groups:0"),
                InlineKeyboardButton(text="📊 Events", callback_data=f"ad:{broadcast.id}:events"),
            ],
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
                    InlineKeyboardButton(
                        text=f"✅ Select all {len(chats)}", callback_data=f"{PICK}A"
                    ),
                    InlineKeyboardButton(text="Clear all", callback_data=f"{PICK}n{page}"),
                ],
                [InlineKeyboardButton(text="Select page", callback_data=f"{PICK}a{page}")],
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
        f"_{preview(body)[0]}_" if body else "_not written yet_",
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
        f"*Status* — {escape(rule.status.value)}",
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
            + " · ".join(f"{icon(s)} {c} {escape(s)}" for s, c in sorted(job_counts.items())),
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
def chats_list(*, chats: Sequence, page: int, other_count: int = 0) -> Screen:  # type: ignore[type-arg]
    if not chats:
        return Screen(
            "💭 *Groups*\n\nNone yet\\.\n\n"
            "Open *Accounts*, pick a connection, and tap *Sync groups*\\.",
            _rows(_home_row()),
        )

    window, page, pages = _page_of(chats, page, PAGE_SIZE)
    lines = [f"💭 *Groups* \\({len(chats)}\\)", ""]
    if other_count:
        # Private chats and channels are synchronized too — forwarding uses them
        # as sources — but they are not what this screen is about, and an
        # unexplained gap between 719 and 40 would look like a bug.
        lines += [
            f"_Plus {other_count} private chats and channels, which an ad never posts to\\._",
            "",
        ]
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


#: Report rows per page. Ten keeps a page with reasons under Telegram's cap
#: even when every title is at its longest.
REPORT_PAGE_SIZE = 10

#: Sort order for the report: what needs attention first, then what is still
#: coming, then what worked. The customer opening this screen is looking for
#: the problems; making them scroll past 150 green ticks to find one ⏭ would
#: hide the very thing the screen exists to show.
_REPORT_RANK = {
    JobStatus.failed: 0,
    JobStatus.dead_letter: 0,
    JobStatus.needs_attention: 0,
    JobStatus.skipped: 1,
    JobStatus.leased: 2,
    JobStatus.pending: 2,
    JobStatus.succeeded: 3,
}


def ad_group_report(
    *,
    broadcast: Broadcast,
    rows: Sequence[tuple],  # type: ignore[type-arg]
    page: int,
) -> Screen:
    """Every group by name, with what happened to it.

    This answers the two questions the counts cannot: *which* group did not get
    it, and *why that one*. Groups that worked are listed too — "did group X
    get it?" deserves a lookup, not an inference from the failures.
    """
    ordered = sorted(rows, key=lambda pair: (_REPORT_RANK.get(pair[0].status, 2), pair[0].position))
    window, page, pages = _page_of(ordered, page, REPORT_PAGE_SIZE)

    delivered = sum(1 for target, _ in rows if target.status is JobStatus.succeeded)
    problems = sum(1 for target, _ in rows if _REPORT_RANK.get(target.status, 2) <= 1)

    lines = [
        f"🧾 *{escape(broadcast.name)} — groups*",
        "",
        f"*Delivered* — {delivered}/{len(rows)}"
        + (f" · *problems* — {problems}" if problems else ""),
        "",
    ]
    for target, chat in window:
        lines.append(f"{icon(target.status.value)} *{escape(chat.title)}*")
        if target.status is JobStatus.succeeded:
            continue
        if target.status in (JobStatus.pending, JobStatus.leased):
            lines.append("   waiting its turn")
        else:
            code = target.last_error_code or reasons.UNKNOWN
            lines.append(f"   {escape(reasons.describe(code))}")

    return Screen(
        "\n".join(lines),
        _rows(
            _pager(f"ad:{broadcast.id}:groups:", page, pages),
            _back(f"ad:{broadcast.id}"),
            _home_row(),
        ),
    )


def activity(
    *,
    events: Sequence,  # type: ignore[type-arg]
    titles: dict[uuid.UUID, str] | None = None,
    back: str = "nav:home",
) -> Screen:
    """What happened, per group.

    The group name is the point of this screen. Without it the list reads
    "⏭ not allowed to post" twelve times, which tells you something is wrong but
    not which group to go and fix.
    """
    if not events:
        return Screen("📊 *Activity*\n\nNothing recorded yet\\.", _rows(_home_row()))

    titles = titles or {}
    lines = ["📊 *Recent activity*", ""]
    for event in events[:12]:
        stamp = event.occurred_at.strftime("%d %b %H:%M")
        what = escape(event.detail_safe or reasons.describe(event.reason_code))
        where = titles.get(event.destination_chat_id) if event.destination_chat_id else None
        head = f"{icon(event.outcome.value)} `{stamp}`"
        if where:
            lines.append(f"{head} *{escape(where)}*")
            lines.append(f"   {what}")
        else:
            lines.append(f"{head} {what}")

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


def repeat_label(repeat_every_s: int | None) -> str:
    return f"every {interval_label(repeat_every_s)}" if repeat_every_s else "once, then stop"


def interval_label(seconds: int) -> str:
    """A repeat interval in the units it was set in.

    ``humanize`` rounds to one decimal, which reads as "12.0 hours" and cannot
    express 90 minutes at all — it becomes "1.5 hours". An interval is a setting
    someone typed, so it is shown back exactly.
    """
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    if minutes:
        parts.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
    return " ".join(parts) or "0 minutes"


def when(moment: datetime) -> str:
    """How long until something happens, in words.

    Relative rather than absolute: the customer's timezone is not reliably
    known, and "in about 4 hours" needs no conversion to be useful.
    """
    seconds = (moment - datetime.now(UTC)).total_seconds()
    if seconds <= 0:
        return "any moment"
    return f"in about {humanize(seconds)}"


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

    Applies to **every** interpolated value, including ones that look safe.
    Enum values are the trap: ``awaiting_code`` and ``needs_attention`` contain
    an underscore, MarkdownV2 reads that as opening italics, and with no closing
    underscore Telegram rejects the whole message — so the screen does not
    render at all rather than rendering oddly.

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


def preview(body: str, limit: int = PREVIEW_CHARS) -> tuple[str, int]:
    """An ad, escaped and clipped to fit. Returns the text and what was cut.

    Clipping happens against the escaped length, because escaping is what makes
    a body overrun Telegram's 4096 — a message of nothing but ``.`` doubles.
    It also walks whole characters rather than slicing the escaped string, so a
    cut can never land between a backslash and what it escapes and leave a
    dangling one, which is a 400 of its own.

    The count returned is in the customer's characters, not escaped ones.
    """
    escaped = escape(body)
    if len(escaped) <= limit:
        return escaped, 0

    out: list[str] = []
    used = 0
    for index, char in enumerate(body):
        width = 2 if char in _MDV2_SPECIALS else 1
        if used + width > limit:
            return "".join(out), len(body) - index
        out.append(escape(char))
        used += width
    return "".join(out), 0


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
