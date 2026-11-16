/**
 * Where a connect's answer is said once it lands.
 *
 * A sign-in slower than its flow's state lives comes back to the bare Plugins
 * page, since the server's record of where to return went with the state.
 * The connect's own record kept it, so the failure is said where the connect
 * started rather than dropping the user out of what they were doing.
 */
import { beforeEach, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';

vi.mock('@/components/ui/use-toast', () => ({ toast: vi.fn() }));

const { rememberConnect } = await import('../connectReturn');
const { useConnectLanding } = await import('../hooks/useConnectLanding');

beforeEach(() => {
  sessionStorage.clear();
});

it('forgets a landing and the connects this tab sent out when the account changes', async () => {
  const { runAuthResets } = await import('@/lib/authResets');
  const { setConnectLanding } = await import('../hooks/useConnectLanding');
  rememberConnect({
    server: 'acme',
    label: 'Acme',
    retry: { kind: 'other' },
    desktopOnly: false,
    returnTo: null,
  });
  setConnectLanding({
    owner: 'page',
    draft: { server: 'acme', label: 'Acme', retry: { kind: 'other' }, desktopOnly: false },
    outcome: 'failed',
    reasonKey: undefined,
  });
  const { result, rerender } = renderHook(() => useConnectLanding(['other']));
  expect(result.current).not.toBeNull();

  runAuthResets();
  rerender();

  expect(result.current).toBeNull();
  expect(sessionStorage.getItem('mcp:connect-started')).toBeNull();
});
