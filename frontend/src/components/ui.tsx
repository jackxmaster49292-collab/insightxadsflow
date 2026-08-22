/**
 * Small shared primitives.
 *
 * `AsyncState` exists so every page renders loading, empty, error, and
 * permission-denied consistently — that is an acceptance requirement, not a
 * nicety, and centralising it means a new page cannot forget one.
 */

import type { ReactNode } from 'react'
import { useState } from 'react'

import { ApiError } from '../api/client'

export function StatusPill({ status }: { status: string }) {
  const tone: Record<string, string> = {
    active: 'ok',
    succeeded: 'ok',
    forwarded: 'ok',
    ok: 'ok',
    paused: 'warn',
    paused_safety: 'warn',
    retry_scheduled: 'warn',
    pending: 'warn',
    needs_attention: 'warn',
    draft: 'muted',
    skipped: 'muted',
    disconnected: 'muted',
    error: 'bad',
    failed: 'bad',
    dead_letter: 'bad',
  }
  return (
    <span className={`pill pill--${tone[status] ?? 'muted'}`}>{status.replace(/_/g, ' ')}</span>
  )
}

export function Eligibility({ ok, reason }: { ok: boolean; reason: string }) {
  return (
    <span className={ok ? 'eligible' : 'ineligible'} title={reason}>
      {ok ? 'Yes' : 'No'}
      {!ok && reason ? <span className="reason"> — {reason}</span> : null}
    </span>
  )
}

interface AsyncStateProps<T> {
  query: { isPending: boolean; error: unknown; data: T | undefined }
  empty?: ReactNode
  children: (data: T) => ReactNode
}

export function AsyncState<T>({ query, empty, children }: AsyncStateProps<T>) {
  if (query.isPending) {
    return (
      <p className="state" role="status" aria-live="polite">
        Loading…
      </p>
    )
  }

  if (query.error) {
    const error = query.error
    if (error instanceof ApiError && error.status === 404) {
      return (
        <p className="state state--error" role="alert">
          Not found, or you do not have access to it.
        </p>
      )
    }
    if (error instanceof ApiError && error.status === 403) {
      return (
        <p className="state state--error" role="alert">
          You do not have permission to view this.
        </p>
      )
    }
    return (
      <p className="state state--error" role="alert">
        {error instanceof ApiError ? error.message : 'Something went wrong.'}
      </p>
    )
  }

  const data = query.data
  const isEmpty = data === undefined || (Array.isArray(data) && data.length === 0)
  if (isEmpty) return <div className="state state--empty">{empty ?? 'Nothing here yet.'}</div>

  return <>{children(data)}</>
}

export function ErrorBanner({ error }: { error: unknown }) {
  if (!error) return null
  const message = error instanceof ApiError ? error.message : 'Something went wrong.'
  const correlation = error instanceof ApiError ? error.correlationId : null
  return (
    <div className="banner banner--error" role="alert">
      <span>{message}</span>
      {correlation ? <code className="banner__id">{correlation}</code> : null}
    </div>
  )
}

export function Notice({ children, tone = 'info' }: { children: ReactNode; tone?: 'info' | 'warn' }) {
  return <div className={`banner banner--${tone}`}>{children}</div>
}

/** Destructive actions always require an explicit confirmation step. */
export function ConfirmButton({
  label,
  confirmLabel,
  onConfirm,
  disabled,
  danger = true,
}: {
  label: string
  confirmLabel: string
  onConfirm: () => void
  disabled?: boolean
  danger?: boolean
}) {
  const [armed, setArmed] = useState(false)

  if (!armed) {
    return (
      <button
        type="button"
        className={danger ? 'btn btn--danger' : 'btn'}
        disabled={disabled}
        onClick={() => setArmed(true)}
      >
        {label}
      </button>
    )
  }

  return (
    <span className="confirm">
      <span className="confirm__question">{confirmLabel}</span>
      <button
        type="button"
        className="btn btn--danger"
        disabled={disabled}
        onClick={() => {
          setArmed(false)
          onConfirm()
        }}
      >
        Confirm
      </button>
      <button type="button" className="btn btn--ghost" onClick={() => setArmed(false)}>
        Cancel
      </button>
    </span>
  )
}

export function RelativeTime({ value }: { value: string | null }) {
  if (!value) return <span className="muted">Never</span>
  const date = new Date(value)
  return <time dateTime={value} title={date.toLocaleString()}>{date.toLocaleString()}</time>
}
