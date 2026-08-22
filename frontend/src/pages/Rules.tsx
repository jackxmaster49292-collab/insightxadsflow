import { Link } from 'react-router-dom'

import { useRules } from '../api/hooks'
import { AsyncState, RelativeTime, StatusPill } from '../components/ui'

export function RulesPage() {
  const rules = useRules()

  return (
    <>
      <h1>Forwarding rules</h1>
      <p className="subtitle">
        Each rule watches one or more source chats and forwards eligible new messages to its
        destinations while you are offline.
      </p>

      <div className="btn-row" style={{ marginBottom: 14 }}>
        <Link className="btn btn--primary" to="/rules/new">
          Create rule
        </Link>
      </div>

      <div className="card">
        <AsyncState query={rules} empty="No forwarding rules yet.">
          {(rows) => (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Rule</th>
                    <th>Sources</th>
                    <th>Destinations</th>
                    <th>Filters</th>
                    <th>Status</th>
                    <th>Last activity</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((rule) => (
                    <tr key={rule.id}>
                      <td>
                        <Link to={`/rules/${rule.id}`}>{rule.name}</Link>
                      </td>
                      <td>{rule.source_titles.join(', ') || <span className="muted">None</span>}</td>
                      <td>{rule.destination_count}</td>
                      <td className="muted">{rule.filter_summary}</td>
                      <td>
                        <StatusPill status={rule.status} />
                        {rule.paused_reason_text ? (
                          <div className="muted" style={{ fontSize: 12 }}>
                            {rule.paused_reason_text}
                          </div>
                        ) : null}
                      </td>
                      <td>
                        <RelativeTime value={rule.last_activity_at} />
                      </td>
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
