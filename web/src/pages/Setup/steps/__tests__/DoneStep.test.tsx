/**
 * The setup wizard's last screen sends the user on to the dashboard, unless a
 * page that sent the user into setup is waiting for them back: then the
 * primary action continues back to that page instead.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { SETUP_RETURN_KEY } from '@/lib/setupReturn';

vi.mock('@/config/supportLinks', () => ({ PLATFORM_URL: '/account' }));
vi.mock('@/hooks/usePreferences', () => ({ usePreferences: () => ({ preferences: {} }) }));
vi.mock('@/hooks/useApiKeys', () => ({ useApiKeys: () => ({ apiKeys: null }) }));
vi.mock('@/components/model/useModelSlotCopy', () => ({
  useModelSlotCopy: () => ({ default: { summary: 'Default model' }, background: { summary: 'Background model' } }),
}));
// The readiness section is its own concern (it fetches, and is covered by its
// own tests); these tests are about the footer's offer and the return trip.
vi.mock('@/pages/Setup/steps/RuntimeReadiness', () => ({
  RuntimeReadiness: () => <div data-testid="runtime-readiness" />,
}));

import DoneStep from '../DoneStep';

const renderDone = () =>
  render(
    <MemoryRouter>
      <DoneStep />
    </MemoryRouter>,
  );

afterEach(() => sessionStorage.clear());

describe('DoneStep', () => {
  it('offers the dashboard when no return is kept', () => {
    renderDone();
    expect(screen.getByRole('button', { name: 'Go to Dashboard' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Continue' })).toBeNull();
  });

  it('shows the runtime readiness section under the summary card', () => {
    renderDone();
    expect(screen.getByTestId('runtime-readiness')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: "You're all set!" })).toBeInTheDocument();
  });

  it('swaps the dashboard for Continue while a return is kept', () => {
    sessionStorage.setItem(
      SETUP_RETURN_KEY,
      JSON.stringify({ url: `${window.location.origin}/account/onboarding/models`, at: Date.now() }),
    );
    renderDone();
    expect(screen.queryByRole('button', { name: 'Go to Dashboard' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Continue' })).toBeInTheDocument();
  });
});
