"""Conversation states for the multi-step flows.

Everything the panel does that needs more than one tap lives here: connecting an
account, composing an ad, writing an auto-reply. State is held in Redis (see
``adminbot.main``), so a bot restart mid-flow resumes rather than dropping the
customer somewhere undefined.

Two things deliberately do **not** live in FSM state:

* the ad being composed — it is a ``draft`` row in the database, because an
  image and a 500-group selection are too much to keep in a conversation key,
  and losing half-composed work to a Redis eviction would be miserable;
* credentials — a login code and a 2FA password pass through memory to the
  adapter and are never written to state, a log, or the database.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ConnectBot(StatesGroup):
    label = State()
    token = State()


class ConnectAccount(StatesGroup):
    label = State()
    phone = State()
    code = State()
    password = State()


class ComposeAd(StatesGroup):
    name = State()
    text = State()
    media = State()
    delay = State()
    repeat = State()
    #: The group picker. Its selection and the ordered chat ids it indexes into
    #: are both held in state data under "selected" and "chat_ids".
    picking = State()


class ComposeRule(StatesGroup):
    name = State()
    source = State()
    picking = State()


class EditAutoReply(StatesGroup):
    text = State()
    cooldown = State()


class IconSetup(StatesGroup):
    #: The operator is sending premium emojis for the panel to adopt. Each
    #: message's custom-emoji entities are read off it; /cancel ends it.
    collect = State()


class EditButton(StatesGroup):
    #: Waiting for the new label. The chosen default's index into
    #: ``views.RENAMEABLE_BUTTONS`` sits in state data under "button_index".
    text = State()
