/**
 * The BigModel redirect landing page: read the code/state once, exchange them
 * once, then leave for Settings without the credential-bearing query riding
 * along. Failures keep the provider's detail off screen.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes, useLocation } from 'react-router';
import { renderWithProviders } from '@/test/utils';

const h = vi.hoisted(() => ({
  submit: vi.fn(),
  redirectUri: 'http://localhost:3000/oauth/zhipu',
}));

vi.mock('@/lib/dashboardApi/api', () => ({ submitZhipuCallback: h.submit }));
vi.mock('@/components/model/useSubscriptionLogin', () => ({
  zhipuRedirectUri: () => h.redirectUri,
}));

import ZhipuCallback from '../ZhipuCallback';

/** Settings stand-in that exposes the query it was reached with. */
function SettingsProbe() {
  const location = useLocation();
  return <p>{`settings page${location.search}`}</p>;
}

function renderCallback(route: string) {
  return renderWithProviders(
    <Routes>
      <Route path="/oauth/zhipu" element={<ZhipuCallback />} />
      <Route path="/settings" element={<SettingsProbe />} />
    </Routes>,
    { route },
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('Zhipu callback page', () => {
  it('exchanges code+state once and leaves for Settings without the credential query', async () => {
    h.submit.mockResolvedValue({ success: true, provider: 'bigmodel', email: 'z@example.com' });
    renderCallback('/oauth/zhipu?code=abc-123&state=st-1');

    expect(await screen.findByText('Signed in. Taking you to Settings...')).toBeInTheDocument();
    expect(h.submit).toHaveBeenCalledTimes(1);
    expect(h.submit).toHaveBeenCalledWith('abc-123', 'st-1', h.redirectUri);

    await waitFor(
      () => expect(screen.getByText(/settings page/)).toBeInTheDocument(),
      { timeout: 3000 },
    );
    expect(screen.getByText(/settings page/)).toHaveTextContent('settings page?tab=model');
  });

  it('accepts the authCode spelling of the code parameter', async () => {
    h.submit.mockResolvedValue({ success: true, provider: 'bigmodel', email: null });
    renderCallback('/oauth/zhipu?authCode=xyz-789&state=st-2');

    expect(await screen.findByText('Signed in. Taking you to Settings...')).toBeInTheDocument();
    expect(h.submit).toHaveBeenCalledWith('xyz-789', 'st-2', h.redirectUri);
  });

  it('does not call the backend when the redirect is missing its parameters', async () => {
    renderCallback('/oauth/zhipu?code=only-code');

    expect(await screen.findByRole('alert')).toHaveTextContent('The sign-in did not complete.');
    expect(h.submit).not.toHaveBeenCalled();
  });

  it('keeps the provider failure detail off screen and offers the way back', async () => {
    h.submit.mockRejectedValue(new Error('backend exploded with secret detail'));
    renderCallback('/oauth/zhipu?code=abc-123&state=st-3');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      'The sign-in did not complete. Go back to Settings and try again, or start the Zhipu sign-in again.',
    );
    expect(alert).not.toHaveTextContent('backend exploded');
    expect(h.submit).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole('button', { name: 'Go to Settings' }));
    expect(await screen.findByText(/settings page/)).toBeInTheDocument();
  });
});
