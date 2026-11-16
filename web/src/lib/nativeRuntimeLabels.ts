/** Display labels for the vendor-native runtime inventory.
 *
 * The backend owns the provider ids and live capability flags; this map owns
 * only presentation, shared by the chat composer, automation options, and the
 * Settings inventory so a new live provider never grows three copies of its
 * label.
 */
const NATIVE_RUNTIME_LABELS: Record<string, string> = {
  codex: 'Codex',
  'claude-code': 'Claude Code',
  cursor: 'Cursor',
  'deepseek-dsh': 'DeepSeek DSH',
  zcode: 'GLM / ZCode',
  minimax: 'MiniMax Code',
  mimo: 'MiMo Code',
  kimi: 'Kimi',
  grok: 'Grok',
  stepfun: 'StepFun',
};

export function nativeRuntimeLabel(provider: string): string {
  return NATIVE_RUNTIME_LABELS[provider] || provider;
}
