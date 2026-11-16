import { describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';

const api = vi.hoisted(() => ({
  ROLE_NAMES: ['router', 'planner', 'reviewer', 'worker'],
  getRoleRegistry: vi.fn(),
  getRoleAudit: vi.fn(),
  updateRole: vi.fn(),
}));

vi.mock('@/api/roleRegistry', () => api);

import { RoleRegistryTab } from '../RoleRegistryTab';

const constraints = {
  read_only: false,
  may_write: true,
  may_execute: true,
  requires_session_resume: false,
  excluded: [],
  max_parallel: null,
};

function view(revision: number, workerDefault: string) {
  const entry = (role: string, names: string[], def: string) => ({
    role,
    candidates: names,
    default: def,
    constraints,
    extra: {},
  });
  return {
    version: 1,
    source: revision ? 'store' : 'fallback',
    revision,
    hard_rules: ['router_routes_never_executes', 'merge_needs_human'],
    roles: {
      router: entry('router', ['deepseek-flash'], 'deepseek-flash'),
      planner: entry('planner', ['claude'], 'claude'),
      reviewer: entry('reviewer', ['codex'], 'codex'),
      worker: entry('worker', ['stepfun', 'kimi'], workerDefault),
    },
  };
}

describe('RoleRegistryTab', () => {
  it('shows the roles and the hard rules', async () => {
    api.getRoleRegistry.mockResolvedValue(view(0, 'stepfun'));
    api.getRoleAudit.mockResolvedValue([]);
    renderWithProviders(<RoleRegistryTab />);
    expect(await screen.findByDisplayValue('stepfun, kimi')).toBeInTheDocument();
    expect(screen.getByText('router_routes_never_executes')).toBeInTheDocument();
  });

  it('saves one role with its reason and the revision it read', async () => {
    api.getRoleRegistry.mockResolvedValue(view(3, 'stepfun'));
    api.getRoleAudit.mockResolvedValue([]);
    api.updateRole.mockResolvedValue(view(4, 'kimi'));
    renderWithProviders(<RoleRegistryTab />);
    const reason = await screen.findByLabelText('worker reason');
    const def = screen.getByLabelText('worker default');
    await userEvent.clear(def);
    await userEvent.type(def, 'kimi');
    await userEvent.type(reason, 'quota low');
    const buttons = screen.getAllByRole('button');
    await userEvent.click(buttons[3]);
    await waitFor(() =>
      expect(api.updateRole).toHaveBeenCalledWith('worker', {
        candidates: ['stepfun', 'kimi'],
        default: 'kimi',
        reason: 'quota low',
        expected_revision: 3,
      }),
    );
  });

  it('refuses to save without a reason', async () => {
    api.getRoleRegistry.mockResolvedValue(view(1, 'stepfun'));
    api.getRoleAudit.mockResolvedValue([]);
    api.updateRole.mockClear();
    renderWithProviders(<RoleRegistryTab />);
    await screen.findByLabelText('worker reason');
    await userEvent.click(screen.getAllByRole('button')[3]);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(api.updateRole).not.toHaveBeenCalled();
  });
});
