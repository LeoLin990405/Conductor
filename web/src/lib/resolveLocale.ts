// Dependency-free on purpose: scripts/locale-preload.ts serialises this
// function into index.html's inline preload, so it may not close over anything
// outside its own body.

/** The last resort when neither the cookie nor the browser names a catalog. */
export const DEFAULT_LOCALE = 'zh-CN';

/**
 * The locale a visitor gets: the `locale` cookie wins, otherwise the
 * deployment default (zh-CN in this fork — 完全中文版). The browser-language
 * probe of upstream was removed deliberately: a Chinese-market deployment
 * shows Chinese regardless of the visitor's OS language, and the header
 * language switcher still writes the cookie for anyone who wants English.
 * The cookie is checked against the list rather than looked up in it, so
 * `locale=constructor` is just unknown.
 */
export function resolveLocale<L extends string>(
  cookie: string,
  language: string,
  locales: readonly L[],
  fallback: L,
): L {
  const exact = (v: string) => locales.find((l) => l === v);
  let fromCookie = '';
  const m = cookie.match(/(?:^|;\s*)locale=([^;]+)/);
  if (m) {
    try {
      fromCookie = decodeURIComponent(m[1]);
    } catch {
      // Malformed %XX reads as no cookie; throwing would white-screen i18n init.
    }
  }
  void language;
  return exact(fromCookie) ?? fallback;
}
