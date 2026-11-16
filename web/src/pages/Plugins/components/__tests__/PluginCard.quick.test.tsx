import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { renderWithProviders } from '@/test/utils';
import type { PluginInfo } from '@/api/core';
import type { PluginStatus } from '../pluginsPage/pluginStatus';
import { PluginCard } from '../PluginCard';

// The lifecycle's verbs are a hook over queries and mutations; here only the
// switch it exposes matters, and the two states it is drawn in.
const life = vi.hoisted(() => ({ toggling: false, toggle: vi.fn() }));

vi.mock('../pluginsPage/usePluginLifecycle', () => ({
  usePluginLifecycle: () => ({
    busy: false,
    toggling: life.toggling,
    toggle: life.toggle,
    updateAvailable: false,
    update: vi.fn(),
    exporting: false,
    exportPackage: vi.fn(),
    confirmingUninstall: false,
    requestUninstall: vi.fn(),
    cancelUninstall: vi.fn(),
    uninstall: vi.fn(),
    uninstalling: false,
    dialogs: null,
  }),
}));

// Inline dropdown, as in ScopeControl.test: jsdom does not drive Radix portals.
vi.mock('@/components/ui/dropdown-menu', () => ({
  DropdownMenu: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuTrigger: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  DropdownMenuContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuItem: ({ children }: { children: React.ReactNode }) => <button>{children}</button>,
  DropdownMenuSeparator: () => null,
}));

const plugin: PluginInfo = {
  name: 'acme',
  version: '1.0.0',
  description: 'A curated bundle.',
  author: null,
  homepage: null,
  source_type: 'git',
  source_ref: 'https://example.test/acme.git',
  enabled: true,
  installed_at: null,
  updated_at: null,
  components: [],
};

const CLEAR: PluginStatus = {
  problem: null,
  needsUser: false,
  server: null,
  fix: null,
  missingKeys: [],
  keys: [],
};

beforeEach(() => {
  life.toggling = false;
  life.toggle.mockReset();
});

describe('PluginCard quick switch', () => {
  it('offers the switch on a row whose slot is a state', () => {
    renderWithProviders(<PluginCard plugin={plugin} status={CLEAR} onOpen={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: 'Switch acme off' }));
    expect(life.toggle).toHaveBeenCalledTimes(1);
  });

  it('holds the switch while it is written', () => {
    life.toggling = true;
    renderWithProviders(<PluginCard plugin={plugin} status={CLEAR} onOpen={vi.fn()} />);

    expect(screen.getByRole('button', { name: 'Switch acme off' })).toBeDisabled();
  });

  it('leaves the slot to a key the row needs first', () => {
    const needsKey: PluginStatus = {
      ...CLEAR,
      problem: { kind: 'connect', count: 1 },
      needsUser: true,
      fix: { kind: 'addKey', key: 'ACME_KEY' },
      missingKeys: ['ACME_KEY'],
    };
    renderWithProviders(<PluginCard plugin={plugin} status={needsKey} onOpen={vi.fn()} />);

    expect(screen.getByRole('link', { name: /Add key/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Switch acme off' })).toBeNull();
  });

  it('leaves the slot to the sign-in a connector is waiting on', () => {
    const needsSignIn: PluginStatus = {
      ...CLEAR,
      problem: { kind: 'connect', count: 1 },
      needsUser: true,
      fix: { kind: 'signIn' },
    };
    renderWithProviders(
      <PluginCard plugin={plugin} status={needsSignIn} onOpen={vi.fn()} onConnect={vi.fn()} />,
    );

    expect(screen.getByRole('button', { name: 'Connect' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Switch acme off' })).toBeNull();
  });
});
