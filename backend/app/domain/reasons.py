"""Closed reason-code vocabulary.

The UI explains *why* something was skipped or is ineligible without ever
surfacing a raw provider error. Adding a code here means adding a customer-facing
sentence in ``REASON_TEXT``.
"""

from __future__ import annotations

from typing import Final

# --- eligibility ------------------------------------------------------------
OK: Final = "ok"
NOT_A_MEMBER: Final = "not_a_member"
BOT_NOT_ADMIN: Final = "bot_not_admin"
PRIVACY_MODE_ENABLED: Final = "privacy_mode_enabled"
WRITE_FORBIDDEN: Final = "write_forbidden"
ADMIN_REQUIRED: Final = "admin_required"
SEND_MEDIA_FORBIDDEN: Final = "send_media_forbidden"
BANNED: Final = "banned"
BLOCKED: Final = "blocked"
CHANNEL_PRIVATE: Final = "channel_private"
PEER_INVALID: Final = "peer_invalid"
TOPIC_CLOSED: Final = "topic_closed"
NOT_A_DESTINATION_TYPE: Final = "not_a_destination_type"
CHAT_MIGRATED: Final = "chat_migrated"
UNKNOWN: Final = "unknown"

# --- content ----------------------------------------------------------------
PROTECTED_CONTENT: Final = "protected_content"
UNSUPPORTED_MESSAGE: Final = "unsupported_message"
UNCOPYABLE_MESSAGE: Final = "uncopyable_message"
MESSAGE_UNAVAILABLE: Final = "message_unavailable"
MEDIA_UNAVAILABLE: Final = "media_unavailable"
CAPTION_TOO_LONG: Final = "caption_too_long"
MESSAGE_TOO_LONG: Final = "message_too_long"
ENTITY_TOO_LARGE: Final = "entity_too_large"
PREMIUM_EMOJI_REQUIRED: Final = "premium_emoji_required"

# --- filtering --------------------------------------------------------------
FILTERED_MEDIA_TYPE: Final = "filtered_media_type"
FILTERED_KEYWORD_INCLUDE: Final = "filtered_keyword_include"
FILTERED_KEYWORD_EXCLUDE: Final = "filtered_keyword_exclude"
FILTERED_AFTER_EDIT: Final = "filtered_after_edit"

# --- transport / platform (produced by classify_error) ----------------------
SLOWMODE_WAIT: Final = "slowmode_wait"
FLOOD_WAIT: Final = "flood_wait"
NETWORK_ERROR: Final = "network_error"
TIMEOUT: Final = "timeout"
SERVER_ERROR: Final = "server_error"
SERVER_RESTARTING: Final = "server_restarting"
RPC_CALL_FAIL: Final = "rpc_call_fail"
GETUPDATES_CONFLICT: Final = "getupdates_conflict"
UNAUTHORIZED: Final = "unauthorized"
AUTH_KEY_UNREGISTERED: Final = "auth_key_unregistered"
AUTH_KEY_DUPLICATED: Final = "auth_key_duplicated"
SESSION_REVOKED: Final = "session_revoked"
SESSION_EXPIRED: Final = "session_expired"
USER_DEACTIVATED: Final = "user_deactivated"
USER_DEACTIVATED_BAN: Final = "user_deactivated_ban"

# --- lifecycle --------------------------------------------------------------
DELIVERED: Final = "delivered"
DUPLICATE_SUPPRESSED: Final = "duplicate_suppressed"
DESTINATION_REMOVED: Final = "destination_removed"
DESTINATION_NOT_ELIGIBLE: Final = "destination_not_eligible"
SOURCE_NOT_ELIGIBLE: Final = "source_not_eligible"
RULE_INACTIVE: Final = "rule_inactive"
CONNECTION_DISCONNECTED: Final = "connection_disconnected"
AMBIGUOUS_TIMEOUT: Final = "ambiguous_timeout"
MAX_ATTEMPTS_EXCEEDED: Final = "max_attempts_exceeded"
SAFETY_PAUSE: Final = "safety_pause"
FLOOD_WAIT_PAUSE: Final = "flood_wait_pause"
AUTH_PAUSE: Final = "auth_pause"
PARTIAL_ALBUM: Final = "partial_album"
RETRYING: Final = "retrying"

# --- broadcasts -------------------------------------------------------------
BROADCAST_INACTIVE: Final = "broadcast_inactive"
BROADCAST_POSTED: Final = "broadcast_posted"
BROADCAST_CANCELLED: Final = "broadcast_cancelled"
BROADCAST_PAUSED_BY_CUSTOMER: Final = "broadcast_paused_by_customer"
BROADCAST_BEING_EDITED: Final = "broadcast_being_edited"
BROADCAST_EMPTY: Final = "broadcast_empty"

