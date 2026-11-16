/**
 * The setup wizard's runtime-readiness section. The server owns the overall
 * verdict, this section only explains it: `ready` means required config and
 * local dependency checks (never an end-to-end model/sandbox call), and
 * `configuration_only` / `not_checked` / `unknown` are all weaker claims than
 * `observed` / `ok` and must be shown as such.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import type { ReadinessCheck, ReadinessResponse } from '@/pages/Setup/utils/api';

const { useReadinessMock } = vi.hoisted(() => ({ useReadinessMock: vi.fn() }));
vi.mock('@/hooks/useReadiness', () => ({ useReadiness: useReadinessMock }));

import { RuntimeReadiness } from '../RuntimeReadiness';

const refetch = vi.fn();

function mockReadiness(overrides: {
  readiness?: ReadinessResponse | null;
  isLoading?: boolean;
  isError?: boolean;
  error?: Error | null;
  isFetching?: boolean;
} = {}) {
  useReadinessMock.mockReturnValue({
    readiness: null,
    isLoading: false,
    isError: false,
    error: null,
    refetch,
    isFetching: false,
    ...overrides,
  });
}

function check(overrides: Partial<ReadinessCheck> & Pick<ReadinessCheck, 'id'>): ReadinessCheck {
  return {
    status: 'ok',
    required: true,
    summary: `Summary for ${overrides.id}`,
    verification: 'observed',
    ...overrides,
  };
}

function response(overrides: Partial<ReadinessResponse> = {}): ReadinessResponse {
  return {
    status: 'ready',
    checked_at: '2026-10-24T06:21:02.949755Z',
    verification: 'local_checks_only',
    checks: [],
    ...overrides,
  };
}

/** A ready answer whose optional sandbox session is unobserved. */
const READY = response({
  checks: [
    check({ id: 'database', summary: 'Dependency responded to a read-only check.' }),
    check({
      id: 'sandbox',
      summary: 'Sandbox provider configuration is present.',
      verification: 'configuration_only',
    }),
    check({
      id: 'sandbox_session',
      status: 'unknown',
      required: false,
      summary: 'No ready Home sandbox session is currently observed in this worker.',
      verification: 'not_checked',
    }),
  ],
});

function renderSection() {
  return renderWithProviders(<RuntimeReadiness />);
}

