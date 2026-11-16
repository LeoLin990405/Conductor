/**
 * Core API utilities — permanent façade over ./api/.
 * All backend endpoints the chat surfaces consume.
 *
 * Consumers (and the 38 whole-module vi.mock factories) import THIS module;
 * the domain leaves under api/ are implementation. streamFetch/postSSEStream/
 * getAuthHeaders/baseURL stay package-internal (transport.ts) on purpose.
 *
 * api/package.json declares the leaves side-effect-free, so a name imported
 * through here brings in only the leaf that defines it. Keep module-scope work
 * out of the leaves unless it only matters once their exports are used.
 */
export {
  apiErrorDetailMessage,
  apiErrorObject,
  apiErrorReason,
  formatApiErrorDetail,
  apiErrorStatus,
} from './api/errors';
export { parseRunIdFromContentLocation, parseThreadIdFromContentLocation } from './api/transport';
export * from './api/workspaces';
export * from './api/metadata';
export * from './api/userVault';
export * from './api/mcp';
export * from './api/mcpBuiltins';
export * from './api/mcpOauth';
export * from './api/skills';
export * from './api/subagents';
export * from './api/plugins';
export * from './api/marketplace';