# --- auto-reply -------------------------------------------------------------
AUTO_REPLY_SENT: Final = "auto_reply_sent"
AUTO_REPLY_COOLDOWN: Final = "auto_reply_cooldown"
AUTO_REPLY_DISABLED: Final = "auto_reply_disabled"
AUTO_REPLY_NOT_PRIVATE: Final = "auto_reply_not_private"

# --- signing in an account --------------------------------------------------
LOGIN_CODE_INVALID: Final = "login_code_invalid"
LOGIN_CODE_EXPIRED: Final = "login_code_expired"
LOGIN_RESTART_NEEDED: Final = "login_restart_needed"
PHONE_NUMBER_INVALID: Final = "phone_number_invalid"
PHONE_NUMBER_BANNED: Final = "phone_number_banned"
PHONE_NUMBER_UNREGISTERED: Final = "phone_number_unregistered"
PHONE_NUMBER_FLOOD: Final = "phone_number_flood"
TWO_FACTOR_REQUIRED: Final = "two_factor_required"
TWO_FACTOR_PASSWORD_INVALID: Final = "two_factor_password_invalid"  # noqa: S105 - a reason code, not a password

# --- account ----------------------------------------------------------------
ACCOUNT_SUSPENDED: Final = "account_suspended"

REASON_TEXT: dict[str, str] = {
    OK: "Available.",
    NOT_A_MEMBER: "The connection is not a member of this chat.",
    BOT_NOT_ADMIN: "The bot must be an administrator of this chat to read its messages.",
    PRIVACY_MODE_ENABLED: (
        "The bot's privacy mode is enabled, so it only sees commands and replies in this group."
    ),
    WRITE_FORBIDDEN: "The connection is not allowed to post in this chat.",
    ADMIN_REQUIRED: "Administrator rights are required for this action.",
    SEND_MEDIA_FORBIDDEN: "The connection is not allowed to send media in this chat.",
    BANNED: "The connection is banned from this chat.",
    BLOCKED: "The connection has been blocked by this chat.",
    CHANNEL_PRIVATE: "This channel is private or no longer accessible to the connection.",
    PEER_INVALID: "Telegram no longer recognises this chat for the connection.",
    TOPIC_CLOSED: "The forum topic is closed.",
    NOT_A_DESTINATION_TYPE: "This chat type cannot be used as a destination.",
    CHAT_MIGRATED: (
        "This group was upgraded to a supergroup, which gives it a new Telegram "
        "identifier. The old one no longer works. Synchronize the connection and "
        "add the new group to the rule."
    ),
    UNKNOWN: "Eligibility has not been checked yet.",
    PROTECTED_CONTENT: (
        "This chat has content protection enabled, so its messages cannot be forwarded or "
        "copied. InsightAdFlow does not bypass content protection."
    ),
    UNSUPPORTED_MESSAGE: "This message type is not supported and was skipped.",
    UNCOPYABLE_MESSAGE: "Telegram does not allow this message type to be copied.",
    MESSAGE_UNAVAILABLE: "The source message is no longer available.",
    MEDIA_UNAVAILABLE: "The media attached to this message is no longer available.",
    CAPTION_TOO_LONG: "The caption exceeds Telegram's length limit.",
    MESSAGE_TOO_LONG: "The message exceeds Telegram's length limit.",
    ENTITY_TOO_LARGE: "The attached file is too large for this connection type.",
    PREMIUM_EMOJI_REQUIRED: (
        "This message uses premium emoji, and Telegram only lets a Telegram "
        "Premium account send those. Either subscribe on the account doing the "
        "posting, or rewrite the ad with ordinary emoji."
    ),
    FILTERED_MEDIA_TYPE: "Skipped: this media type is not selected on the rule.",
    FILTERED_KEYWORD_INCLUDE: "Skipped: no required keyword was found.",
    FILTERED_KEYWORD_EXCLUDE: "Skipped: an excluded keyword was found.",
    FILTERED_AFTER_EDIT: "Skipped: the rule was edited and this message no longer matches.",
    DELIVERED: "Forwarded successfully.",
    DUPLICATE_SUPPRESSED: "Already delivered to this destination — not sent again.",
    DESTINATION_REMOVED: "Skipped: this destination was removed from the rule.",
    DESTINATION_NOT_ELIGIBLE: "Skipped: this destination is no longer eligible.",
    SOURCE_NOT_ELIGIBLE: "Skipped: this source is no longer readable by the connection.",
    RULE_INACTIVE: "Skipped: the rule is not active.",
    CONNECTION_DISCONNECTED: "Skipped: the Telegram connection is disconnected.",
    AMBIGUOUS_TIMEOUT: (
        "The request timed out and Telegram may or may not have delivered this message. "
        "It was not retried automatically, to avoid posting a duplicate. Retry manually if needed."
    ),
    MAX_ATTEMPTS_EXCEEDED: "Skipped: the maximum number of attempts was reached.",
    SAFETY_PAUSE: "The rule was paused automatically after repeated serious failures.",
    FLOOD_WAIT_PAUSE: "The rule was paused because Telegram requested a long wait.",
    AUTH_PAUSE: "The connection was paused because it is no longer authorized.",
    PARTIAL_ALBUM: "Part of a grouped-media album did not arrive in time.",
    RETRYING: "A temporary failure occurred; a retry has been scheduled.",
    BROADCAST_INACTIVE: "Skipped: the broadcast is paused, cancelled or already finished.",
    BROADCAST_POSTED: "Posted to this group.",
    BROADCAST_CANCELLED: "Cancelled before this group was reached.",
    BROADCAST_PAUSED_BY_CUSTOMER: (
        "You paused this ad. Nothing more will be posted until you resume it."
    ),
    BROADCAST_BEING_EDITED: (
        "Paused while you edit it. Save and resume to continue where it left off."
    ),
    BROADCAST_EMPTY: "The broadcast has no message to send.",
    AUTO_REPLY_SENT: "Replied automatically to an incoming direct message.",
    AUTO_REPLY_COOLDOWN: (
        "Not replied: this person was already answered recently. The cooldown is "
        "what keeps an automatic reply from becoming repeat messaging."
    ),
    AUTO_REPLY_DISABLED: "Not replied: auto-reply is turned off for this connection.",
    AUTO_REPLY_NOT_PRIVATE: (
        "Not replied: automatic replies are only ever sent in a private chat "
        "started by the other person."
    ),
    LOGIN_CODE_INVALID: (
        "Telegram rejected that login code, and almost certainly not because of "
        "a typo. Telegram cancels any login code it sees an account send inside "
        "a chat, so typing it here burns it even when the digits are right. "
        "Sending it again will fail the same way. This works only when the "
        "account you are connecting is not the one you are messaging this bot "
        "from."
    ),
    LOGIN_CODE_EXPIRED: (
        "That login code has expired. Cancel the sign-in and start again to get a new one."
    ),
    LOGIN_RESTART_NEEDED: (
        "Telegram asked us to start the sign-in over. Begin again from Accounts."
    ),
    PHONE_NUMBER_INVALID: (
        "Telegram does not recognise that phone number. Include the country "
        "code, for example +919876543210."
    ),
    PHONE_NUMBER_BANNED: (
        "Telegram has banned that phone number, so it cannot be connected. "
        "Nothing here can change that; contact Telegram support."
    ),
    PHONE_NUMBER_UNREGISTERED: (
        "That phone number has no Telegram account. Sign up in the Telegram app "
        "first, then connect it here."
    ),
    PHONE_NUMBER_FLOOD: (
        "Telegram has temporarily blocked sign-in attempts for that number "
        "after too many tries. Wait — usually a day — before trying again."
    ),
    TWO_FACTOR_REQUIRED: "This account has two-step verification. Send the password to continue.",
    TWO_FACTOR_PASSWORD_INVALID: (
        "That two-step verification password was not accepted. It is the "
        "password you set in Telegram, not the login code."
    ),
    ACCOUNT_SUSPENDED: (
        "This account is suspended, so nothing is being sent. Messages already "
        "delivered are unaffected."
    ),
    SLOWMODE_WAIT: (
        "This group has slow mode enabled, so Telegram limits how often anyone can "
        "post in it. The message will be sent once the wait has passed."
    ),
    FLOOD_WAIT: (
        "Telegram asked us to slow down and we are waiting the full time it "
        "requested before trying again."
    ),
    NETWORK_ERROR: "A network problem occurred. This will be retried automatically.",
    TIMEOUT: "The request to Telegram timed out. This will be retried automatically.",
    SERVER_ERROR: "Telegram returned a server error. This will be retried automatically.",
    SERVER_RESTARTING: "Telegram is restarting. This will be retried automatically.",
    RPC_CALL_FAIL: "Telegram could not complete the request. This will be retried automatically.",
    GETUPDATES_CONFLICT: (
        "Another process is already receiving updates for this bot token. Each bot "
        "token supports only one consumer — check that the same token is not used "
        "for both the control panel and a forwarding connection."
    ),
    UNAUTHORIZED: ("Telegram rejected this connection's credentials. Reconnect it to continue."),
    AUTH_KEY_UNREGISTERED: (
        "This Telegram session is no longer registered. Reconnect the connection."
    ),
    AUTH_KEY_DUPLICATED: (
        "This Telegram session was used from another location and Telegram "
        "invalidated it. Reconnect the connection."
    ),
    SESSION_REVOKED: (
        "This Telegram session was revoked, most likely from your Telegram "
        "account's active-sessions list. Reconnect the connection."
    ),
    SESSION_EXPIRED: "This Telegram session has expired. Reconnect the connection.",
    USER_DEACTIVATED: "The Telegram account for this connection has been deactivated.",
    USER_DEACTIVATED_BAN: (
        "The Telegram account for this connection has been deactivated by Telegram."
    ),
}


def describe(reason_code: str) -> str:
    return REASON_TEXT.get(reason_code, REASON_TEXT[UNKNOWN])
