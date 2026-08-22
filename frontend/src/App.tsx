import { useQueryClient } from '@tanstack/react-query'
import { NavLink, Navigate, Route, Routes, useNavigate } from 'react-router-dom'

import { api } from './api/client'
import { useMe } from './api/hooks'
import { isInsideTelegram } from './api/telegram'
import { ChatsPage } from './pages/Chats'
import { ConnectionsPage } from './pages/Connections'
import { HomePage } from './pages/Home'
import { LoginPage } from './pages/Login'
import { RuleDetailPage } from './pages/RuleDetail'
import { RuleEditorPage } from './pages/RuleEditor'
import { RulesPage } from './pages/Rules'
import { SettingsPage } from './pages/Settings'
import { TelegramGate } from './pages/TelegramGate'

function Shell() {
  const me = useMe()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  async function signOut() {
    await api.post('/auth/logout')
    queryClient.clear()
    navigate('/login')
  }

  return (
    <div className="layout">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <aside className="sidebar">
        <div className="sidebar__brand">Insight Store</div>
        <div className="sidebar__tagline">Telegram forwarding</div>
        <nav aria-label="Main">
          <NavLink to="/" end>
            Home
          </NavLink>
          <NavLink to="/connections">Telegram connections</NavLink>
          <NavLink to="/chats">Chats</NavLink>
          <NavLink to="/rules">Forwarding rules</NavLink>
          <NavLink to="/settings">Settings</NavLink>
        </nav>
        <div className="sidebar__footer">
          <div className="muted mono">{me.data?.email ?? '…'}</div>
          <button type="button" className="btn btn--ghost" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </aside>

      <main className="main" id="main">
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/connections" element={<ConnectionsPage />} />
          <Route path="/chats" element={<ChatsPage />} />
          <Route path="/rules" element={<RulesPage />} />
          <Route path="/rules/new" element={<RuleEditorPage />} />
          <Route path="/rules/:ruleId" element={<RuleDetailPage />} />
          <Route path="/rules/:ruleId/edit" element={<RuleEditorPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<p className="state">Page not found.</p>} />
        </Routes>
      </main>
    </div>
  )
}

export function App() {
  const me = useMe()

  if (me.isPending) {
    return (
      <p className="state" role="status">
        Loading…
      </p>
    )
  }

  if (me.error) {
    // Inside Telegram there is no password to ask for — authenticate with the
    // signed launch payload instead of showing a login form.
    if (isInsideTelegram()) return <TelegramGate />

    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    )
  }

  return (
    <Routes>
      <Route path="/login" element={<Navigate to="/" replace />} />
      <Route path="*" element={<Shell />} />
    </Routes>
  )
}
