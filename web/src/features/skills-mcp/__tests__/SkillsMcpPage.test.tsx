import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';

import SkillsMcpPage from '../SkillsMcpPage';
import type { SkillsMcpPageProps } from '../SkillsMcpPage';
import type { SkillsMcpStatus } from '../model';

const FIXTURE: SkillsMcpStatus = {
  skills: {
    agents: [
      {
        name: 'codex',
        cells: [
          { name: 'fleet-rules', state: 'installed' },
          { name: 'agent-browser', state: 'missing' },
          { name: 'review', state: 'drifted' },
        ],
        env: {
          API_KEY: 'super-secret',
          HOME: '/home/operator',
          nested: { refresh_token: 'super-secret', host: 'local' },
        },
      },
      {
        name: 'claude',
        cells: [
          { name: 'fleet-rules', state: 'conflict' },
          { name: 'agent-browser', state: 'installed' },
        ],
      },
    ],
  },
  mcp: {
    agents: [
      {
        name: 'codex',
        cells: [
          { name: 'fleet-memory', state: 'installed' },
          { name: 'codegraph', state: 'drifted' },
        ],
        env: { password: 'super-secret' },
      },
      {
        name: 'claude',
        cells: [
          { name: 'fleet-memory', state: 'missing' },
          { name: 'codegraph', state: 'conflict' },
        ],
      },
    ],
  },
};

function renderFixture(props: Omit<SkillsMcpPageProps, 'fetcher'> = {}) {
  return render(
    <MemoryRouter>
      <SkillsMcpPage fetcher={async () => FIXTURE} {...props} />
    </MemoryRouter>,
  );
}

