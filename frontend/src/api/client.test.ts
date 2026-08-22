import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api, newIdempotencyKey } from './client'

function mockFetch(response: Partial<Response> & { jsonBody?: unknown }) {
  const body = response.jsonBody === undefined ? '' : JSON.stringify(response.jsonBody)
  const fetchMock = vi.fn().mockResolvedValue({
    ok: response.ok ?? true,
    status: response.status ?? 200,
    text: () => Promise.resolve(body),
    headers: new Headers(),
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
  document.cookie = 'insight_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT'
})

describe('api client', () => {
  it('sends credentials so the session cookie is included', async () => {
    const fetchMock = mockFetch({ jsonBody: { ok: true } })
    await api.get('/me')
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ credentials: 'include' })
  })

  it('attaches the CSRF double-submit header on mutations only', async () => {
    document.cookie = 'insight_csrf=token-value'

    const post = mockFetch({ jsonBody: {} })
    await api.post('/forwarding-rules', { name: 'x' })
    expect(post.mock.calls[0]?.[1].headers['X-CSRF-Token']).toBe('token-value')

    const get = mockFetch({ jsonBody: {} })
    await api.get('/forwarding-rules')
    expect(get.mock.calls[0]?.[1].headers['X-CSRF-Token']).toBeUndefined()
  })

  it('attaches an Idempotency-Key when one is supplied', async () => {
    const fetchMock = mockFetch({ jsonBody: {} })
    await api.post('/forwarding-rules/1/activate', undefined, 'key-123')
    expect(fetchMock.mock.calls[0]?.[1].headers['Idempotency-Key']).toBe('key-123')
  })

  it('turns the error envelope into a typed ApiError', async () => {
    mockFetch({
      ok: false,
      status: 422,
      jsonBody: {
        error: {
          code: 'destination_not_eligible',
          message: 'The connection cannot post in this chat.',
          correlation_id: 'corr-1',
          details: { chat_id: 'abc' },
        },
      },
    })

    await expect(api.post('/forwarding-rules')).rejects.toMatchObject({
      status: 422,
      code: 'destination_not_eligible',
      correlationId: 'corr-1',
      details: { chat_id: 'abc' },
    })
  })

  it('flags unauthenticated responses', async () => {
    mockFetch({ ok: false, status: 401, jsonBody: { error: { code: 'not_authenticated' } } })
    const error = await api.get('/me').catch((caught) => caught)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).isUnauthenticated).toBe(true)
  })

  it('handles a 204 with no body', async () => {
    mockFetch({ status: 204 })
    await expect(api.delete('/forwarding-rules/1')).resolves.toBeUndefined()
  })

  it('generates distinct idempotency keys', () => {
    expect(newIdempotencyKey()).not.toBe(newIdempotencyKey())
  })
})
