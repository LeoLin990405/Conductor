import { selectableNativeAgents } from '../nativeAgents';
import type { NativeRuntimeStatus } from '../nativeAgents';

function runtime(overrides: Partial<NativeRuntimeStatus>): NativeRuntimeStatus {
  return {
    provider: 'codex',
    installed: true,
    binary: '/usr/local/bin/codex',
    live_verified: true,
    capabilities: {
      streaming: true,
      tool_events: true,
      session_resume: true,
      cache_usage: true,
      approvals: true,
      mcp: true,
      memory: true,
      skills: true,
      workspace_files: true,
      cancel: true,
    },
    error: null,
    ...overrides,
  };
}

describe('selectableNativeAgents', () => {
  it('offers only installed, live-verified runtimes', () => {
    const result = selectableNativeAgents([
      runtime({ provider: 'codex' }),
      runtime({ provider: 'deepseek-dsh', live_verified: false }),
      runtime({ provider: 'zcode', installed: false }),
    ]);
    expect(result.map((row) => row.provider)).toEqual(['codex']);
  });

  it('returns an empty list before the inventory loads', () => {
    expect(selectableNativeAgents(undefined)).toEqual([]);
  });
});
