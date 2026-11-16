/**
 * Settings → Account connections: the descriptor-driven section that replaced
 * the hard-coded Kimi/Zhipu cards. The catalog hook, the users' API keys and
 * the per-provider status/flow endpoints are mocked, so what is pinned here is
 * the rendering contract — one card per domestic login descriptor, the
 * API-key-only rows, the two-choice API-key fallback, and the MiMo paste flow —
 * while the international cards stay exactly as they were.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes, useLocation } from 'react-router';
import { renderWithProviders } from '@/test/utils';
import i18n from '@/i18n';

const h = vi.hoisted(() => ({
  providers: [] as import('@/hooks/useOAuthProviders').OAuthProviderDescriptor[],
  apiKeys: null as Record<string, unknown> | null,
}));

const api = vi.hoisted(() => ({
  // The four international cards read their status on mount.
  getCodexOAuthStatus: vi.fn(),
  getClaudeOAuthStatus: vi.fn(),
  getGrokOAuthStatus: vi.fn(),
  getGeminiOAuthStatus: vi.fn(),
  initiateCodexDevice: vi.fn(),
  pollCodexDevice: vi.fn(),
  disconnectCodexOAuth: vi.fn(),
  initiateClaudeOAuth: vi.fn(),
  submitClaudeCallback: vi.fn(),
  disconnectClaudeOAuth: vi.fn(),
  initiateGrokDevice: vi.fn(),
  pollGrokDevice: vi.fn(),
  disconnectGrokOAuth: vi.fn(),
  initiateGeminiOAuth: vi.fn(),
  submitGeminiCallback: vi.fn(),
  disconnectGeminiOAuth: vi.fn(),
  // The unified section's per-provider status and MiMo flow.
  getKimiOAuthStatus: vi.fn(),
  getZhipuOAuthStatus: vi.fn(),
  getMimoOAuthStatus: vi.fn(),
  getDeepSeekOAuthStatus: vi.fn(),
  getDeepSeekOAuthModels: vi.fn(),
  disconnectDeepSeekOAuth: vi.fn(),
  initiateDeepSeekOAuth: vi.fn(),
  initiateKimiDevice: vi.fn(),
  pollKimiDevice: vi.fn(),
  disconnectKimiOAuth: vi.fn(),
  initiateZhipuOAuth: vi.fn(),
  pollZhipuOAuth: vi.fn(),
  disconnectZhipuOAuth: vi.fn(),
  initiateMimoOAuth: vi.fn(),
  submitMimoCallback: vi.fn(),
  disconnectMimoOAuth: vi.fn(),
}));

vi.mock('@/lib/dashboardApi/api', () => api);
vi.mock('@/hooks/useOAuthProviders', () => ({
  useOAuthProviders: () => ({ providers: h.providers, isLoading: false }),
}));
vi.mock('@/hooks/useApiKeys', () => ({
  useApiKeys: () => ({ apiKeys: h.apiKeys, isLoading: false }),
}));

import { ConnectedAccounts } from '../ConnectedAccounts';

type Descriptor = import('@/hooks/useOAuthProviders').OAuthProviderDescriptor;

function descriptor(patch: Partial<Descriptor> & Pick<Descriptor, 'id' | 'brand'>): Descriptor {
  return {
    display_name: patch.id,
    display_name_en: patch.id,
    region: 'cn',
    kind: 'device',
    login_supported: true,
    unavailable_reason: null,
    unavailable_reason_en: null,
    api_key_provider: null,
    website_url: null,
    models: [],
    order: 20,
    ...patch,
  };
}

const kimi = descriptor({
  id: 'kimi-oauth',
  display_name: 'Kimi 订阅登录',
  display_name_en: 'Kimi subscription',
  brand: 'moonshot',
  kind: 'device',
  api_key_provider: 'moonshot-coding',
  website_url: 'https://www.kimi.com/code/',
  order: 20,
});
const zhipuCn = descriptor({
  id: 'z-ai-cn-oauth',
  display_name: '智谱订阅登录',
  display_name_en: 'Zhipu subscription',
  brand: 'z-ai-cn',
  kind: 'redirect',
  api_key_provider: 'z-ai-cn-coding',
  order: 21,
});
const zai = descriptor({
  id: 'z-ai-oauth',
  display_name: 'Z.ai 订阅登录',
  display_name_en: 'Z.ai subscription',
  brand: 'z-ai',
  kind: 'redirect',
  region: 'intl',
  api_key_provider: 'z-ai-coding',
  order: 22,
});
const mimo = descriptor({
  id: 'mimo-oauth',
  display_name: 'MiMo 订阅登录',
  display_name_en: 'MiMo subscription',
  brand: 'mimo',
  kind: 'paste',
  api_key_provider: 'mimo',
  order: 23,
});
const codex = descriptor({
  id: 'codex-oauth',
  display_name: 'ChatGPT Codex',
  display_name_en: 'ChatGPT Codex (catalog)',
  brand: 'openai',
  region: 'intl',
  order: 10,
});
const dashscope = descriptor({
  id: 'dashscope',
  display_name: '通义千问 / 百炼',
  display_name_en: 'Qwen (DashScope)',
  brand: 'dashscope',
  kind: 'api_key_only',
  login_supported: false,
  api_key_provider: 'dashscope',
  website_url: 'https://bailian.example/',
  unavailable_reason:
    'Qwen Code OAuth 免费额度已于 2026-04-15 停止（qwen-code 0.25.1 内提示），仅提供 API Key / Token Plan Key 接入。',
  unavailable_reason_en:
    "Qwen Code's OAuth free quota ended on 2026-04-15 (shown in qwen-code 0.25.1); only API key / Token Plan key access remains.",
  order: 30,
});
const minimax = descriptor({
  id: 'minimax',
  display_name: 'MiniMax',
  display_name_en: 'MiniMax',
  brand: 'minimax',
  kind: 'api_key_only',
  login_supported: false,
  api_key_provider: 'minimax',
  website_url: 'https://platform.minimaxi.com/',
  unavailable_reason: '官方 CLI 登录基于 API Key，不提供账号 OAuth。',
  unavailable_reason_en: 'The official CLI signs in with an API key; no account OAuth is offered.',
  order: 31,
});

const disconnected = { connected: false, account_id: null, email: null, plan_type: null };
const zhipuDisconnected = { connected: false, provider: null, email: null };

function cardFor(title: string): HTMLElement {
  const card = screen.getByText(title).closest('.tn-settings-card');
  if (!(card instanceof HTMLElement)) throw new Error(`no card around "${title}"`);
  return card;
}

/** Settings with the setup connect step it navigates to, so a navigation can
 *  report the state it carried. */
