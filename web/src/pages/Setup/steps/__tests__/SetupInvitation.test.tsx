/**
 * The invitation entry lives on the provider step now that it is the first
 * page. Redemption still goes to the platform auth service (`/api/auth/`, not
 * `/api/v1/`), and a 409 means this account already redeemed it.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router';
import { renderWithProviders } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';

const { post, getCurrentUser } = vi.hoisted(() => ({
  post: vi.fn(),
  getCurrentUser: vi.fn(),
}));

vi.mock('@/api/client', () => ({ api: { post } }));
vi.mock('@/lib/dashboardApi/api', () => ({ getCurrentUser }));
vi.mock('@/hooks/useUser', () => ({
  useUser: () => ({ user: { access_tier: -1 }, isLoading: false }),
}));

import SetupInvitation from '../SetupInvitation';

function renderInvitation() {
  const utils = renderWithProviders(
    <Routes>
      <Route path="/setup/provider" element={<SetupInvitation />} />
      <Route path="/setup/defaults" element={<p>defaults step</p>} />
    </Routes>,
    { route: '/setup/provider' },
  );
  return { invalidate: vi.spyOn(utils.queryClient, 'invalidateQueries') };
}

async function redeem(code: string) {
  const user = userEvent.setup();
  const rendered = renderInvitation();
  await user.click(screen.getByRole('button', { name: 'Have an invitation code?' }));
  await user.type(screen.getByPlaceholderText('Enter your invitation code'), code);
  await user.click(screen.getByRole('button', { name: 'Redeem' }));
  return rendered;
}

function rejectWith(status: number) {
  post.mockRejectedValueOnce({ response: { status, data: {} } });
}

describe('SetupInvitation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getCurrentUser.mockResolvedValue({});
  });

  it('redeems and refreshes access before moving on', async () => {
    post.mockResolvedValueOnce({ data: { ok: true } });
    const { invalidate } = await redeem('  INVITE-123 ');

    expect(await screen.findByText('defaults step')).toBeInTheDocument();
    expect(post).toHaveBeenCalledWith('/api/auth/invitations/redeem', { code: 'INVITE-123' });
    expect(getCurrentUser).toHaveBeenCalledWith({ refresh_tier: true });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.user.me() });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.platform.models() });
  });

  it('treats 409 as already redeemed', async () => {
    rejectWith(409);
    await redeem('USED-CODE');
    expect(await screen.findByText('defaults step')).toBeInTheDocument();
  });

  it('stays and explains an invalid code', async () => {
    rejectWith(404);
    await redeem('BAD');
    expect(await screen.findByText('Invalid invitation code.')).toBeInTheDocument();
    expect(screen.queryByText('defaults step')).not.toBeInTheDocument();
  });
});
