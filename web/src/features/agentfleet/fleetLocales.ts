/**
 * Registers the tnAgentFleet copy beside the main catalogs (S03
 * registerFeatureLocales), so the page never edits en-US.json / zh-CN.json.
 */
import { registerFeatureLocales } from '@/lib/featureLocales';
import en from './locales/tnAgentFleet.en-US.json';
import zh from './locales/tnAgentFleet.zh-CN.json';

registerFeatureLocales({ 'en-US': en, 'zh-CN': zh });

export { en as tnAgentFleetEn, zh as tnAgentFleetZh };
