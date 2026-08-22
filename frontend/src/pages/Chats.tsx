import { useState } from 'react'

import { api } from '../api/client'
import { useChats, useConnections } from '../api/hooks'
import { AsyncState, Eligibility, Notice, RelativeTime } from '../components/ui'

export function ChatsPage() {
  const connections = useConnections()
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [notice, setNotice] = useState<string | null>(null)

  const chats = useChats(filters)

  function set(key: string, value: string) {
    setFilters((current) => {
      const next = { ...current }
      if (value === '') delete next[key]
      else next[key] = value
      return next
    })
  }

  async function recheck(chatId: string) {
    const result = await api.post<{ message: string }>(`/telegram/chats/${chatId}/check-access`)
    setNotice(result.message)
  }

  return (
    <>
      <h1>Chats</h1>
      <p className="subtitle">
        Appearing here does not grant eligibility. Access is checked explicitly, and revalidated
        again immediately before every delivery.
      </p>

      {notice ? <Notice>{notice}</Notice> : null}

      <div className="filters">
        <div>
          <label htmlFor="f-conn">Connection</label>
          <select id="f-conn" onChange={(event) => set('connection_id', event.target.value)}>
            <option value="">All</option>
            {(connections.data ?? []).map((connection) => (
              <option key={connection.id} value={connection.id}>
                {connection.label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="f-src">Source eligible</label>
          <select id="f-src" onChange={(event) => set('source_eligible', event.target.value)}>
            <option value="">Any</option>
            <option value="true">Yes</option>
            <option value="false">No</option>
          </select>
        </div>
        <div>
          <label htmlFor="f-dst">Destination eligible</label>
          <select id="f-dst" onChange={(event) => set('destination_eligible', event.target.value)}>
            <option value="">Any</option>
            <option value="true">Yes</option>
            <option value="false">No</option>
          </select>
        </div>
        <div>
          <label htmlFor="f-type">Type</label>
          <select id="f-type" onChange={(event) => set('type', event.target.value)}>
            <option value="">Any</option>
            <option value="channel">Channel</option>
            <option value="supergroup">Supergroup</option>
            <option value="group">Group</option>
            <option value="private">Private</option>
          </select>
        </div>
        <div>
          <label htmlFor="f-public">Visibility</label>
          <select id="f-public" onChange={(event) => set('is_public', event.target.value)}>
            <option value="">Any</option>
            <option value="true">Public</option>
            <option value="false">Private</option>
          </select>
        </div>
        <div>
          <label htmlFor="f-active">State</label>
          <select id="f-active" onChange={(event) => set('is_active', event.target.value)}>
            <option value="">Any</option>
            <option value="true">Active</option>
            <option value="false">Inactive</option>
          </select>
        </div>
        <div>
          <label htmlFor="f-error">Has error</label>
          <select id="f-error" onChange={(event) => set('has_error', event.target.value)}>
            <option value="">Any</option>
            <option value="true">Yes</option>
            <option value="false">No</option>
          </select>
        </div>
        <div>
          <label htmlFor="f-q">Search</label>
          <input id="f-q" type="text" onChange={(event) => set('q', event.target.value)} />
        </div>
      </div>

      <div className="card">
        <AsyncState
          query={chats}
          empty="No chats match. Synchronize a connection on the Connections page first."
        >
          {(rows) => (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Chat</th>
                    <th>Type</th>
                    <th>Source</th>
                    <th>Destination</th>
                    <th>Synced</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((chat) => (
                    <tr key={chat.id}>
                      <td>
                        <div>
                          {chat.title}
                          {chat.has_protected_content ? (
                            <span className="chip" style={{ marginLeft: 6 }}>
                              protected
                            </span>
                          ) : null}
                          {!chat.is_active ? (
                            <span className="chip" style={{ marginLeft: 6 }}>
                              inactive
                            </span>
                          ) : null}
                        </div>
                        <div className="mono muted">
                          {chat.peer_type}:{chat.peer_id}
                        </div>
                      </td>
                      <td>
                        {chat.chat_kind}
                        <div className="muted" style={{ fontSize: 12 }}>
                          {chat.is_public === null ? '' : chat.is_public ? 'public' : 'private'}
                        </div>
                      </td>
                      <td>
                        <Eligibility ok={chat.source_eligible} reason={chat.source_reason_text} />
                      </td>
                      <td>
                        <Eligibility
                          ok={chat.destination_eligible}
                          reason={chat.destination_reason_text}
                        />
                      </td>
                      <td>
                        <RelativeTime value={chat.last_synced_at} />
                      </td>
                      <td>
                        <button type="button" className="btn btn--ghost" onClick={() => void recheck(chat.id)}>
                          Re-check
                        </button>
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
