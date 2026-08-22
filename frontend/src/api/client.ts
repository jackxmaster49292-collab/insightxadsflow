/**
 * Thin fetch wrapper.
 *
 * Responsibilities kept here so no page has to remember them:
 *  - send the session cookie (`credentials: 'include'`),
 *  - attach the CSRF double-submit header on mutations,
 *  - attach an `Idempotency-Key` on control commands so a retry after a flaky
 *    network cannot double-apply,
 *  - surface the backend's safe error envelope as a typed error.
 */

const BASE = '/api/v1'

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: Record<string, unknown> = {},
    readonly correlationId: string | null = null,
  ) {
    super(message)
    this.name = 'ApiError'
  }

  /** True when the customer simply needs to sign in again. */
  get isUnauthenticated(): boolean {
    return this.status === 401
  }
}

function readCookie(name: string): string {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`))
  return match?.[1] ? decodeURIComponent(match[1]) : ''
}

type Method = 'GET' | 'POST' | 'PATCH' | 'DELETE'

interface RequestOptions {
  body?: unknown
  /** Pass a stable key to make a control command safely retryable. */
  idempotencyKey?: string
}

async function request<T>(method: Method, path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {}

  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  if (method !== 'GET') headers['X-CSRF-Token'] = readCookie('insight_csrf')
  if (options.idempotencyKey) headers['Idempotency-Key'] = options.idempotencyKey

  const response = await fetch(`${BASE}${path}`, {
    method,
    headers,
    credentials: 'include',
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  })

  if (response.status === 204) return undefined as T

  const text = await response.text()
  const payload = text ? JSON.parse(text) : null

  if (!response.ok) {
    const error = payload?.error ?? {}
    throw new ApiError(
      response.status,
      error.code ?? 'unknown_error',
      error.message ?? 'Something went wrong.',
      error.details ?? {},
      error.correlation_id ?? response.headers.get('X-Correlation-Id'),
    )
  }

  return payload as T
}

export function newIdempotencyKey(): string {
  return crypto.randomUUID()
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown, idempotencyKey?: string) =>
    request<T>('POST', path, { body, idempotencyKey }),
  patch: <T>(path: string, body?: unknown) => request<T>('PATCH', path, { body }),
  delete: <T>(path: string) => request<T>('DELETE', path),
}
