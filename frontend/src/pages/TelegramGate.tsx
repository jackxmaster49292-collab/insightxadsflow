import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'

import { ApiError, api } from '../api/client'
import { keys } from '../api/hooks'
import { getInitData, telegramDisplayName } from '../api/telegram'
import { ErrorBanner } from '../components/ui'

/**
 * Signs in automatically when the panel is opened from the bot.
 *
 * The launch payload is signed by Telegram with a key derived from the bot
 * token, so the backend can verify who this is without any password. Being
 * verified is not the same as being allowed, though: the backend still checks
 * the admin allowlist, and a non-admin gets a clear refusal rather than a
 * confusing empty panel.
 */
export function TelegramGate() {
  const queryClient = useQueryClient()
  const [error, setError] = useState<unknown>(null)
  const attempted = useRef(false)

  useEffect(() => {
    // StrictMode double-invokes effects in development; one login attempt is enough.
    if (attempted.current) return
    attempted.current = true

    const initData = getInitData()
    if (!initData) return

    void (async () => {
      try {
        await api.post('/auth/telegram', { init_data: initData })
        await queryClient.invalidateQueries({ queryKey: keys.me })
      } catch (caught) {
        setError(caught)
      }
    })()
  }, [queryClient])

  const name = telegramDisplayName()
  const notAdmin = error instanceof ApiError && error.status === 403

  return (
    <div className="auth">
      <div className="card auth__card">
        <h1>Insight Store</h1>

        {!error ? (
          <p className="subtitle" role="status" aria-live="polite">
            Signing in{name ? ` as ${name}` : ''}…
          </p>
        ) : (
          <>
            <ErrorBanner error={error} />
            {notAdmin ? (
              <p className="muted" style={{ fontSize: 13 }}>
                Ask an existing operator to add your Telegram user ID to{' '}
                <code>ADMIN_TELEGRAM_IDS</code>, then reopen the panel from the bot.
              </p>
            ) : (
              <p className="muted" style={{ fontSize: 13 }}>
                Close this window and reopen the panel from the bot. Launch data expires,
                so an old window cannot be reused.
              </p>
            )}
          </>
        )}
      </div>
    </div>
  )
}
