/// <reference types="vitest/globals" />
import { configure } from '@testing-library/react';
import i18n, { initI18n } from '@/i18n';
import enUS from '@/locales/en-US.json';
import zhCN from '@/locales/zh-CN.json';
import { installBrowserTraps } from './browserTraps';
import { installJsdomShims } from './jsdomShims';

// The app's i18n config with both catalogs handed over up front, so `t()`
// returns real strings and a language switch lands before the next line. The
// app loads catalogs on demand, which would make every switch in a test async.
const catalogs = { 'en-US': { translation: enUS }, 'zh-CN': { translation: zhCN } };

// One setup file for both environments: a test file that declares
// `// @vitest-environment node` has no `window`, and gets the traps instead of
// the jsdom shims.
if (typeof window === 'undefined') {
  // jsdom answers `navigator.language` with en-US; Node derives it from the
  // machine's locale. Pin it so a node file renders the same strings on every
  // machine. Done before the traps so i18n's own document probe still runs.
  await initI18n(catalogs);
  await i18n.changeLanguage('en-US');
  installBrowserTraps();
} else {
  await initI18n(catalogs);
  // UI tests assert the English catalog; the product still defaults to zh-CN.
  // Locale-specific tests explicitly change their cookie or active language.
  await i18n.changeLanguage('en-US');
  // The default 1s `findBy*`/`waitFor` budget is too tight when a loaded
  // machine runs many jsdom files: a slower first paint turns an eventual
  // render into a false "not in the document". The per-test timeout still
  // bounds genuinely hung tests.
  configure({ asyncUtilTimeout: 3000 });
  installJsdomShims();
}
