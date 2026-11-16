/** Registers the tnSettings pair once, at module load, for every Settings entry point. */
import { registerFeatureLocales } from '@/lib/featureLocales';
import tnSettingsEn from './tnSettings.en-US.json';
import tnSettingsZh from './tnSettings.zh-CN.json';

registerFeatureLocales({ 'en-US': tnSettingsEn, 'zh-CN': tnSettingsZh });