function renderLive() {
  return render(
    <MemoryRouter>
      <SkillsMcpPage />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('SkillsMcpPage', () => {
  it('renders skill and MCP matrices with installed, missing, drifted, and conflict glyphs', async () => {
    renderFixture();

    const skills = await screen.findByRole('region', { name: 'Agents × skills' });
    const mcp = screen.getByRole('region', { name: 'Agents × MCP servers' });

    const codexSkills = within(skills).getByRole('row', { name: /codex/ });
    expect(within(codexSkills).getByText('✔ installed')).toBeInTheDocument();
    expect(within(codexSkills).getByText('✖ missing')).toBeInTheDocument();
    expect(within(codexSkills).getByText('◆ drifted')).toBeInTheDocument();

    const claudeSkills = within(skills).getByRole('row', { name: /claude/ });
    expect(within(claudeSkills).getByText('◆ conflict')).toBeInTheDocument();

    const claudeMcp = within(mcp).getByRole('row', { name: /claude/ });
    expect(within(claudeMcp).getByText('✖ missing')).toBeInTheDocument();
    expect(within(claudeMcp).getByText('◆ conflict')).toBeInTheDocument();
    expect(within(mcp).getByRole('columnheader', { name: 'fleet-memory' })).toBeInTheDocument();
  });

  it('never renders a fixture env value whose field name is a secret', async () => {
    renderFixture();
    await screen.findByRole('region', { name: 'Agents × skills' });

    expect(document.body).not.toHaveTextContent('super-secret');
    expect(document.body.innerHTML).not.toContain('super-secret');
    expect(screen.getAllByText('••••').length).toBeGreaterThan(0);
    expect(screen.getByText('/home/operator')).toBeInTheDocument();
    expect(screen.getByText(/"host":"local"/)).toBeInTheDocument();
  });

  it('names Adopt, Apply, and Rollback and shows their diff in a pre', async () => {
    const user = userEvent.setup();
    const onAdopt = vi.fn().mockResolvedValue('adopt-diff');
    const onApply = vi.fn().mockResolvedValue('apply-diff');
    const onRollback = vi.fn().mockResolvedValue('rollback-diff');
    renderFixture({ onAdopt, onApply, onRollback });

    const adopt = await screen.findByRole('button', { name: 'Adopt codex skills' });
    const apply = screen.getByRole('button', { name: 'Apply codex MCP' });
    const rollback = screen.getByRole('button', { name: 'Rollback claude skills' });
    expect(adopt).toHaveAccessibleName('Adopt codex skills');
    expect(apply).toHaveAccessibleName('Apply codex MCP');
    expect(rollback).toHaveAccessibleName('Rollback claude skills');

    await user.click(adopt);
    expect(onAdopt).toHaveBeenCalledWith({ kind: 'skills', agent: 'codex' });
    const skillsPreview = screen.getByRole('group', { name: 'Diff preview codex skills' });
    expect(skillsPreview.tagName).toBe('PRE');
    expect(skillsPreview.textContent).toContain('adopt-diff');
    skillsPreview.focus();
    expect(skillsPreview).toHaveFocus();

    await user.click(apply);
    expect(onApply).toHaveBeenCalledWith({ kind: 'mcp', agent: 'codex' });
    expect(screen.getByRole('group', { name: 'Diff preview codex MCP' }).textContent).toContain('apply-diff');

    await user.click(rollback);
    expect(onRollback).toHaveBeenCalledWith({ kind: 'skills', agent: 'claude' });
    expect(screen.getByRole('group', { name: 'Diff preview claude skills' }).textContent).toContain('rollback-diff');
    expect(skillsPreview.textContent).not.toContain('rollback-diff');
  });

  it('posts adopt, apply, and rollback and scrubs secrets out of the diff', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/v1/skills-mcp/status')) {
        return new Response(JSON.stringify(FIXTURE), { status: 200 });
      }
      if (url.endsWith('/api/v1/skills-mcp/adopt')) {
        return new Response(JSON.stringify({ diff: 'adopt token=super-secret', api_key: 'super-secret' }), {
          status: 200,
        });
      }
      if (url.endsWith('/api/v1/skills-mcp/apply')) {
        return new Response(JSON.stringify({ diff: 'apply-diff' }), { status: 200 });
      }
      if (url.endsWith('/api/v1/skills-mcp/rollback')) {
        return new Response('rollback-diff', { status: 200 });
      }
      return new Response('missing', { status: 404 });
    });
    vi.stubGlobal('fetch', fetchMock);
    renderLive();

    await user.click(await screen.findByRole('button', { name: 'Adopt codex skills' }));
    expect(await screen.findByText(/adopt token=/)).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain('super-secret');
    expect(screen.getByRole('group', { name: 'Diff preview codex skills' }).textContent).toContain('••••');

    const adoptCall = fetchMock.mock.calls.find((call) => String(call[0]).endsWith('/adopt'));
    expect(adoptCall?.[1]).toMatchObject({ method: 'POST' });
    expect(JSON.parse(String(adoptCall?.[1]?.body))).toEqual({ kind: 'skills', agent: 'codex' });

    await user.click(screen.getByRole('button', { name: 'Apply claude MCP' }));
    expect(await screen.findByText('apply-diff')).toBeInTheDocument();
    const applyCall = fetchMock.mock.calls.find((call) => String(call[0]).endsWith('/apply'));
    expect(JSON.parse(String(applyCall?.[1]?.body))).toEqual({ kind: 'mcp', agent: 'claude' });

    await user.click(screen.getByRole('button', { name: 'Rollback codex skills' }));
    expect((await screen.findByRole('group', { name: 'Diff preview codex skills' })).textContent).toContain(
      'rollback-diff',
    );
    const rollbackCall = fetchMock.mock.calls.find((call) => String(call[0]).endsWith('/rollback'));
    expect(rollbackCall?.[1]).toMatchObject({ method: 'POST' });
  });

  it('shows the local CLI as the source of truth when status 404s', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ api_key: 'super-secret' }), { status: 404 })),
    );
    renderLive();

    expect(await screen.findByText(/local CLI is the source of truth/)).toBeInTheDocument();
    expect(screen.getByText('conductor skills status --json')).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain('super-secret');
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('explains a failed status without crashing or claiming the CLI is empty', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('nope', { status: 500 })));
    renderLive();

    expect(await screen.findByRole('alert')).toHaveTextContent('Skills & MCP status failed (500)');
    expect(screen.queryByText(/conductor skills status --json/)).not.toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });
});