function renderSettings() {
  return renderWithProviders(
    <Routes>
      <Route path="/settings" element={<ConnectedAccounts />} />
      <Route path="/setup/connect" element={<ConnectStepProbe />} />
    </Routes>,
    { route: '/settings' },
  );
}

function ConnectStepProbe() {
  const location = useLocation();
  const state = (location.state ?? {}) as Record<string, unknown>;
  return (
    <p>
      {`connect step: ${String(state.method)} ${String(state.provider)} ${String(
        state.displayName,
      )} ${String(state.brandKey)}`}
    </p>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  h.providers = [kimi, zhipuCn, zai, mimo, codex, dashscope, minimax];
  h.apiKeys = { providers: [] };
  for (const status of [
    api.getCodexOAuthStatus,
    api.getClaudeOAuthStatus,
    api.getGrokOAuthStatus,
    api.getGeminiOAuthStatus,
  ]) {
    status.mockResolvedValue({ ...disconnected });
  }
  api.getKimiOAuthStatus.mockResolvedValue({ ...disconnected });
  api.getZhipuOAuthStatus.mockResolvedValue({ ...zhipuDisconnected });
  api.getMimoOAuthStatus.mockResolvedValue({ ...disconnected });
  vi.spyOn(window, 'open').mockReturnValue(null);
});

afterEach(async () => {
  vi.restoreAllMocks();
  if (i18n.language !== 'en-US') await act(() => i18n.changeLanguage('en-US'));
});

