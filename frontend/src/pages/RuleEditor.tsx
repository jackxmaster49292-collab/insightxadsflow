import { useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { ApiError, api } from '../api/client'
import { keys, useChats, useConnections, useRule } from '../api/hooks'
import type { Rule } from '../api/types'
import { ErrorBanner, Notice } from '../components/ui'

const MEDIA_TYPES = ['text', 'photo', 'video', 'document', 'audio', 'voice', 'poll', 'animation']

function toList(value: string): string[] {
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

export function RuleEditorPage() {
  const { ruleId } = useParams()
  const editing = Boolean(ruleId)
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const connections = useConnections()
  const existing = useRule(ruleId ?? '')

  const [connectionId, setConnectionId] = useState('')
  const [name, setName] = useState('')
  const [sources, setSources] = useState<string[]>([])
  const [destinations, setDestinations] = useState<string[]>([])
  const [forwardMode, setForwardMode] = useState<'forward' | 'copy'>('forward')
  const [delayMs, setDelayMs] = useState(0)
  const [include, setInclude] = useState('')
  const [exclude, setExclude] = useState('')
  const [matchMode, setMatchMode] = useState<'substring' | 'word'>('substring')
  const [mediaTypes, setMediaTypes] = useState<string[]>([])
  const [allowLoop, setAllowLoop] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  // Seed the form once when editing an existing rule.
  useEffect(() => {
    const rule: Rule | undefined = existing.data
    if (!editing || !rule) return
    setConnectionId(rule.connection_id)
    setName(rule.name)
    setSources(rule.sources.map((chat) => chat.id))
    setDestinations(rule.destinations.map((chat) => chat.id))
    setForwardMode(rule.forward_mode)
    setDelayMs(rule.delay_ms)
    setInclude(rule.keyword_include.join(', '))
    setExclude(rule.keyword_exclude.join(', '))
    setMatchMode(rule.keyword_match_mode)
    setMediaTypes(rule.media_types)
  }, [editing, existing.data])

  useEffect(() => {
    if (!editing && !connectionId && connections.data?.[0]) {
      setConnectionId(connections.data[0].id)
    }
  }, [connections.data, connectionId, editing])

  const chats = useChats(connectionId ? { connection_id: connectionId } : {})

  const sourceOptions = useMemo(
    () => (chats.data ?? []).filter((chat) => chat.is_active),
    [chats.data],
  )

  const preview = useMemo(() => {
    const sourceTitles = sourceOptions.filter((c) => sources.includes(c.id)).map((c) => c.title)
    const destinationTitles = sourceOptions
      .filter((c) => destinations.includes(c.id))
      .map((c) => c.title)
    if (!sourceTitles.length) return 'Add at least one source chat to see a preview.'
    const joined =
      destinationTitles.length > 1
        ? `${destinationTitles.slice(0, -1).join(', ')}, and ${destinationTitles.at(-1)}`
        : (destinationTitles[0] ?? 'no destinations')
    const verb = forwardMode === 'forward' ? 'forward it to' : 'post a copy of it to'
    return `When a new eligible message appears in ${sourceTitles.join(', ')}, ${verb} ${joined}, subject to the configured filters and platform-safe processing rules.`
  }, [sourceOptions, sources, destinations, forwardMode])

  function toggle(list: string[], setList: (value: string[]) => void, id: string) {
    setList(list.includes(id) ? list.filter((item) => item !== id) : [...list, id])
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setBusy(true)
    const body = {
      name,
      connection_id: connectionId,
      source_chat_ids: sources,
      destination_chat_ids: destinations,
      forward_mode: forwardMode,
      delay_ms: delayMs,
      keyword_include: toList(include),
      keyword_exclude: toList(exclude),
      keyword_match_mode: matchMode,
      media_types: mediaTypes,
      preserve_links: true,
      preserve_caption: true,
      allow_source_as_destination: allowLoop,
    }
    try {
      const saved = editing
        ? await api.patch<Rule>(`/forwarding-rules/${ruleId}`, body)
        : await api.post<Rule>('/forwarding-rules', body)
      await queryClient.invalidateQueries({ queryKey: keys.rules })
      navigate(`/rules/${saved.id}`)
    } catch (caught) {
      setError(caught)
      if (caught instanceof ApiError && caught.code === 'source_is_also_destination') {
        setAllowLoop(false)
      }
    } finally {
      setBusy(false)
    }
  }

  const loopDetected = sources.some((id) => destinations.includes(id))

  return (
    <form onSubmit={submit}>
      <h1>{editing ? 'Edit forwarding rule' : 'Create forwarding rule'}</h1>
      <p className="subtitle">
        Only chats the connection is authorized to read from and post to can be selected.
      </p>

      <ErrorBanner error={error} />

      <div className="card">
        <label htmlFor="rule-name">Rule name</label>
        <input
          id="rule-name"
          type="text"
          required
          value={name}
          onChange={(event) => setName(event.target.value)}
        />

        <label htmlFor="rule-connection">Connection</label>
        <select
          id="rule-connection"
          value={connectionId}
          disabled={editing}
          onChange={(event) => {
            setConnectionId(event.target.value)
            setSources([])
            setDestinations([])
          }}
        >
          {(connections.data ?? []).map((connection) => (
            <option key={connection.id} value={connection.id}>
              {connection.label} ({connection.kind})
            </option>
          ))}
        </select>
        {editing ? (
          <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
            A rule cannot be moved to a different connection. Create a new rule instead.
          </p>
        ) : null}
      </div>

      <div className="card">
        <strong>Source chats</strong>
        <p className="muted" style={{ fontSize: 13 }}>
          Only chats the connection can read. Chats with content protection cannot be sources.
        </p>
        <div className="selector">
          {sourceOptions.map((chat) => (
            <label
              key={chat.id}
              className={`selector__row ${chat.source_eligible ? '' : 'selector__row--disabled'}`}
            >
              <input
                type="checkbox"
                disabled={!chat.source_eligible}
                checked={sources.includes(chat.id)}
                onChange={() => toggle(sources, setSources, chat.id)}
              />
              <span>{chat.title}</span>
              {!chat.source_eligible ? (
                <span className="reason">— {chat.source_reason_text}</span>
              ) : null}
            </label>
          ))}
        </div>
      </div>

      <div className="card">
        <strong>Destination chats</strong>
        <div className="selector">
          {sourceOptions.map((chat) => (
            <label
              key={chat.id}
              className={`selector__row ${chat.destination_eligible ? '' : 'selector__row--disabled'}`}
            >
              <input
                type="checkbox"
                disabled={!chat.destination_eligible}
                checked={destinations.includes(chat.id)}
                onChange={() => toggle(destinations, setDestinations, chat.id)}
              />
              <span>{chat.title}</span>
              {!chat.destination_eligible ? (
                <span className="reason">— {chat.destination_reason_text}</span>
              ) : null}
            </label>
          ))}
        </div>

        {loopDetected ? (
          <Notice tone="warn">
            A chat is selected as both a source and a destination, which would forward messages back
            into the same chat.
            <label className="checkbox">
              <input
                type="checkbox"
                checked={allowLoop}
                onChange={(event) => setAllowLoop(event.target.checked)}
              />
              I understand and want this
            </label>
          </Notice>
        ) : null}
      </div>

      <div className="card">
        <strong>Filters</strong>

        <label htmlFor="rule-include">Required keywords (comma separated)</label>
        <input
          id="rule-include"
          type="text"
          value={include}
          placeholder="launch, release"
          onChange={(event) => setInclude(event.target.value)}
        />

        <label htmlFor="rule-exclude">Excluded keywords (comma separated)</label>
        <input
          id="rule-exclude"
          type="text"
          value={exclude}
          placeholder="draft, internal"
          onChange={(event) => setExclude(event.target.value)}
        />

        <label htmlFor="rule-match">Keyword matching</label>
        <select
          id="rule-match"
          value={matchMode}
          onChange={(event) => setMatchMode(event.target.value as 'substring' | 'word')}
        >
          <option value="substring">Substring (matches inside words)</option>
          <option value="word">Whole word only</option>
        </select>

        <label>Media types (none selected means all supported types)</label>
        <div className="chip-list">
          {MEDIA_TYPES.map((type) => (
            <label key={type} className="checkbox">
              <input
                type="checkbox"
                checked={mediaTypes.includes(type)}
                onChange={() => toggle(mediaTypes, setMediaTypes, type)}
              />
              {type}
            </label>
          ))}
        </div>
      </div>

      <div className="card">
        <strong>Delivery</strong>

        <label htmlFor="rule-mode">Mode</label>
        <select
          id="rule-mode"
          value={forwardMode}
          onChange={(event) => setForwardMode(event.target.value as 'forward' | 'copy')}
        >
          <option value="forward">Forward (keeps the original attribution)</option>
          <option value="copy">Copy (posts without attribution)</option>
        </select>

        <label htmlFor="rule-delay">Delay between destinations (milliseconds)</label>
        <input
          id="rule-delay"
          type="number"
          min={0}
          max={3600000}
          value={delayMs}
          onChange={(event) => setDelayMs(Number(event.target.value))}
        />
        <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
          A transparent, bounded pause used for orderly pacing. Telegram&rsquo;s own rate limits are
          always respected on top of this.
        </p>
      </div>

      <div className="card">
        <strong>Preview</strong>
        <div className="preview" style={{ marginTop: 8 }}>
          {preview}
        </div>
      </div>

      <div className="btn-row">
        <button type="submit" className="btn btn--primary" disabled={busy}>
          {busy ? 'Saving…' : editing ? 'Save changes' : 'Create rule'}
        </button>
        <button type="button" className="btn btn--ghost" onClick={() => navigate('/rules')}>
          Cancel
        </button>
      </div>
    </form>
  )
}
