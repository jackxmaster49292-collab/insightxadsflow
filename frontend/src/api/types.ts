/**
 * Mirrors the backend response models.
 *
 * Telegram identifiers are `string`, never `number`: the API serializes them as
 * strings precisely so JavaScript's 53-bit safe-integer limit can never round a
 * peer id, and so `(peer_type, peer_id)` stays visible as the real key.
 */

export type ConnectionKind = 'bot' | 'user'

export type ConnectionStatus =
  | 'pending'
  | 'awaiting_code'
  | 'awaiting_2fa'
  | 'active'
  | 'error'
  | 'paused_safety'
  | 'disconnected'

export type RuleStatus = 'draft' | 'active' | 'paused' | 'error' | 'disconnected'

export type EventOutcome = 'forwarded' | 'skipped' | 'failed' | 'retry_scheduled' | 'paused'

export interface Capabilities {
  can_read_subscribed_channels: boolean
  can_read_group_messages: boolean
  can_read_history: boolean
  max_download_bytes: number | null
  notes: string[]
}

export interface Connection {
  id: string
  kind: ConnectionKind
  label: string
  status: ConnectionStatus
  telegram_username: string | null
  telegram_account_id: string | null
  last_health_check_at: string | null
  last_successful_check_at: string | null
  last_error_code: string | null
  last_error_message_safe: string | null
  created_at: string
  capabilities?: Capabilities | null
}

export interface Chat {
  id: string
  connection_id: string
  peer_type: string
  peer_id: string
  title: string
  username: string | null
  chat_kind: string
  is_public: boolean | null
  has_protected_content: boolean | null
  is_active: boolean
  last_synced_at: string | null
  source_eligible: boolean
  source_reason_code: string
  source_reason_text: string
  destination_eligible: boolean
  destination_reason_code: string
  destination_reason_text: string
}

export interface RuleChatSummary {
  id: string
  title: string
  peer_id: string
  peer_type: string
  eligible: boolean
  reason_code: string
}

export interface Rule {
  id: string
  name: string
  connection_id: string
  status: RuleStatus
  version: number
  forward_mode: 'forward' | 'copy'
  delay_ms: number
  keyword_include: string[]
  keyword_exclude: string[]
  keyword_match_mode: 'substring' | 'word'
  media_types: string[]
  preserve_links: boolean
  preserve_caption: boolean
  paused_reason_code: string | null
  paused_reason_text: string | null
  last_activity_at: string | null
  created_at: string
  sources: RuleChatSummary[]
  destinations: RuleChatSummary[]
  preview: string
}

export interface RuleListItem {
  id: string
  name: string
  status: RuleStatus
  connection_id: string
  destination_count: number
  source_titles: string[]
  filter_summary: string
  last_activity_at: string | null
  paused_reason_text: string | null
}

export interface ForwardingEvent {
  id: string
  rule_id: string
  outcome: EventOutcome
  reason_code: string
  reason_text: string
  detail_safe: string | null
  attempt: number
  source_chat_id: string | null
  destination_chat_id: string | null
  source_message_ids: string[]
  occurred_at: string
}

export interface RuleJob {
  id: string
  destination_chat_id: string
  destination_title: string
  status: string
  attempt_count: number
  last_error_code: string | null
  last_error_text: string | null
  source_message_ids: string[]
  updated_at: string
}

export interface UsageSummary {
  period: string
  forwarded: number
  skipped: number
  failed: number
  retry_scheduled: number
  paused: number
}

export interface Me {
  id: string
  email: string
  timezone: string
  connection_count: number
  active_rule_count: number
}

export interface AuditEvent {
  id: string
  action: string
  object_type: string
  object_id: string | null
  correlation_id: string | null
  created_at: string
}

export interface Accepted {
  status: 'accepted'
  task_id: string | null
  message: string
}
