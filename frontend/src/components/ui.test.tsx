/**
 * Every page renders through AsyncState, so covering its four states here covers
 * the "loading, empty, error, permission-denied" requirement for all of them.
 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ApiError } from '../api/client'
import { AsyncState, Eligibility, ErrorBanner, StatusPill } from './ui'

const ok = <T,>(data: T) => ({ isPending: false, error: null, data })

describe('AsyncState', () => {
  it('shows a live-region loading state', () => {
    render(
      <AsyncState query={{ isPending: true, error: null, data: undefined }}>
        {() => <p>never</p>}
      </AsyncState>,
    )
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent('Loading…')
    expect(status).toHaveAttribute('aria-live', 'polite')
  })

  it('shows the empty state for an empty list', () => {
    render(<AsyncState query={ok([])} empty="No chats yet.">{() => <p>never</p>}</AsyncState>)
    expect(screen.getByText('No chats yet.')).toBeInTheDocument()
  })

  it('renders data when present', () => {
    render(<AsyncState query={ok(['one'])}>{(rows) => <p>{rows.join()}</p>}</AsyncState>)
    expect(screen.getByText('one')).toBeInTheDocument()
  })

  it('shows a not-found message rather than confirming another user’s object exists', () => {
    const error = new ApiError(404, 'not_found', 'No such forwarding rule.')
    render(
      <AsyncState query={{ isPending: false, error, data: undefined }}>{() => <p>never</p>}</AsyncState>,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('Not found, or you do not have access')
  })

  it('shows a permission-denied message for 403', () => {
    const error = new ApiError(403, 'csrf_failed', 'The request could not be verified.')
    render(
      <AsyncState query={{ isPending: false, error, data: undefined }}>{() => <p>never</p>}</AsyncState>,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('do not have permission')
  })

  it('surfaces a safe error message for other failures', () => {
    const error = new ApiError(422, 'destination_not_eligible', 'The connection cannot post here.')
    render(
      <AsyncState query={{ isPending: false, error, data: undefined }}>{() => <p>never</p>}</AsyncState>,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('The connection cannot post here.')
  })
})

describe('ErrorBanner', () => {
  it('renders nothing without an error', () => {
    const { container } = render(<ErrorBanner error={null} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows the correlation id so a failure can be traced', () => {
    const error = new ApiError(500, 'internal_error', 'Something went wrong.', {}, 'abc123')
    render(<ErrorBanner error={error} />)
    expect(screen.getByRole('alert')).toHaveTextContent('abc123')
  })

  it('never renders a raw provider string', () => {
    render(<ErrorBanner error={new Error('CHAT_WRITE_FORBIDDEN at telethon.tl')} />)
    expect(screen.getByRole('alert')).toHaveTextContent('Something went wrong.')
    expect(screen.queryByText(/telethon/)).toBeNull()
  })
})

describe('Eligibility', () => {
  it('explains why a chat is ineligible', () => {
    render(<Eligibility ok={false} reason="The bot must be an administrator." />)
    expect(screen.getByText(/must be an administrator/)).toBeInTheDocument()
  })

  it('shows nothing extra when eligible', () => {
    render(<Eligibility ok reason="Available." />)
    expect(screen.getByText('Yes')).toBeInTheDocument()
  })
})

describe('StatusPill', () => {
  it.each([
    ['active', 'pill--ok'],
    ['paused', 'pill--warn'],
    ['failed', 'pill--bad'],
    ['draft', 'pill--muted'],
    ['something_new', 'pill--muted'],
  ])('renders %s with the right tone', (status, expected) => {
    const { container } = render(<StatusPill status={status} />)
    expect(container.firstElementChild).toHaveClass(expected)
  })

  it('humanises underscored statuses', () => {
    render(<StatusPill status="needs_attention" />)
    expect(screen.getByText('needs attention')).toBeInTheDocument()
  })
})
