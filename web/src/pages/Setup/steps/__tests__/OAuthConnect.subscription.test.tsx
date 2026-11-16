/**
 * The Setup wizard must be able to sign in with every subscription provider in
 * the manifest, not only Codex/Claude. Each flow is exercised here against a
 * mocked API, through to the step it moves on to.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router';
import { renderWithProviders } from '@/test/utils';

const api = vi.hoisted(() => ({
  initiateCodexDevice: vi.fn(),
  pollCodexDevice: vi.fn(),
  disconnectCodexOAuth: vi.fn(),
  initiateGrokDevice: vi.fn(),
  pollGrokDevice: vi.fn(),
  disconnectGrokOAuth: vi.fn(),
  initiateKimiDevice: vi.fn(),
  pollKimiDevice: vi.fn(),
  disconnectKimiOAuth: vi.fn(),
  initiateClaudeOAuth: vi.fn(),
  submitClaudeCallback: vi.fn(),
  disconnectClaudeOAuth: vi.fn(),
  initiateGeminiOAuth: vi.fn(),
  submitGeminiCallback: vi.fn(),
  disconnectGeminiOAuth: vi.fn(),
  initiateZhipuOAuth: vi.fn(),
  pollZhipuOAuth: vi.fn(),
  disconnectZhipuOAuth: vi.fn(),
  initiateMimoOAuth: vi.fn(),
  submitMimoCallback: vi.fn(),
  disconnectMimoOAuth: vi.fn(),
}));

vi.mock('@/lib/dashboardApi/api', () => api);

import { OAuthConnect } from '../connectStep/OAuthConnect';
import type { LocationState } from '../connectStep/shared';

function renderConnect(state: LocationState) {
  return renderWithProviders(
    <Routes>
      <Route path="/setup/connect" element={<OAuthConnect state={state} />} />
      <Route path="/setup/models" element={<p>models step</p>} />
    </Routes>,
    { route: '/setup/connect' },
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(window, 'open').mockReturnValue(null);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('OAuthConnect subscription coverage', () => {
  it('runs the Gemini authorize-and-paste flow', async () => {
    api.initiateGeminiOAuth.mockResolvedValue({ authorize_url: 'https://google.example/authorize' });
    api.submitGeminiCallback.mockResolvedValue({ success: true, email: 'g@example.com' });
    const user = userEvent.setup();
    renderConnect({ method: 'oauth', provider: 'gemini-oauth', displayName: 'Gemini' });

    expect(screen.getByText('Connect your Gemini subscription through the browser.')).toBeInTheDocument();
    const start = screen.getByRole('button', { name: /Open the Gemini sign-in page/ });
    expect(start).toBeDisabled();
    await user.click(screen.getByRole('checkbox'));
    expect(start).toBeEnabled();
    await user.click(start);

    const input = await screen.findByRole('textbox');
    expect(api.initiateGeminiOAuth).toHaveBeenCalledTimes(1);
    expect(window.open).toHaveBeenCalledWith('https://google.example/authorize', '_blank', 'noopener');
    await user.type(input, '4/0AbCdEf');
    await user.click(screen.getByRole('button', { name: 'Submit' }));

    expect(await screen.findByText('models step')).toBeInTheDocument();
    expect(api.submitGeminiCallback).toHaveBeenCalledWith('4/0AbCdEf');
  });

  it.each([
    {
      provider: 'grok-oauth',
      displayName: 'Grok',
      initiate: () => api.initiateGrokDevice,
      poll: () => api.pollGrokDevice,
      start: /Open the Grok authorization page/,
    },
    {
      provider: 'kimi-oauth',
      displayName: 'Kimi',
      initiate: () => api.initiateKimiDevice,
      poll: () => api.pollKimiDevice,
      start: /Open the Kimi authorization page/,
    },
  ])('runs the $displayName device-code flow', async ({ provider, displayName, initiate, poll, start }) => {
    initiate().mockResolvedValue({
      user_code: 'ABCD-EFGH',
      verification_url: 'https://provider.example/device',
      interval: 0.01,
    });
    poll().mockResolvedValueOnce({ pending: true }).mockResolvedValue({
      success: true,
      email: 'user@example.com',
      account_id: 'acct-1',
    });
    const user = userEvent.setup();
    renderConnect({ method: 'oauth', provider, displayName });

    await user.click(screen.getByRole('checkbox'));
    await user.click(screen.getByRole('button', { name: start }));

    expect(await screen.findByText('ABCD-EFGH')).toBeInTheDocument();
    expect(window.open).toHaveBeenCalledWith('https://provider.example/device', '_blank', 'noopener');
    expect(await screen.findByText('models step')).toBeInTheDocument();
    expect(poll()).toHaveBeenCalledTimes(2);
  });

  it('runs the BigModel browser flow with a loopback redirect', async () => {
    api.initiateZhipuOAuth.mockResolvedValue({
      authorize_url: 'https://bigmodel.example/auth',
      state: 'state-1',
      interval: 0.01,
    });
    api.pollZhipuOAuth.mockResolvedValueOnce({ pending: true }).mockResolvedValue({
      success: true,
      provider: 'bigmodel',
      email: 'z@example.com',
    });
    const user = userEvent.setup();
    renderConnect({ method: 'oauth', provider: 'z-ai-cn-oauth', displayName: '智谱' });

    await user.click(screen.getByRole('checkbox'));
    await user.click(screen.getByRole('button', { name: /Open the 智谱 sign-in page/ }));

    expect(await screen.findByText('models step')).toBeInTheDocument();
    expect(api.initiateZhipuOAuth).toHaveBeenCalledWith(
      'bigmodel',
      expect.stringMatching(/\/oauth\/zhipu$/),
    );
    expect(api.pollZhipuOAuth.mock.calls).toEqual([['state-1'], ['state-1']]);
  });

  it('runs the Z.ai browser flow without a redirect_uri', async () => {
    api.initiateZhipuOAuth.mockResolvedValue({
      authorize_url: 'https://z.ai/auth',
      state: 'state-2',
      interval: 0.01,
    });
    api.pollZhipuOAuth.mockResolvedValue({ success: true, provider: 'zai', email: 'z@example.com' });
    const user = userEvent.setup();
    renderConnect({ method: 'oauth', provider: 'z-ai-oauth', displayName: 'Z.ai' });

    await user.click(screen.getByRole('checkbox'));
    await user.click(screen.getByRole('button', { name: /Open the Z.ai sign-in page/ }));

    expect(await screen.findByText('models step')).toBeInTheDocument();
    expect(api.initiateZhipuOAuth).toHaveBeenCalledWith('zai', undefined);
  });

  it('runs the MiMo authorize-and-paste flow', async () => {
    api.initiateMimoOAuth.mockResolvedValue({ authorize_url: 'https://mimo.example/authorize' });
    api.submitMimoCallback.mockResolvedValue({
      success: true,
      uid: 'mimo-user-1',
      base_url_host: 'api.mimo.example',
    });
    const user = userEvent.setup();
    renderConnect({ method: 'oauth', provider: 'mimo-oauth', displayName: 'MiMo' });

    await user.click(screen.getByRole('checkbox'));
    await user.click(screen.getByRole('button', { name: /Open the MiMo sign-in page/ }));

    const input = await screen.findByRole('textbox');
    expect(api.initiateMimoOAuth).toHaveBeenCalledTimes(1);
    expect(window.open).toHaveBeenCalledWith(
      'https://mimo.example/authorize',
      '_blank',
      'noopener',
    );
    await user.type(input, 'u-encrypted-code');
    await user.click(screen.getByRole('button', { name: 'Submit' }));

    expect(await screen.findByText('models step')).toBeInTheDocument();
    expect(api.submitMimoCallback).toHaveBeenCalledWith('u-encrypted-code');
  });

  it('explains a provider this build has no flow for instead of guessing one', () => {
    renderConnect({ method: 'oauth', provider: 'mystery-oauth', displayName: 'Mystery' });

    expect(screen.getByRole('alert')).toHaveTextContent(
      'This provider does not support subscription sign-in yet.',
    );
    expect(screen.queryByRole('button', { name: /Open the Mystery/ })).toBeNull();
  });
});
