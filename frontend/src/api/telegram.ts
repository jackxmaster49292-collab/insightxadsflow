/**
 * Telegram Mini App integration.
 *
 * When the panel is opened from the bot, Telegram injects `window.Telegram.WebApp`
 * carrying a signed `initData` string. We send that to the backend, which verifies
 * the HMAC against the admin bot token and issues a normal session cookie — so
 * every other API call in the app works unchanged, with no password anywhere.
 *
 * `initDataUnsafe` is deliberately never used for anything but cosmetics:
 * Telegram's own docs warn it must not be trusted.
 */

interface TelegramWebApp {
  initData: string
  initDataUnsafe?: { user?: { id: number; username?: string; first_name?: string } }
  version: string
  platform: string
  colorScheme: 'light' | 'dark'
  ready: () => void
  expand: () => void
  close: () => void
  MainButton?: { hide: () => void }
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp }
  }
}

export function getWebApp(): TelegramWebApp | null {
  return window.Telegram?.WebApp ?? null
}

/** True when the panel is running inside Telegram with a usable launch payload. */
export function isInsideTelegram(): boolean {
  const app = getWebApp()
  return Boolean(app && app.initData)
}

/** Cosmetic only — never used for authorization. */
export function telegramDisplayName(): string | null {
  const user = getWebApp()?.initDataUnsafe?.user
  if (!user) return null
  return user.username ? `@${user.username}` : (user.first_name ?? null)
}

export function initTelegramChrome(): void {
  const app = getWebApp()
  if (!app) return
  app.ready()
  app.expand()
  // Telegram supplies the theme; matching it avoids a jarring white flash.
  document.documentElement.dataset.telegramTheme = app.colorScheme
}

export function getInitData(): string | null {
  return getWebApp()?.initData || null
}
