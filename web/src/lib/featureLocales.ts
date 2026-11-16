/**
 * Feature locale bundles (tn*) register beside the main catalogs without
 * editing en-US.json / zh-CN.json. Slices call registerFeatureLocales at
 * module load; parity and namespace collision checks live here so each
 * feature's own test can pin its pair.
 *
 * i18next only attaches addResourceBundle after init(). App is imported
 * before main awaits initI18n(), so a module-load register must wait for
 * the initialized event when the API is not yet present.
 */
import i18n from 'i18next';
import type { Locale } from '@/lib/locale';

export type FeatureLocaleBundle = Record<string, unknown>;

function applyBundles(bundles: Partial<Record<Locale, FeatureLocaleBundle>>): void {
  for (const [lng, bundle] of Object.entries(bundles) as Array<[Locale, FeatureLocaleBundle]>) {
    if (!bundle) continue;
    i18n.addResourceBundle(lng, 'translation', bundle, true, false);
  }
}

/** Deep-merge a feature bundle into the translation namespace (overwrite=false). */
export function registerFeatureLocales(
  bundles: Partial<Record<Locale, FeatureLocaleBundle>>,
): void {
  if (typeof i18n.addResourceBundle === 'function') {
    applyBundles(bundles);
    return;
  }
  const onReady = () => {
    i18n.off('initialized', onReady);
    applyBundles(bundles);
  };
  i18n.on('initialized', onReady);
}

function flattenKeys(value: unknown, prefix = ''): string[] {
  if (value === null || value === undefined) return prefix ? [prefix] : [];
  if (typeof value !== 'object' || Array.isArray(value)) return prefix ? [prefix] : [];
  const out: string[] = [];
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    const next = prefix ? `${prefix}.${key}` : key;
    if (child !== null && typeof child === 'object' && !Array.isArray(child)) {
      out.push(...flattenKeys(child, next));
    } else {
      out.push(next);
    }
  }
  return out;
}

/** Fail when en-US and zh-CN feature files disagree on key set. */
export function assertParity(
  en: FeatureLocaleBundle,
  zh: FeatureLocaleBundle,
): { enOnly: string[]; zhOnly: string[] } {
  const enKeys = new Set(flattenKeys(en));
  const zhKeys = new Set(flattenKeys(zh));
  const enOnly = [...enKeys].filter((k) => !zhKeys.has(k)).sort();
  const zhOnly = [...zhKeys].filter((k) => !enKeys.has(k)).sort();
  if (enOnly.length || zhOnly.length) {
    const parts: string[] = [];
    if (enOnly.length) parts.push(`en-only: ${enOnly.join(', ')}`);
    if (zhOnly.length) parts.push(`zh-only: ${zhOnly.join(', ')}`);
    throw new Error(`feature locale key parity failed (${parts.join('; ')})`);
  }
  return { enOnly, zhOnly };
}

/** A tn* top-level namespace must not already exist in a main catalog. */
export function assertNoNamespaceCollision(
  namespace: string,
  mainCatalog: Record<string, unknown>,
): void {
  if (Object.prototype.hasOwnProperty.call(mainCatalog, namespace)) {
    throw new Error(`feature locale namespace "${namespace}" collides with the main catalog`);
  }
}
