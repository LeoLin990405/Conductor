/**
 * Where a vendor's logo comes from, for every surface that draws one.
 *
 * Two sources, because the vendors differ in kind rather than in handling. An
 * LLM provider's mark is a file we ship: half of them are local runtimes with
 * no website to ask, and the art is better than any favicon. A connector's
 * mark is fetched live from the vendor's own site through the backend, because
 * the set changes without a frontend release and the good art is only
 * discoverable by reading their page.
 *
 * Both answer with the same `BrandArt`, so `BrandMark` never learns which kind
 * it is drawing. What they do not share is a lookup: provider ids and server
 * names are different keyspaces, and one map over both would silently hand a
 * connector named `openai` the model provider's logo.
 */
import { baseURL } from '@/api/core/api/transport';

import iconOpenai from '@/assets/providers/openai.png';
import iconAnthropic from '@/assets/providers/anthropic.png';
import iconGemini from '@/assets/providers/gemini.png';
import iconOpenrouter from '@/assets/providers/openrouter.png';
import iconZai from '@/assets/providers/z-ai.png';
import iconMinimax from '@/assets/providers/minimax.png';
import iconDashscope from '@/assets/providers/dashscope.png';
import iconVolcengine from '@/assets/providers/volcengine.png';
import iconMoonshot from '@/assets/providers/moonshot.png';
import iconDeepseek from '@/assets/providers/deepseek.png';
import iconGroq from '@/assets/providers/groq.png';
import iconCerebras from '@/assets/providers/cerebras.png';
import iconOllama from '@/assets/providers/ollama.png';
import iconLmStudio from '@/assets/providers/lmstudio.png';
import iconVllm from '@/assets/providers/vllm.png';
import iconStepfun from '@/assets/providers/stepfun.png';
import iconMimo from '@/assets/providers/mimo.png';
import iconKimi from '@/assets/providers/kimi.png';
import iconXai from '@/assets/providers/xai.png';
import iconDoubao from '@/assets/providers/doubao.png';
import iconDeepinfra from '@/assets/providers/deepinfra.png';

export type BrandArt = {
  src: string;
  /** The logo is a transparent glyph and needs a light bed to stay visible. */
  padded?: boolean;
};

/** Local runtimes ship their logo as a dark glyph on transparency. */
const PADDED = new Set([
  'ollama',
  'lm-studio',
  'vllm',
  // Dark glyph on transparency: needs a light bed on the dark shell.
  'stepfun',
  'stepfun-plan',
  'xai',
  'grok',
  'grok-oauth',
]);

const LLM_PROVIDER_ART: Record<string, string> = {
  openai: iconOpenai,
  'codex-oauth': iconOpenai,
  anthropic: iconAnthropic,
  'claude-oauth': iconAnthropic,
  gemini: iconGemini,
  openrouter: iconOpenrouter,
  grok: iconXai,
  'grok-oauth': iconXai,
  xai: iconXai,
  'kimi-oauth': iconKimi,
  mimo: iconMimo,
  'mimo-oauth': iconMimo,
  stepfun: iconStepfun,
  'stepfun-plan': iconStepfun,
  'doubao-plan': iconDoubao,
  'doubao-coding': iconDoubao,
  'deepseek-account': iconDeepseek,
  'dashscope-intl': iconDashscope,
  deepinfra: iconDeepinfra,
  'z-ai-oauth': iconZai,
  'z-ai-cn-oauth': iconZai,
  'gemini-oauth': iconGemini,
  'z-ai': iconZai,
  'z-ai-coding': iconZai,
  'z-ai-cn': iconZai,
  'z-ai-cn-coding': iconZai,
  minimax: iconMinimax,
  'minimax-coding': iconMinimax,
  'minimax-intl': iconMinimax,
  dashscope: iconDashscope,
  volcengine: iconVolcengine,
  moonshot: iconMoonshot,
  'moonshot-coding': iconMoonshot,
  deepseek: iconDeepseek,
  groq: iconGroq,
  cerebras: iconCerebras,
  ollama: iconOllama,
  'lm-studio': iconLmStudio,
  vllm: iconVllm,
};

/** The bundled mark for an LLM provider id, or undefined for one we do not ship. */
export function llmProviderArt(provider: string): BrandArt | undefined {
  const src = LLM_PROVIDER_ART[provider];
  if (!src) return undefined;
  return { src, padded: PADDED.has(provider) };
}

/**
 * The mark an MCP server declared for itself in the handshake.
 *
 * A fourth keyspace and the only one whose art we neither ship nor look up:
 * the server named it, the host fetched it, and what arrives here is a path
 * this origin already serves. There is nothing to decide, which is why this is
 * the same one-line shape as `bundleArt` rather than a lookup like the two
 * maps above.
 */
export function mcpServerArt(server: {
  icon_url?: string | null;
}): BrandArt | undefined {
  if (!server.icon_url) return undefined;
  return { src: `${baseURL}${server.icon_url}` };
}

/**
 * A shipped bundle's mark, when it wears someone else's brand.
 *
 * Only someone else's: our own bundles draw `ConductorMark`, which is inline
 * and takes the tile's own color, so there is nothing here to return for them.
 * A wrapper bundle is the opposite case in every way. The brand is not ours to
 * redraw, it changes without a frontend release, and the good art is only
 * discoverable by reading the vendor's page, so the manifest names a site and
 * the backend resolves it.
 *
 * A third keyspace, deliberately not folded into either map above: bundle
 * names, provider ids and server names collide by design (`openai` is a
 * plausible value in all three), and one lookup over them would hand a bundle
 * the model provider's logo.
 */
export function bundleArt(bundle: { icon_url?: string | null }): BrandArt | undefined {
  // The manifest decides, not the name.
  if (!bundle.icon_url) return undefined;
  return { src: `${baseURL}${bundle.icon_url}` };
}
