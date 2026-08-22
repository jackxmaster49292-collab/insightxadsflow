import { Link } from 'react-router-dom'

import { useActivity, useConnections, useRules, useUsage } from '../api/hooks'
import { AsyncState, Notice, RelativeTime, StatusPill } from '../components/ui'

export function HomePage() {
  const connections = useConnections()
  const rules = useRules()
  const usage = useUsage('24h')
  const activity = useActivity()

  const unhealthy = (connections.data ?? []).filter(
    (connection) => connection.status !== 'active' && connection.status !== 'pending',
  )
  const pausedRules = (rules.data ?? []).filter((rule) => rule.status === 'paused')
  const activeRules = (rules.data ?? []).filter((rule) => rule.status === 'active')

  return (
    <>
      <h1>Home</h1>
      <p className="subtitle">Connection health, active rules, and recent forwarding activity.</p>

      {unhealthy.map((connection) => (
        <Notice key={connection.id} tone="warn">
          <strong>{connection.label}</strong> is {connection.status.replace(/_/g, ' ')}.{' '}
          {connection.last_error_message_safe ?? 'Reconnect it to resume forwarding.'}{' '}
          <Link to="/connections">Manage connections</Link>
        </Notice>
      ))}

      {pausedRules.map((rule) => (
        <Notice key={rule.id} tone="warn">
          <strong>{rule.name}</strong> is paused. {rule.paused_reason_text ?? ''}{' '}
          <Link to={`/rules/${rule.id}`}>Open rule</Link>
        </Notice>
      ))}

      <div className="grid">
        <div className="card">
          <div className="stat__label">Connections</div>
          <div className="stat__value">{connections.data?.length ?? '—'}</div>
        </div>
        <div className="card">
          <div className="stat__label">Active rules</div>
          <div className="stat__value">{activeRules.length}</div>
        </div>
        <div className="card">
          <div className="stat__label">Forwarded (24h)</div>
          <div className="stat__value">{usage.data?.forwarded ?? '—'}</div>
        </div>
        <div className="card">
          <div className="stat__label">Skipped (24h)</div>
          <div className="stat__value">{usage.data?.skipped ?? '—'}</div>
        </div>
        <div className="card">
          <div className="stat__label">Failed (24h)</div>
          <div className="stat__value">{usage.data?.failed ?? '—'}</div>
        </div>
      </div>

      <p className="muted" style={{ fontSize: 12 }}>
        These are operational counters, not quotas. This product has no usage limits of its own —
        but Telegram may still restrict an account or operation.
      </p>

      <div className="btn-row" style={{ margin: '18px 0' }}>
        <Link className="btn btn--primary" to="/connections">
          Connect Telegram
        </Link>
        <Link className="btn" to="/rules/new">
          Create a forwarding rule
        </Link>
      </div>

      <h2>Recent activity</h2>
      <div className="card">
        <AsyncState query={activity} empty="No forwarding activity yet.">
          {(events) => (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Outcome</th>
                    <th>Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {events.slice(0, 15).map((event) => (
                    <tr key={event.id}>
                      <td>
                        <RelativeTime value={event.occurred_at} />
                      </td>
                      <td>
                        <StatusPill status={event.outcome} />
                      </td>
                      <td>{event.detail_safe ?? event.reason_text}</td>
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
