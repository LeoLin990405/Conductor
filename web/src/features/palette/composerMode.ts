/**
 * Composer mode cycle (Alt+M): auto → dispatch → read-only → auto.
 * Owned by the palette host so every surface reads one source.
 */

export const COMPOSER_MODES = ['auto', 'dispatch', 'read-only'] as const;
export type ComposerMode = (typeof COMPOSER_MODES)[number];

let mode: ComposerMode = 'auto';
const listeners = new Set<(next: ComposerMode) => void>();

export function getComposerMode(): ComposerMode {
  return mode;
}

export function setComposerMode(next: ComposerMode): void {
  if (mode === next) return;
  mode = next;
  for (const listener of listeners) listener(mode);
}

export function cycleComposerMode(current: ComposerMode = mode): ComposerMode {
  const at = COMPOSER_MODES.indexOf(current);
  const next = COMPOSER_MODES[(at + 1) % COMPOSER_MODES.length];
  setComposerMode(next);
  return next;
}

export function subscribeComposerMode(listener: (next: ComposerMode) => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Reset between tests; product code should not call this. */
export function __resetComposerModeForTests(): void {
  mode = 'auto';
  listeners.clear();
}
