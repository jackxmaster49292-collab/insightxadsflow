import { useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useState } from 'react'

import { api, newIdempotencyKey } from '../api/client'
import type { Connection } from '../api/types'
import {
  keys,
  useConnections,
  useDisconnect,
  useHealthCheck,
  useSyncChats,
} from '../api/hooks'
import { AsyncState, ConfirmButton, ErrorBanner, Notice, RelativeTime, StatusPill } from '../components/ui'

function ConnectionCard({ connection }: { connection: Connection }) {
  const sync = useSyncChats(connection.id)
  const health = useHealthCheck(connection.id)
  const disconnect = useDisconnect(connection.id)
  const [notice, setNotice] = useState<string | null>(null)

  const capabilities = connection.capabilities
  const disabled = connection.status === 'disconnected'

  return (
    <div className="card">
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <div>
          <strong>{connection.label}</strong>{' '}
          <span className="chip">{connection.kind === 'bot' ? 'Bot' : 'User account'}</span>{' '}
          <StatusPill status={connection.status} />
          <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>
            {connection.telegram_username ? `@${connection.telegram_username} · ` : ''}
            Last successful check: <RelativeTime value={connection.last_successful_check_at} />
          </div>
        </div>
      </div>

      {connection.last_error_message_safe ? (
        <Notice tone="warn">{connection.last_error_message_safe}</Notice>
      ) : null}

      {capabilities ? (
        <>
          <h2 style={{ fontSize: 14 }}>What this connection can access</h2>
          <ul className="muted" style={{ fontSize: 13, marginTop: 0, paddingLeft: 18 }}>
            <li>
              Read channels it only subscribes to:{' '}
              <strong>{capabilities.can_read_subscribed_channels ? 'Yes' : 'No'}</strong>
            </li>
            <li>
              Read group messages: <strong>{capabilities.can_read_group_messages ? 'Yes' : 'Admin only'}</strong>
            </li>
            <li>
              Media download limit:{' '}
              <strong>
                {capabilities.max_download_bytes
                  ? `${Math.round(capabilities.max_download_bytes / 1024 / 1024)} MB`
                  : 'No fixed limit'}
              </strong>
            </li>
          </ul>
          {capabilities.notes.map((note) => (
            <p key={note} className="muted" style={{ fontSize: 13, margin: '4px 0' }}>
              {note}
            </p>
          ))}
        </>
      ) : null}

      {notice ? <Notice>{notice}</Notice> : null}
      <ErrorBanner error={sync.error ?? health.error ?? disconnect.error} />

      <div className="btn-row" style={{ marginTop: 12 }}>
        <button
          type="button"
          className="btn"
          disabled={disabled || sync.isPending}
          onClick={() => sync.mutate(undefined, { onSuccess: (data) => setNotice(data.message) })}
        >
          Synchronize chats
        </button>
        <button
          type="button"
          className="btn"
          disabled={disabled || health.isPending}
          onClick={() => health.mutate(undefined, { onSuccess: (data) => setNotice(data.message) })}
        >
          Check health
        </button>
        <ConfirmButton
          label="Disconnect"
          confirmLabel="Disconnect and keep the session?"
          disabled={disabled}
          danger={false}
          onConfirm={() => disconnect.mutate(false, { onSuccess: (data) => setNotice(data.message) })}
        />
        <ConfirmButton
          label="Disconnect and revoke"
          confirmLabel="Revoke the Telegram session too? This cannot be undone."
          disabled={disabled}
          onConfirm={() => disconnect.mutate(true, { onSuccess: (data) => setNotice(data.message) })}
        />
      </div>
    </div>
  )
}

function ConnectBotForm({ onDone }: { onDone: () => void }) {
  const [label, setLabel] = useState('')
  const [token, setToken] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await api.post('/telegram/connections/bot', { label, bot_token: token }, newIdempotencyKey())
      setToken('')
      setLabel('')
      onDone()
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="card" onSubmit={submit}>
      <strong>Connect a bot</strong>
      <p className="muted" style={{ fontSize: 13 }}>
        Create a bot with @BotFather, then add it to each source channel and grant it post rights in
        each destination. A bot cannot list its own chats, so send a message in each chat before
        synchronizing.
      </p>
      <ErrorBanner error={error} />

      <label htmlFor="bot-label">Label</label>
      <input
        id="bot-label"
        type="text"
        required
        value={label}
        placeholder="Announcements bot"
        onChange={(event) => setLabel(event.target.value)}
      />

      <label htmlFor="bot-token">Bot token</label>
      <input
        id="bot-token"
        type="password"
        required
        autoComplete="off"
        value={token}
        placeholder="123456789:AA…"
        onChange={(event) => setToken(event.target.value)}
      />
      <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
        Stored encrypted at rest and never shown again.
      </p>

      <button type="submit" className="btn btn--primary" disabled={busy} style={{ marginTop: 12 }}>
        {busy ? 'Connecting…' : 'Connect bot'}
      </button>
    </form>
  )
}

