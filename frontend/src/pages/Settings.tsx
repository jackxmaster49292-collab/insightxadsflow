import { useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'

import { api } from '../api/client'
import { useAuditEvents, useConnections, useMe } from '../api/hooks'
import { AsyncState, ConfirmButton, Notice, RelativeTime } from '../components/ui'

export function SettingsPage() {
  const me = useMe()
  const connections = useConnections()
  const audit = useAuditEvents()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  async function revokeAllSessions() {
    await api.post('/auth/revoke-all')
    queryClient.clear()
    navigate('/login')
  }

  async function exportData() {
    const [chats, rules, events] = await Promise.all([
      api.get('/telegram/chats'),
      api.get('/forwarding-rules'),
      api.get('/activity?limit=200'),
    ])
    const payload = {
      exported_at: new Date().toISOString(),
      account: me.data,
      connections: connections.data,
      chats,
      rules,
      recent_events: events,
    }
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }),
    )
    const link = document.createElement('a')
    link.href = url
    link.download = 'insight-store-export.json'
    link.click()
    URL.revokeObjectURL(url)
  }

  return (
    <>
      <h1>Settings</h1>
      <p className="subtitle">Account, security, and data.</p>

      <div className="card">
        <strong>Account</strong>
        <table>
          <tbody>
            <tr>
              <th>Email</th>
              <td>{me.data?.email ?? '—'}</td>
            </tr>
            <tr>
              <th>Timezone</th>
              <td>{me.data?.timezone ?? 'UTC'}</td>
            </tr>
            <tr>
              <th>Connections</th>
              <td>{me.data?.connection_count ?? 0}</td>
            </tr>
            <tr>
              <th>Active rules</th>
              <td>{me.data?.active_rule_count ?? 0}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="card">
        <strong>Security</strong>
        <p className="muted" style={{ fontSize: 13 }}>
          Signing out of all sessions takes effect immediately — sessions are stored server-side, so
          revocation is real rather than waiting for a token to expire.
        </p>
        <div className="btn-row">
          <ConfirmButton
            label="Sign out of all sessions"
            confirmLabel="Sign out everywhere, including here?"
            danger={false}
            onConfirm={() => void revokeAllSessions()}
          />
        </div>
      </div>

      <div className="card">
        <strong>Notifications</strong>
        <p className="muted" style={{ fontSize: 13 }}>
          Alerts appear in this dashboard: paused rules and unhealthy connections are surfaced on
          Home. Email and Telegram alerts are not part of this release.
        </p>
      </div>

      <div className="card">
        <strong>Your data</strong>
        <p className="muted" style={{ fontSize: 13 }}>
          The export contains your account, chats, rules, and recent events. It never contains bot
          tokens, Telegram session material, or message content.
        </p>
        <div className="btn-row">
          <button type="button" className="btn" onClick={() => void exportData()}>
            Export as JSON
          </button>
        </div>
        <Notice tone="warn">
          To delete your account, first disconnect and revoke each Telegram connection on the
          Connections page, then contact your operator. Account deletion is not self-service in this
          release.
        </Notice>
      </div>

      <h2>Audit log</h2>
      <div className="card">
        <AsyncState query={audit} empty="No audited actions yet.">
          {(rows) => (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Action</th>
                    <th>Object</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((event) => (
                    <tr key={event.id}>
                      <td>
                        <RelativeTime value={event.created_at} />
                      </td>
                      <td className="mono">{event.action}</td>
                      <td className="muted">{event.object_type}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </AsyncState>
      </div>
    </>
  )
}
