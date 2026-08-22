import { useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useState } from 'react'

import { ApiError, api } from '../api/client'
import { keys } from '../api/hooks'
import { ErrorBanner } from '../components/ui'

export function LoginPage() {
  const queryClient = useQueryClient()
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await api.post(`/auth/${mode}`, { email, password })
      await queryClient.invalidateQueries({ queryKey: keys.me })
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="auth">
      <form className="card auth__card" onSubmit={submit}>
        <h1>Insight Store</h1>
        <p className="subtitle">
          {mode === 'login' ? 'Sign in to your control panel.' : 'Create your account.'}
        </p>

        <ErrorBanner error={error} />

        <label htmlFor="email">Email</label>
        <input
          id="email"
          type="email"
          autoComplete="username"
          required
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />

        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
          required
          minLength={mode === 'register' ? 12 : undefined}
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
        {mode === 'register' ? (
          <p className="muted" style={{ fontSize: 13, marginTop: 6 }}>
            At least 12 characters.
          </p>
        ) : null}

        <div className="btn-row" style={{ marginTop: 18 }}>
          <button type="submit" className="btn btn--primary" disabled={busy}>
            {busy ? 'Please wait…' : mode === 'login' ? 'Sign in' : 'Create account'}
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => {
              setMode(mode === 'login' ? 'register' : 'login')
              setError(null)
            }}
          >
            {mode === 'login' ? 'Create an account' : 'I already have an account'}
          </button>
        </div>

        {mode === 'login' && error instanceof ApiError && error.status === 429 ? (
          <p className="muted" style={{ fontSize: 13 }}>
            Too many attempts. Wait a few minutes before trying again.
          </p>
        ) : null}

        <p className="muted" style={{ fontSize: 12, marginTop: 22 }}>
          Password recovery is not available in this release. If you lose access, an operator must
          reset the account.
        </p>
      </form>
    </div>
  )
}