function rowOf(id: string) {
  return within(screen.getByTestId(`readiness-check-${id}`));
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('RuntimeReadiness — states', () => {
  it('shows the check-in-progress state with a skeleton and a disabled refresh', () => {
    mockReadiness({ isLoading: true, isFetching: true });
    renderSection();

    expect(screen.getByText('Checking runtime configuration…')).toBeInTheDocument();
    expect(screen.getByTestId('readiness-skeleton')).toBeInTheDocument();
    // The scope note is not a result, so it is present in every state.
    expect(screen.getByText(/Local checks only: end-to-end model and sandbox use are not verified here\./)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Checking…' })).toBeDisabled();
  });

  it('shows the failure state with retry, and never dumps the raw error', async () => {
    mockReadiness({ isError: true, error: new Error('boom: internal stack') });
    renderSection();

    expect(screen.getByText('Readiness check unavailable')).toBeInTheDocument();
    expect(screen.getByText('The readiness check did not return. Try again shortly.')).toBeInTheDocument();
    expect(screen.queryByText(/boom/)).toBeNull();

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  it('offers Recheck and disables it while a recheck is in flight', async () => {
    mockReadiness({ readiness: READY });
    const { unmount } = renderSection();

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Recheck' }));
    expect(refetch).toHaveBeenCalledTimes(1);
    unmount();

    mockReadiness({ readiness: READY, isFetching: true });
    renderSection();
    expect(screen.getByRole('button', { name: 'Checking…' })).toBeDisabled();
  });
});

describe('RuntimeReadiness — ready', () => {
  beforeEach(() => mockReadiness({ readiness: READY }));

  it('reports Ready but explicitly denies an end-to-end verification claim', () => {
    renderSection();

    expect(screen.getByRole('heading', { name: 'Runtime readiness' })).toBeInTheDocument();
    expect(screen.getByText('Ready')).toBeInTheDocument();
    expect(screen.getByText('Overall status')).toBeInTheDocument();
    expect(screen.getByText(/model and sandbox calls are still unverified/)).toBeInTheDocument();
    expect(
      screen.getByText(/Local checks only: end-to-end model and sandbox use are not verified here\./),
    ).toBeInTheDocument();
    // No check ever renders a bare "Verified" claim.
    expect(screen.queryByText('Verified')).toBeNull();
  });

  it('distinguishes an observed check from a configuration-only one', () => {
    renderSection();

    expect(screen.getByText('Observed by this check.')).toBeInTheDocument();
    expect(
      screen.getByText('Shows that configuration or credentials exist; validity and reachability were not verified.'),
    ).toBeInTheDocument();
    expect(rowOf('database').getByText('Observed')).toBeInTheDocument();
    expect(rowOf('sandbox').getByText('Configuration only')).toBeInTheDocument();
  });

  it('renders an unobserved optional sandbox session as a neutral note, not a blocking error', () => {
    renderSection();

    const row = rowOf('sandbox_session');
    expect(row.getByText('Sandbox session')).toBeInTheDocument();
    expect(row.getByText('Optional')).toBeInTheDocument();
    expect(row.getByText('Unknown')).toBeInTheDocument();
    expect(row.getByText('This check returned no definite result.')).toBeInTheDocument();
    expect(row.getByText(/No local session is observed in this worker; this is not a blocking error\./)).toBeInTheDocument();
    expect(row.queryByText('Missing')).toBeNull();
    // The overall verdict is the server's Ready, not a sync of the checks.
    expect(screen.queryByText('Action needed')).toBeNull();
  });

  it('formats checked_at as the check time', () => {
    renderSection();

    expect(screen.getByText(/^Checked at /)).toBeInTheDocument();
    expect(screen.getByText('Time of this check.')).toBeInTheDocument();
    expect(screen.getByText('Keys and tokens are never shown here.')).toBeInTheDocument();
  });
});

describe('RuntimeReadiness — needs action', () => {
  it('reports Action needed and links a missing primary model to model settings', async () => {
    mockReadiness({
      readiness: response({
        status: 'needs_action',
        checks: [
          check({ id: 'database' }),
          check({
            id: 'primary_model',
            status: 'missing',
            required: true,
            summary: 'No usable credential is configured for the selected primary model.',
            verification: 'configuration_only',
          }),
        ],
      }),
    });
    renderSection();

    expect(screen.getByText('Action needed')).toBeInTheDocument();
    expect(screen.getByText(/A required setting is missing/)).toBeInTheDocument();

    const row = rowOf('primary_model');
    expect(row.getByText('Primary model')).toBeInTheDocument();
    expect(row.getByText('Missing')).toBeInTheDocument();
    expect(row.getByText('Not configured; related features may be unavailable.')).toBeInTheDocument();
    expect(row.getByText('No usable credential is configured for the selected primary model.')).toBeInTheDocument();

    const link = row.getByRole('link', { name: 'Check model settings' });
    expect(link).toHaveAttribute('href', '/settings?tab=model');
  });
});

describe('RuntimeReadiness — degraded and unknown', () => {
  it('reports Limited and explains a degraded check distinctly', () => {
    mockReadiness({
      readiness: response({
        status: 'degraded',
        checks: [check({ id: 'mcp', status: 'degraded', summary: 'Built-in MCP did not answer.' })],
      }),
    });
    renderSection();

    expect(screen.getByText('Limited')).toBeInTheDocument();
    expect(screen.getByText(/Some local checks are unhealthy/)).toBeInTheDocument();
    expect(rowOf('mcp').getByText('Degraded')).toBeInTheDocument();
    expect(rowOf('mcp').getByText('The check reported a problem that may affect use.')).toBeInTheDocument();
  });

  it('explains an unknown required check without inferring a cause', () => {
    mockReadiness({
      readiness: response({
        status: 'degraded',
        checks: [
          check({
            id: 'redis',
            status: 'unknown',
            summary: 'The check returned no result.',
            verification: 'not_checked',
          }),
        ],
      }),
    });
    renderSection();

    const row = rowOf('redis');
    expect(row.getByText('Unknown')).toBeInTheDocument();
    expect(row.getByText('This check returned no definite result.')).toBeInTheDocument();
    expect(row.getByText('This check did not verify it.')).toBeInTheDocument();
  });

  it('falls back to the generic label and summary for an unknown check shape', () => {
    mockReadiness({
      readiness: response({
        status: 'degraded',
        checks: [check({ id: 'some_new_check', summary: '', verification: 'not_checked' })],
      }),
    });
    renderSection();

    const row = rowOf('some_new_check');
    expect(row.getByText('Other check')).toBeInTheDocument();
    expect(row.getByText('No summary provided')).toBeInTheDocument();
  });
});

describe('RuntimeReadiness — timestamps and summaries', () => {
  it('renders an empty checked_at as —', () => {
    mockReadiness({ readiness: response({ checked_at: '' }) });
    renderSection();

    expect(screen.getByText('Checked at —')).toBeInTheDocument();
  });

  it('renders an unparseable checked_at as —', () => {
    mockReadiness({ readiness: response({ checked_at: 'not-a-timestamp' }) });
    renderSection();

    expect(screen.getByText('Checked at —')).toBeInTheDocument();
  });
});

describe('RuntimeReadiness — payload discipline', () => {
  it('renders only known fields: extra token/url fields never reach the DOM', () => {
    const payload = {
      ...response(),
      token: 'super-secret-token',
      url: 'https://internal.example.invalid/readiness',
      checks: [
        {
          ...check({ id: 'database' }),
          token: 'super-secret-token',
          url: 'https://internal.example.invalid/check',
        },
      ],
    } as unknown as ReadinessResponse;
    mockReadiness({ readiness: payload });
    renderSection();

    expect(screen.queryByText(/super-secret-token/)).toBeNull();
    expect(screen.queryByText(/internal\.example\.invalid/)).toBeNull();
    expect(
      screen.queryByText('https://internal.example.invalid/readiness'),
    ).toBeNull();
    expect(screen.queryByText('Verified')).toBeNull();
  });
});
