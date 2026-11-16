import { describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import '@testing-library/jest-dom';

import { renderWithProviders } from '@/test/utils';
import { NativeRuntimeInventory } from '../panels/NativeRuntimeInventory';

const caps = {
  streaming: true,
  tool_events: true,
  session_resume: true,
  cache_usage: false,
  approvals: true,
  mcp: true,
  memory: true,
  skills: true,
  workspace_files: true,
  cancel: true,
};

vi.mock('@/hooks/useNativeAgents', () => ({
  useNativeAgents: () => ({
    isLoading: false,
    data: [
      {
        provider: 'grok', installed: true, binary: '/x', live_verified: true,
        capabilities: caps, error: null,
      },
      {
        provider: 'stepfun', installed: true, binary: '/y', live_verified: false,
        capabilities: { ...caps, streaming: false, tool_events: false, approvals: false },
        error: null,
      },
    ],
  }),
}));

describe('NativeRuntimeInventory', () => {
  it('renders backend capabilities and live status, not a hardcoded matrix', () => {
    renderWithProviders(<NativeRuntimeInventory />);
    expect(screen.getByTestId('native-runtime-inventory')).toBeInTheDocument();
    expect(screen.getByText('Grok')).toBeInTheDocument();
    expect(screen.getByText('StepFun')).toBeInTheDocument();
    expect(screen.getAllByText(/Live|已 live/).length).toBeGreaterThan(0);
    expect(screen.getByText(/Installed, not live-verified|已安装/)).toBeInTheDocument();
    // MCP/memory/skills are true for both rows in this fixture.
    expect(screen.getAllByText(/MCP/).length).toBe(2);
  });
});
