import { canBeginMcpOAuth, isDesktopShell } from '@/lib/desktop';
import { needsOauthConnect } from '@/components/mcp/mcpState';
import type { McpOauthStatus } from '@/api/core';

/** Why a connect cannot start here, or null when the click is live. */
export type ConnectBlock = 'shell-outdated' | 'native-only' | 'unknown' | null;

/** Whether this shell can begin an OAuth connect at all. */
export function connectBlock(): ConnectBlock {
  if (isDesktopShell() && !canBeginMcpOAuth()) return 'shell-outdated';
  return null;
}

/** The grant a row behaves as, which is not always the one it stored. */
export function settledGrant(
  granted: string[] | null | undefined,
  oauthStatus: McpOauthStatus | null | undefined,
): string[] | null {
  if (granted != null) return granted;
  return oauthStatus && !needsOauthConnect(oauthStatus) ? [] : null;
}
