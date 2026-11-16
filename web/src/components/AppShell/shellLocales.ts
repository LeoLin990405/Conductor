/**
 * tnShell — the authenticated chrome's own copy.
 *
 * The main catalogs are frozen until S30, so the shell registers its pair
 * beside them. Importing this module is enough: navItems imports it before it
 * declares a label key, and every renderer of the rail/tab bar therefore sees
 * tnShell already merged into the active language.
 */
import { registerFeatureLocales, type FeatureLocaleBundle } from '@/lib/featureLocales';
import enUS from './locales/tnShell.en-US.json';
import zhCN from './locales/tnShell.zh-CN.json';

export const SHELL_LOCALES: Record<'en-US' | 'zh-CN', FeatureLocaleBundle> = {
  'en-US': enUS,
  'zh-CN': zhCN,
};

registerFeatureLocales(SHELL_LOCALES);