function ConnectUserForm({ onDone }: { onDone: () => void }) {
  const [step, setStep] = useState<'phone' | 'code' | '2fa'>('phone')
  const [label, setLabel] = useState('')
  const [phone, setPhone] = useState('')
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const [connectionId, setConnectionId] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  async function run(action: () => Promise<void>) {
    setError(null)
    setBusy(true)
    try {
      await action()
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="card" onSubmit={(event) => event.preventDefault()}>
      <strong>Connect a Telegram account</strong>
      <Notice tone="warn">
        This connects your personal Telegram account. It can read any chat you have joined, but
        Telegram may restrict the account if it is used abusively. Prefer a bot where one is enough.
      </Notice>
      <ErrorBanner error={error} />

      {step === 'phone' ? (
        <>
          <label htmlFor="user-label">Label</label>
          <input
            id="user-label"
            type="text"
            required
            value={label}
            placeholder="Main account"
            onChange={(event) => setLabel(event.target.value)}
          />
          <label htmlFor="user-phone">Phone number</label>
          <input
            id="user-phone"
            type="text"
            required
            value={phone}
            placeholder="+441234567890"
            onChange={(event) => setPhone(event.target.value)}
          />
          <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
            Only a hash of your number is stored.
          </p>
          <button
            type="button"
            className="btn btn--primary"
            disabled={busy}
            style={{ marginTop: 12 }}
            onClick={() =>
              void run(async () => {
                const created = await api.post<{ id: string }>('/telegram/connections/user/start', {
                  label,
                  phone,
                })
                setConnectionId(created.id)
                setStep('code')
              })
            }
          >
            {busy ? 'Sending code…' : 'Send login code'}
          </button>
        </>
      ) : null}

      {step === 'code' ? (
        <>
          <label htmlFor="user-code">Login code</label>
          <input
            id="user-code"
            type="text"
            inputMode="numeric"
            autoComplete="one-time-code"
            required
            value={code}
            onChange={(event) => setCode(event.target.value)}
          />
          <button
            type="button"
            className="btn btn--primary"
            disabled={busy}
            style={{ marginTop: 12 }}
            onClick={() =>
              void run(async () => {
                const result = await api.post<{ status: string }>(
                  '/telegram/connections/user/verify',
                  { connection_id: connectionId, code },
                )
                if (result.status === 'awaiting_2fa') setStep('2fa')
                else onDone()
              })
            }
          >
            {busy ? 'Verifying…' : 'Verify code'}
          </button>
        </>
      ) : null}

      {step === '2fa' ? (
        <>
          <label htmlFor="user-2fa">Two-factor password</label>
          <input
            id="user-2fa"
            type="password"
            autoComplete="off"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
          <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
            Used once to complete sign-in. It is never stored, hashed, or logged.
          </p>
          <button
            type="button"
            className="btn btn--primary"
            disabled={busy}
            style={{ marginTop: 12 }}
            onClick={() =>
              void run(async () => {
                await api.post(`/telegram/connections/${connectionId}/2fa`, { password })
                setPassword('')
                onDone()
              })
            }
          >
            {busy ? 'Signing in…' : 'Complete sign-in'}
          </button>
        </>
      ) : null}
    </form>
  )
}

export function ConnectionsPage() {
  const connections = useConnections()
  const queryClient = useQueryClient()
  const [adding, setAdding] = useState<'bot' | 'user' | null>(null)

  const refresh = () => {
    setAdding(null)
    void queryClient.invalidateQueries({ queryKey: keys.connections })
  }

  return (
    <>
      <h1>Telegram connections</h1>
      <p className="subtitle">
        The active connection type is always shown. This product never silently falls back from a
        bot to an account, or between accounts.
      </p>

      <div className="btn-row" style={{ marginBottom: 14 }}>
        <button type="button" className="btn btn--primary" onClick={() => setAdding('bot')}>
          Connect a bot
        </button>
        <button type="button" className="btn" onClick={() => setAdding('user')}>
          Connect an account
        </button>
      </div>

      {adding === 'bot' ? <ConnectBotForm onDone={refresh} /> : null}
      {adding === 'user' ? <ConnectUserForm onDone={refresh} /> : null}

      <AsyncState query={connections} empty="No connections yet. Connect a bot to get started.">
        {(rows) => (
          <>
            {rows.map((connection) => (
              <ConnectionCard key={connection.id} connection={connection} />
            ))}
          </>
        )}
      </AsyncState>
    </>
  )
}
