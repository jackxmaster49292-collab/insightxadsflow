import { Link, useNavigate, useParams } from 'react-router-dom'

import { useDeleteRule, useRule, useRuleCommand, useRuleEvents, useRuleJobs } from '../api/hooks'
import {
  AsyncState,
  ConfirmButton,
  Eligibility,
  ErrorBanner,
  Notice,
  RelativeTime,
  StatusPill,
} from '../components/ui'

export function RuleDetailPage() {
  const { ruleId = '' } = useParams()
  const navigate = useNavigate()

  const rule = useRule(ruleId)
  const events = useRuleEvents(ruleId)
  const jobs = useRuleJobs(ruleId)

  const activate = useRuleCommand(ruleId, 'activate')
  const pause = useRuleCommand(ruleId, 'pause')
  const resume = useRuleCommand(ruleId, 'resume')
  const retry = useRuleCommand(ruleId, 'retry-failed')
  const remove = useDeleteRule()

  return (
    <AsyncState query={rule}>
      {(data) => (
        <>
          <h1>{data.name}</h1>
          <p className="subtitle">
            <StatusPill status={data.status} /> · version {data.version} ·{' '}
            {data.forward_mode === 'forward' ? 'Forward' : 'Copy'} mode
          </p>

          {data.paused_reason_text ? <Notice tone="warn">{data.paused_reason_text}</Notice> : null}
          <ErrorBanner error={activate.error ?? pause.error ?? resume.error ?? retry.error} />

          <div className="preview">{data.preview}</div>

          <div className="btn-row" style={{ margin: '16px 0' }}>
            {data.status !== 'active' ? (
              <button
                type="button"
                className="btn btn--primary"
                disabled={activate.isPending || resume.isPending}
                onClick={() => (data.status === 'paused' ? resume.mutate() : activate.mutate())}
              >
                {data.status === 'paused' ? 'Resume' : 'Activate'}
              </button>
            ) : (
              <button type="button" className="btn" disabled={pause.isPending} onClick={() => pause.mutate()}>
                Pause
              </button>
            )}
            <Link className="btn" to={`/rules/${ruleId}/edit`}>
              Edit
            </Link>
            <button type="button" className="btn" disabled={retry.isPending} onClick={() => retry.mutate()}>
              Retry failed destinations
            </button>
            <ConfirmButton
              label="Delete"
              confirmLabel="Delete this rule? Its history is kept."
              onConfirm={() =>
                remove.mutate(ruleId, { onSuccess: () => navigate('/rules') })
              }
            />
          </div>

          <h2>Configuration</h2>
          <div className="card">
            <table>
              <tbody>
                <tr>
                  <th>Delay between destinations</th>
                  <td>{data.delay_ms} ms</td>
                </tr>
                <tr>
                  <th>Media types</th>
                  <td>{data.media_types.length ? data.media_types.join(', ') : 'All supported types'}</td>
                </tr>
                <tr>
                  <th>Required keywords</th>
                  <td>{data.keyword_include.length ? data.keyword_include.join(', ') : 'None'}</td>
                </tr>
                <tr>
                  <th>Excluded keywords</th>
                  <td>{data.keyword_exclude.length ? data.keyword_exclude.join(', ') : 'None'}</td>
                </tr>
                <tr>
                  <th>Keyword matching</th>
                  <td>{data.keyword_match_mode === 'word' ? 'Whole word' : 'Substring'}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <h2>Sources</h2>
          <div className="card">
            <table>
              <tbody>
                {data.sources.map((chat) => (
                  <tr key={chat.id}>
                    <td>{chat.title}</td>
                    <td className="mono muted">
                      {chat.peer_type}:{chat.peer_id}
                    </td>
                    <td>
                      <Eligibility ok={chat.eligible} reason={chat.reason_code} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <h2>Destinations</h2>
          <div className="card">
            <AsyncState query={jobs} empty="No deliveries attempted yet.">
              {(rows) => (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Destination</th>
                        <th>Status</th>
                        <th>Attempts</th>
                        <th>Detail</th>
                        <th>Updated</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((job) => (
                        <tr key={job.id}>
                          <td>{job.destination_title}</td>
                          <td>
                            <StatusPill status={job.status} />
                          </td>
                          <td>{job.attempt_count}</td>
                          <td className="muted">{job.last_error_text ?? '—'}</td>
                          <td>
                            <RelativeTime value={job.updated_at} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </AsyncState>
          </div>

          <h2>Recent forwarding events</h2>
          <div className="card">
            <AsyncState query={events} empty="No events recorded yet.">
              {(rows) => (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>When</th>
                        <th>Outcome</th>
                        <th>Attempt</th>
                        <th>Detail</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((event) => (
                        <tr key={event.id}>
                          <td>
                            <RelativeTime value={event.occurred_at} />
                          </td>
                          <td>
                            <StatusPill status={event.outcome} />
                          </td>
                          <td>{event.attempt}</td>
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
      )}
    </AsyncState>
  )
}