describe('unified account connections section', () => {
  it('offers Antigravity Google sign-in and an API-key fallback', async () => {
    renderSettings();

    const notice = await screen.findByTestId('gemini-antigravity-notice');
    expect(notice).toHaveTextContent(/Antigravity/i);
    expect(within(notice).getByRole('link', { name: /Gemini API key/i })).toHaveAttribute(
      'href',
      'https://aistudio.google.com/apikey',
    );
  });

  it('renders one card per domestic login descriptor and keeps the international cards', async () => {
    renderSettings();

    expect(
      await screen.findByRole('heading', { name: 'Account connections' }),
    ).toBeInTheDocument();
    for (const title of [
      'Kimi subscription',
      'Zhipu subscription',
      'Z.ai subscription',
      'MiMo subscription',
    ]) {
      expect(screen.getAllByText(title)).toHaveLength(1);
    }

    // The descriptors that belong to the international cards are not duplicated
    // here, and the old hard-coded domestic cards are gone.
    expect(screen.queryByText('ChatGPT Codex (catalog)')).toBeNull();
    expect(screen.getByText('ChatGPT Codex')).toBeInTheDocument();
    expect(screen.queryByText('Kimi subscription sign-in')).toBeNull();
    expect(screen.queryByText('Zhipu Coding Plan sign-in')).toBeNull();
  });

  it('shows the connected account the status reports and disconnects it', async () => {
    const user = userEvent.setup();
    h.providers = [kimi];
    api.getKimiOAuthStatus
      .mockResolvedValueOnce({
        connected: true,
        account_id: 'acct-1',
        email: 'kimi@example.com',
        plan_type: 'pro',
      })
      .mockResolvedValue({ ...disconnected });
    api.disconnectKimiOAuth.mockResolvedValue({ success: true });
    renderSettings();

    const card = cardFor('Kimi subscription');
    expect(await within(card).findByText('kimi@example.com')).toBeInTheDocument();
    expect(within(card).getByText('pro')).toBeInTheDocument();

    await user.click(within(card).getByRole('button', { name: 'Disconnect' }));
    await waitFor(() => expect(api.disconnectKimiOAuth).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(within(card).getByRole('button', { name: 'Connect' })).toBeInTheDocument(),
    );
  });

  it.each([
    ['Kimi subscription', 'moonshot-coding', 'moonshot'],
    ['MiMo subscription', 'mimo', 'mimo'],
  ])(
    'offers the API-key fallback on the %s card',
    async (title, provider, brand) => {
      const user = userEvent.setup();
      renderSettings();

      const card = cardFor(title);
      await user.click(within(card).getByRole('button', { name: 'Or enter an API key' }));

      expect(
        await screen.findByText(`connect step: api_key ${provider} ${title} ${brand}`),
      ).toBeInTheDocument();
    },
  );

  it('runs the MiMo paste flow and shows the connection it mints', async () => {
    const user = userEvent.setup();
    h.providers = [mimo];
    api.initiateMimoOAuth.mockResolvedValue({ authorize_url: 'https://mimo.example/authorize' });
    api.submitMimoCallback.mockResolvedValue({
      success: true,
      uid: 'mimo-user-1',
      base_url_host: 'api.mimo.example',
    });
    api.getMimoOAuthStatus
      .mockResolvedValueOnce({ ...disconnected })
      .mockResolvedValue({
        connected: true,
        account_id: null,
        email: null,
        plan_type: 'coding_plan',
      });
    renderSettings();

    const card = cardFor('MiMo subscription');
    await user.click(within(card).getByRole('button', { name: 'Connect' }));

    expect(window.open).toHaveBeenCalledWith(
      'https://mimo.example/authorize',
      '_blank',
      'noopener',
    );
    const input = await within(card).findByPlaceholderText('Paste the authorization code');
    expect(
      within(card).getByRole('link', { name: 'Open the sign-in page again' }),
    ).toHaveAttribute('href', 'https://mimo.example/authorize');

    await user.type(input, 'u-encrypted-code');
    await user.click(within(card).getByRole('button', { name: 'Submit' }));

    expect(api.submitMimoCallback).toHaveBeenCalledWith('u-encrypted-code');
    await waitFor(() => expect(within(card).getByText('coding_plan')).toBeInTheDocument());
    expect(within(card).getByRole('button', { name: 'Disconnect' })).toBeInTheDocument();
  });

  it('reports a rejected MiMo code and keeps the pasted attempt open', async () => {
    const user = userEvent.setup();
    h.providers = [mimo];
    api.initiateMimoOAuth.mockResolvedValue({ authorize_url: 'https://mimo.example/authorize' });
    api.submitMimoCallback.mockRejectedValue(new Error('invalid code'));
    renderSettings();

    const card = cardFor('MiMo subscription');
    await user.click(within(card).getByRole('button', { name: 'Connect' }));
    const input = await within(card).findByPlaceholderText('Paste the authorization code');
    await user.type(input, 'bad-code');
    await user.click(within(card).getByRole('button', { name: 'Submit' }));

    expect(await within(card).findByRole('alert')).toHaveTextContent(
      'That authorization code was not accepted. Please try again.',
    );
    expect(within(card).getByPlaceholderText('Paste the authorization code')).toBeInTheDocument();
  });

  it('gives the device-code flow a waiting status and a cancel', async () => {
    const user = userEvent.setup();
    h.providers = [kimi];
    api.initiateKimiDevice.mockResolvedValue({
      user_code: 'KIMI-CODE',
      verification_url: 'https://kimi.example/device',
      interval: 0.01,
    });
    api.pollKimiDevice.mockResolvedValue({ pending: true });
    renderSettings();

    const card = cardFor('Kimi subscription');
    await user.click(within(card).getByRole('button', { name: 'Connect' }));

    expect(await within(card).findByText('KIMI-CODE')).toBeInTheDocument();
    expect(within(card).getByRole('status')).toHaveTextContent('Waiting for approval...');

    await user.click(within(card).getByRole('button', { name: 'Cancel' }));
    expect(within(card).queryByText('KIMI-CODE')).toBeNull();
  });

  it('shows the connected console only on the card that signed in', async () => {
    h.providers = [zhipuCn, zai];
    api.getZhipuOAuthStatus.mockResolvedValue({
      connected: true,
      provider: 'bigmodel',
      email: 'glm@example.com',
    });
    renderSettings();

    const zhipuCard = cardFor('Zhipu subscription');
    expect(
      await within(zhipuCard).findByText(/BigModel \(China\) · glm@example\.com/),
    ).toBeInTheDocument();
    expect(within(zhipuCard).getByRole('button', { name: 'Disconnect' })).toBeInTheDocument();

    const zaiCard = cardFor('Z.ai subscription');
    expect(within(zaiCard).queryByText('glm@example.com')).toBeNull();
    expect(within(zaiCard).getByRole('button', { name: 'Connect' })).toBeInTheDocument();
  });

  it('shows API-key-only providers with their key state, explanation and links', async () => {
    const user = userEvent.setup();
    h.providers = [dashscope, minimax];
    h.apiKeys = { providers: [{ provider: 'dashscope', has_key: true }] };
    renderSettings();

    const qwenRow = cardFor('Qwen (DashScope)');
    expect(within(qwenRow).getByText('API key set')).toBeInTheDocument();
    expect(
      within(qwenRow).getByText(/OAuth free quota ended on 2026-04-15/),
    ).toBeInTheDocument();
    const website = within(qwenRow).getByRole('link', {
      name: 'Get an API key on the website',
    });
    expect(website).toHaveAttribute('href', 'https://bailian.example/');
    expect(website).toHaveAttribute('target', '_blank');
    expect(website.getAttribute('rel')).toContain('noopener');

    const minimaxRow = cardFor('MiniMax');
    expect(within(minimaxRow).getByText('No API key')).toBeInTheDocument();

    await user.click(within(qwenRow).getByRole('button', { name: 'Enter API key' }));
    expect(
      await screen.findByText('connect step: api_key dashscope Qwen (DashScope) dashscope'),
    ).toBeInTheDocument();
  });

  it('hides the section when the catalog has nothing to offer', async () => {
    h.providers = [];
    renderSettings();

    // The international cards are still there; the catalog-driven section is not.
    expect(await screen.findByText('ChatGPT Codex')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Account connections' })).toBeNull();
  });

  it('renders the section in the active language', async () => {
    await act(() => i18n.changeLanguage('zh-CN'));
    h.providers = [kimi, dashscope];
    renderSettings();

    expect(
      await screen.findByRole('heading', { name: '模型账号连接' }),
    ).toBeInTheDocument();
    expect(screen.getByText('Kimi 订阅登录')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '或填写 API 密钥' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /登录官网获取 Key/ })).toBeInTheDocument();
    expect(screen.getByText('未配置 API 密钥')).toBeInTheDocument();
    expect(screen.getByText(/Qwen Code OAuth 免费额度已于 2026-04-15 停止/)).toBeInTheDocument();
  });
});
