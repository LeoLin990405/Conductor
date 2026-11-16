/**
 * A plan page whose plan also sells a subscription login offers it as a second
 * choice under the API-key form. The mapping comes from the backend capability
 * catalog (`api_key_provider` → descriptor), not a hard-coded table, so it
 * covers MiMo as well; the API-key path stays primary and unchanged.
 */
import { describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes, useLocation } from 'react-router';
import { renderWithProviders } from '@/test/utils';

const h = vi.hoisted(() => ({
  providers: [] as import('@/hooks/useOAuthProviders').OAuthProviderDescriptor[],
}));

vi.mock('@/hooks/useApiKeys', () => ({
  useUpdateApiKeys: () => ({ mutateAsync: vi.fn() }),
}));
vi.mock('@/hooks/useOAuthProviders', () => ({
  useOAuthProviders: () => ({ providers: h.providers, isLoading: false }),
}));

import { ApiKeyConnect } from '../connectStep/ApiKeyConnect';
import type { LocationState } from '../connectStep/shared';

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
  api_key_provider: 'moonshot-coding',
});
const zhipuCn = descriptor({
  id: 'z-ai-cn-oauth',
  display_name: '智谱订阅登录',
  display_name_en: 'Zhipu subscription',
  brand: 'z-ai-cn',
  kind: 'redirect',
  api_key_provider: 'z-ai-cn-coding',
});
const zai = descriptor({
  id: 'z-ai-oauth',
  display_name: 'Z.ai 订阅登录',
  display_name_en: 'Z.ai subscription',
  brand: 'z-ai',
  kind: 'redirect',
  region: 'intl',
  api_key_provider: 'z-ai-coding',
});
const mimo = descriptor({
  id: 'mimo-oauth',
  display_name: 'MiMo 订阅登录',
  display_name_en: 'MiMo subscription',
  brand: 'mimo',
  kind: 'paste',
  api_key_provider: 'mimo',
});
const codex = descriptor({
  id: 'codex-oauth',
  display_name: 'ChatGPT Codex',
  display_name_en: 'ChatGPT Codex',
  brand: 'openai',
  region: 'intl',
  api_key_provider: 'openai',
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
  order: 30,
});

h.providers = [kimi, zhipuCn, zai, mimo, codex, dashscope];

function ConnectProbe({ state }: { state: LocationState }) {
  const location = useLocation();
  const next = location.state as {
    method?: string;
    provider?: string;
    displayName?: string;
  } | null;
  if (next?.method === 'oauth') {
    return <p>{`oauth connect: ${next.provider}/${next.displayName}`}</p>;
  }
  return <ApiKeyConnect state={state} />;
}

function renderPlan(state: LocationState) {
  return renderWithProviders(
    <Routes>
      <Route path="/setup/connect" element={<ConnectProbe state={state} />} />
    </Routes>,
    { route: '/setup/connect' },
  );
}

describe('ApiKeyConnect plan account-login choice', () => {
  it.each([
    ['moonshot-coding', 'Moonshot (Kimi)', 'Kimi subscription', 'kimi-oauth'],
    ['z-ai-coding', 'Z.ai', 'Z.ai subscription', 'z-ai-oauth'],
    ['z-ai-cn-coding', 'Zhipu', 'Zhipu subscription', 'z-ai-cn-oauth'],
    ['mimo', 'MiMo', 'MiMo subscription', 'mimo-oauth'],
  ])(
    'offers the %s account login beside its API key',
    async (provider, displayName, accountName, oauthProvider) => {
      const user = userEvent.setup();
      renderPlan({ method: 'coding_plan', provider, displayName });

      // The API-key path is still the primary action.
      expect(screen.getByRole('button', { name: 'Continue' })).toBeInTheDocument();

      await user.click(
        screen.getByRole('button', {
          name: `Or sign in with your ${accountName} account`,
        }),
      );
      expect(
        await screen.findByText(
          new RegExp(`oauth connect: ${oauthProvider}/${accountName}`),
        ),
      ).toBeInTheDocument();
    },
  );

  it('follows a region switch to that variant’s account login', async () => {
    const user = userEvent.setup();
    renderPlan({
      method: 'coding_plan',
      provider: 'z-ai-coding',
      displayName: 'Zhipu',
      regionVariants: [
        {
          provider: 'z-ai-cn-coding',
          display_name: '智谱（中国）',
          region: 'cn',
          base_url: 'https://open.bigmodel.cn/api/paas/v4',
          sdk: 'openai',
        },
      ] as LocationState['regionVariants'],
    });

    await user.click(screen.getByRole('button', { name: 'China' }));
    await user.click(
      screen.getByRole('button', { name: 'Or sign in with your Zhipu subscription account' }),
    );

    expect(
      await screen.findByText(/oauth connect: z-ai-cn-oauth\/Zhipu subscription/),
    ).toBeInTheDocument();
  });

  it('adds no account-login choice for a plan without one', () => {
    renderPlan({ method: 'coding_plan', provider: 'doubao-coding', displayName: 'Doubao (ARK Plan)' });

    expect(screen.queryByRole('button', { name: /Or sign in with/ })).toBeNull();
    expect(screen.getByRole('button', { name: 'Continue' })).toBeInTheDocument();
  });

  it('does not offer an API-key-only provider as a login', () => {
    renderPlan({ method: 'api_key', provider: 'dashscope', displayName: 'Qwen (DashScope)' });

    expect(screen.queryByRole('button', { name: /Or sign in with/ })).toBeNull();
    expect(screen.getByRole('button', { name: 'Continue' })).toBeInTheDocument();
  });
});

describe('plan-aware API-key editor', () => {
  it('shows the Agent Plan context and does not offer a region switch absent variants', () => {
    renderPlan({
      method: 'coding_plan',
      provider: 'minimax-coding',
      displayName: 'MiniMax',
      brandKey: 'minimax',
      sdk: 'anthropic',
      defaultBaseUrl: 'https://api.minimax.cn/anthropic',
      defaultRegion: 'cn',
      planKind: 'agent',
      planName: 'M Plan',
      accessType: 'coding_plan',
    });

    const context = screen.getByTestId('connect-plan-context');
    expect(context).toHaveTextContent('M Plan');
    expect(context).toHaveTextContent('Agent');
    expect(screen.getByText(/M Plan subscription key/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'International' })).toBeNull();
  });
});
