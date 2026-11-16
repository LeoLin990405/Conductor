/**
 * The provider step groups by where a model runs and shows every way one
 * family can be configured: API key, coding plan, subscription, and the
 * domestic/overseas variants of each, side by side under the same card.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes, useLocation } from 'react-router';
import { renderWithProviders } from '@/test/utils';

const h = vi.hoisted(() => ({
  catalog: [] as unknown[],
  configured: [] as string[],
  preferences: null as Record<string, unknown> | null,
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ preferences: h.preferences }),
}));
vi.mock('@/hooks/useModels', () => ({
  useModels: () => ({ models: { provider_catalog: h.catalog }, isLoading: false }),
}));
vi.mock('@/hooks/useConfiguredProviders', () => ({
  useConfiguredProviders: () => ({ configuredSet: new Set(h.configured) }),
}));
// The invitation entry is present on the first page but its redemption is
// covered separately; this suite only needs the signed-in, no-access state.
vi.mock('@/hooks/useUser', () => ({
  useUser: () => ({ user: { access_tier: -1 }, isLoading: false }),
}));

import ProviderStep from '../ProviderStep';

function entry(
  provider: string,
  display_name: string,
  access_type: string,
  region: string,
  extra: Record<string, unknown> = {},
) {
  return {
    provider,
    display_name,
    access_type,
    brand_key: extra.brand_key ?? provider,
    region,
    byok_eligible: true,
    ...extra,
  };
}

function catalog() {
  return [
    entry('deepseek', 'DeepSeek', 'api_key', 'cn'),
    entry('openai', 'OpenAI', 'api_key', 'intl'),
    entry('z-ai', 'Zhipu AI', 'api_key', 'intl', {
      region_variants: [
        {
          provider: 'z-ai-cn',
          display_name: 'Zhipu AI (CN)',
          region: 'cn',
          sdk: 'glm',
          base_url: 'https://open.bigmodel.cn/api/paas/v4',
          use_response_api: false,
        },
      ],
    }),
    entry('z-ai-coding', 'Zhipu AI Coding Plan', 'coding_plan', 'intl', { brand_key: 'z-ai' }),
    entry('z-ai-cn-coding', 'Zhipu AI Coding Plan (CN)', 'coding_plan', 'cn', {
      brand_key: 'z-ai-cn',
    }),
    entry('moonshot', 'Moonshot', 'api_key', 'intl'),
    entry('moonshot-coding', 'Kimi Coding Plan', 'coding_plan', 'intl', {
      brand_key: 'moonshot',
    }),
    entry('kimi-oauth', 'Kimi Subscription', 'oauth', 'cn', { brand_key: 'moonshot' }),
    entry('ollama', 'Ollama', 'local', 'local'),
  ];
}

function ConnectProbe() {
  const location = useLocation();
  return <pre>{JSON.stringify(location.state)}</pre>;
}

function renderProvider() {
  return renderWithProviders(
    <Routes>
      <Route path="/setup/provider" element={<ProviderStep />} />
      <Route path="/setup/connect" element={<ConnectProbe />} />
      <Route path="/setup/models" element={<p>models step</p>} />
    </Routes>,
    { route: '/setup/provider' },
  );
}

afterEach(() => {
  h.catalog = [];
  h.configured = [];
  h.preferences = null;
});

describe('provider grouping', () => {
  it('splits families into China, United States / Overseas and Local', () => {
    h.catalog = catalog();
    renderProvider();

    expect(screen.getByRole('heading', { name: 'China' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'United States / Overseas' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Local' })).toBeInTheDocument();

    expect(screen.getByRole('radio', { name: 'DeepSeek' })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: 'Zhipu AI / Z.ai' })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: 'Moonshot / Kimi' })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: 'Ollama' })).toBeInTheDocument();
  });

  it('lists API, coding plan and subscription under the same model card', () => {
    h.catalog = catalog();
    renderProvider();

    const moonshot = screen.getByRole('radio', { name: 'Moonshot / Kimi' });
    expect(within(moonshot).getByRole('button', { name: /API/ })).toBeInTheDocument();
    expect(within(moonshot).getByRole('button', { name: /Plan/ })).toBeInTheDocument();
    expect(within(moonshot).getByRole('button', { name: /Subscription/ })).toBeInTheDocument();
  });

  it('marks domestic and overseas options for one family', () => {
    h.catalog = catalog();
    renderProvider();

    const zhipu = screen.getByRole('radio', { name: 'Zhipu AI / Z.ai' });
    const labels = within(zhipu)
      .getAllByRole('button')
      .map((b) => b.textContent?.replace(/\s+/g, ' ').trim());
    expect(labels.some((l) => l?.includes('API China'))).toBe(true);
    expect(labels.some((l) => l?.includes('API Overseas'))).toBe(true);
    expect(labels.some((l) => l?.includes('Plan China'))).toBe(true);
    expect(labels.some((l) => l?.includes('Plan Overseas'))).toBe(true);
  });
});

describe('choosing an access option', () => {
  it('passes the chosen coding-plan route and region to the connect step', async () => {
    h.catalog = catalog();
    const user = userEvent.setup();
    renderProvider();

    const zhipu = screen.getByRole('radio', { name: 'Zhipu AI / Z.ai' });
    await user.click(within(zhipu).getByRole('button', { name: /Plan China/ }));
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    const state = await screen.findByText(/"method":"coding_plan"/);
    expect(state).toHaveTextContent('"provider":"z-ai-cn-coding"');
    expect(state).toHaveTextContent('"defaultRegion":"cn"');
    expect(state).toHaveTextContent('"brandKey":"z-ai-cn"');
  });

  it('clicking a card defaults to the first option when none is chosen', async () => {
    h.catalog = catalog();
    const user = userEvent.setup();
    renderProvider();

    await user.click(screen.getByRole('radio', { name: 'DeepSeek' }));
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    const state = await screen.findByText(/"method":"api_key"/);
    expect(state).toHaveTextContent('"provider":"deepseek"');
  });
});


describe('plan and endpoint distinctions', () => {
  it('names Token plans, Agent plans, and endpoints not configured in this build', () => {
    h.catalog = [
      entry('dashscope', 'Qwen / DashScope', 'api_key', 'cn', {
        plan_capabilities: { token_plan: true, agent_plan: false },
        api_regions: ['cn', 'intl'],
      }),
      entry('dashscope-intl', 'Qwen / DashScope (Intl)', 'api_key', 'intl'),
      entry('mimo', 'Xiaomi MiMo', 'coding_plan', 'intl', {
        plan_kind: 'token',
        plan_name: 'Token Plan',
        plan_capabilities: { token_plan: true, agent_plan: false },
        api_regions: ['cn', 'intl'],
      }),
      entry('doubao-coding', 'Doubao Coding', 'coding_plan', 'cn', {
        brand_key: 'volcengine',
        plan_kind: 'agent',
        plan_name: 'Coding Plan',
        plan_capabilities: { token_plan: false, agent_plan: true },
        api_regions: ['cn'],
      }),
      entry('doubao-plan', 'Doubao Agent', 'coding_plan', 'cn', {
        brand_key: 'volcengine',
        plan_kind: 'agent',
        plan_name: 'Agent Plan',
        plan_capabilities: { token_plan: false, agent_plan: true },
        api_regions: ['cn'],
      }),
    ];
    renderProvider();

    const qwen = screen.getByRole('radio', { name: 'Qwen / DashScope' });
    expect(within(qwen).getByText(/Token Plan · not configured/)).toBeInTheDocument();

    const mimo = screen.getByRole('radio', { name: 'Xiaomi MiMo' });
    expect(within(mimo).getByRole('button', { name: 'Token Plan Overseas' })).toBeInTheDocument();
    expect(within(mimo).getByText(/China · not configured/)).toBeInTheDocument();

    const doubao = screen.getByRole('radio', { name: /Doubao/ });
    expect(within(doubao).getByRole('button', { name: /Agent Plan · Coding Plan/ })).toBeInTheDocument();
    expect(within(doubao).getByRole('button', { name: /Agent Plan · Agent Plan/ })).toBeInTheDocument();
  });
});

describe('Antigravity sign-in', () => {
  it('offers the Antigravity OAuth option', () => {
    h.catalog = catalog();
    (h.catalog as unknown[]).push(
      entry('gemini-oauth', 'Gemini (Antigravity 登录)', 'oauth', 'intl', {
        brand_key: 'gemini',
      }),
    );
    renderProvider();

    const gemini = screen.getByRole('radio', { name: 'Google Gemini' });
    expect(within(gemini).getByRole('button', { name: /Subscription/ })).toBeInTheDocument();
  });
});
