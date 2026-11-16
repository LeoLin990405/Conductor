import { describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';

const api = vi.hoisted(() => ({
  getLocalVersion: vi.fn(),
  checkLocalUpdate: vi.fn(),
  applyLocalUpdate: vi.fn(),
  restartLocalUpdate: vi.fn(),
}));

vi.mock('@/api/systemUpdates', () => api);

import { AboutTab } from '../AboutTab';

describe('AboutTab', () => {
  it('shows the installed commit and offers a check when a checkout is available', async () => {
    api.getLocalVersion.mockResolvedValue({
      supported: true,
      enabled: true,
      version: '0.1.3',
      commit: 'abc1234',
      branch: 'main',
      dirty: false,
    });

    renderWithProviders(<AboutTab />);

    expect(await screen.findByText('0.1.3')).toBeInTheDocument();
    expect(screen.getByText('abc1234')).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Check for updates' })).toBeEnabled(),
    );
  });

  it('shows an actionable error when the tree is dirty', async () => {
    api.getLocalVersion.mockResolvedValue({
      supported: true,
      enabled: true,
      version: '0.1.3',
      commit: 'abc1234',
      branch: 'main',
      dirty: true,
    });

    renderWithProviders(<AboutTab />);

    expect(await screen.findByText(/Local changes/)).toBeInTheDocument();
    expect(screen.getByText(/update blocked/i)).toBeInTheDocument();
  });
});
