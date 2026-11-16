// @vitest-environment node
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';

const REPO_ROOT = resolve(__dirname, '..', '..', '..');
// Sweep the whole source tree: any statically-written locale key anywhere in
// the app must resolve in both catalogs. (This started Dashboard-only; the
// chat surface grew its own keys and drifted silently.)
const SRC_DIR = resolve(REPO_ROOT, 'src');

// The catalogs are read from disk, not imported: the test setup hands the
// imported objects to i18next, and any feature module a test pulls in merges
// its tn* bundle into them (addResourceBundle, deep), so an import could hold
// feature keys the file does not.
type Catalog = Record<string, unknown>;
const readJson = (path: string): Catalog => JSON.parse(readFileSync(path, 'utf8')) as Catalog;
const enMain = readJson(resolve(REPO_ROOT, 'src/locales/en-US.json'));
const zhMain = readJson(resolve(REPO_ROOT, 'src/locales/zh-CN.json'));

// ── Feature locale files ──────────────────────────────────
//
// Terminal-native slices keep their copy in `<dir>/locales/tn<Name>.{en-US,
// zh-CN}.json`, one top-level `tn<Name>` namespace per pair, registered at
// module load (lib/featureLocales.ts). They are part of the catalog the app
// resolves against, so the sweep below checks references into them too.
interface FeaturePair {
  namespace: string;
  dir: string;
  en: Catalog | null;
  zh: Catalog | null;
}

function featurePairs(): FeaturePair[] {
  const pairs = new Map<string, FeaturePair>();
  const visit = (dir: string) => {
    for (const entry of readdirSync(dir)) {
      const full = join(dir, entry);
      if (statSync(full).isDirectory()) {
        if (entry !== 'node_modules') visit(full);
        continue;
      }
      const m = /^(tn[A-Z][A-Za-z0-9]*)\.(en-US|zh-CN)\.json$/.exec(entry);
      if (!m || !dir.endsWith('/locales')) continue;
      const id = `${dir}/${m[1]}`;
      const pair = pairs.get(id) ?? { namespace: m[1], dir: dir.replace(REPO_ROOT + '/', ''), en: null, zh: null };
      pair[m[2] === 'en-US' ? 'en' : 'zh'] = readJson(full);
      pairs.set(id, pair);
    }
  };
  visit(SRC_DIR);
  return [...pairs.values()].sort((a, b) => a.namespace.localeCompare(b.namespace));
}

const FEATURES = featurePairs();

function withFeatures(main: Catalog, side: 'en' | 'zh'): Catalog {
  const merged: Catalog = { ...main };
  for (const pair of FEATURES) {
    const bundle = pair[side];
    if (bundle && bundle[pair.namespace] && !(pair.namespace in main)) merged[pair.namespace] = bundle[pair.namespace];
  }
  return merged;
}

// What the app resolves against: the main catalog plus every tn* bundle.
const enUS = withFeatures(enMain, 'en');
const zhCN = withFeatures(zhMain, 'zh');

// A captured string counts as a locale key only when its first segment is a
// real top-level catalog namespace — that keeps dotted non-keys the regexes
// can also match ('settings.json', module paths) out of the report.
const NAMESPACES = new Set(Object.keys(enUS));
function isLocaleKey(candidate: string): boolean {
  const dot = candidate.indexOf('.');
  return dot > 0 && NAMESPACES.has(candidate.slice(0, dot));
}

// Match `t('foo.bar.baz')` and `t("foo.bar.baz")` — the second arg form for
// interpolation is fine because we only capture the first quoted argument.
const T_CALL = /\bt\(\s*['"]([a-zA-Z0-9_.]+)['"]/g;
// `<Trans i18nKey="foo.bar">` names its key as a JSX prop.
const I18N_KEY = /\bi18nKey\s*=\s*\{?\s*['"]([a-zA-Z0-9_.]+)['"]/g;
// Match `titleKey: 'foo.bar'` / `descriptionKey: 'foo.bar'` / etc. — keys
// stored in widget definitions, STATUS_UI tables, and PresetMeta. Catches our
// static metadata references that aren't wrapped in t(). A bare `key:` is a
// locale key too (freshness specs, table columns); a React `key` never names
// a catalog namespace, so `isLocaleKey` keeps it out.
const KEY_PROP = /\b(?:key|titleKey|descriptionKey|nameKey|tagKey|bestForKey|labelKey|blurbKey|messageKey|noteKey|bodyKey|descKey)\s*[:=]\s*['"]([a-zA-Z0-9_.]+)['"]/g;
// Bare quoted keys held in const maps and passed to a helper rather than to
// `t()` directly: SOURCE_KEY / BUCKET_KEY on the dashboard, the tab-label map
// and the skill-action failure helper under plugins, and the probe verdict
// tables under mcp, and the status, group and delivery tables under
// automation. Scoped to those namespaces rather than swept tree-wide,
// because `isLocaleKey` only checks
// the first segment and plenty of dotted non-keys (module paths, filenames)
// would otherwise qualify. The cost of being in this list is that a namespace
// here may not also be used for storage keys or other dotted identifiers --
// see `plugins:deckExpanded`, which uses a colon for exactly that reason.
const KEY_VALUE = /['"]((?:dashboard|plugins|orders|mcp|automation|timezone)\.[a-zA-Z0-9_.]+)['"]/g;

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === '__tests__' || entry === 'node_modules') continue;
      walk(full, out);
      continue;
    }
    if (entry.endsWith('.ts') || entry.endsWith('.tsx')) {
      out.push(full);
    }
  }
  return out;
}

function lookup(obj: unknown, key: string): unknown {
  return key.split('.').reduce<unknown>((acc, part) => {
    if (acc && typeof acc === 'object' && part in (acc as object)) {
      return (acc as Record<string, unknown>)[part];
    }
    return undefined;
  }, obj);
}

// i18next falls back to plural-suffixed variants (`_one` / `_other` /
// `_zero`) when the bare key is absent and the call passes `count`, and to
// `_ordinal_*` ones when it also passes `ordinal: true`. A test that only
// checks the bare key would falsely flag those plural-only entries.
const PLURAL_SUFFIXES = ['', '_one', '_other', '_zero', '_two', '_few', '_many', '_ordinal_other'];
function resolveAnyVariant(obj: unknown, key: string): boolean {
  for (const suffix of PLURAL_SUFFIXES) {
    if (typeof lookup(obj, key + suffix) === 'string') return true;
  }
  return false;
}

function collectKeys(): { keys: Set<string>; perFile: Map<string, string[]> } {
  const files = walk(SRC_DIR);
  const keys = new Set<string>();
  const perFile = new Map<string, string[]>();
  for (const file of files) {
    const src = readFileSync(file, 'utf8');
    const found: string[] = [];
    for (const re of [T_CALL, I18N_KEY, KEY_PROP, KEY_VALUE]) {
      re.lastIndex = 0;
      let m: RegExpExecArray | null;
      while ((m = re.exec(src)) !== null) {
        if (isLocaleKey(m[1])) {
          keys.add(m[1]);
          found.push(m[1]);
        }
      }
    }
    if (found.length > 0) perFile.set(file, found);
  }
  return { keys, perFile };
}

// ── Interpolation ─────────────────────────────────────────
//
// A `{{var}}` the call does not pass renders literally ("No quote for
// {{symbol}}"), and nothing else catches it: the key resolves, the types
// pass. So for each `t('key', …)` whose options are an object literal, every
// variable in the key's strings (each plural variant, both catalogs) has to
// be among the names the literal passes. A call whose options are a variable
// or carry a spread is skipped, since what it passes is not in the text.

const VAR = /\{\{\s*-?\s*([A-Za-z_$][\w$]*)/g;

function stringVariants(obj: unknown, key: string): string[] {
  return PLURAL_SUFFIXES.map((suffix) => lookup(obj, key + suffix)).filter((v): v is string => typeof v === 'string');
}

/** The source from `start` (an opening bracket) to its match, skipping
 *  brackets inside string and template literals. */
function balanced(src: string, start: number): string | null {
  const open = src[start];
  const close = open === '{' ? '}' : ')';
  let depth = 0;
  let quote: string | null = null;
  for (let i = start; i < src.length; i++) {
    const c = src[i];
    if (quote) {
      if (c === '\\') i++;
      else if (c === quote) quote = null;
      continue;
    }
    if (c === "'" || c === '"' || c === '`') quote = c;
    else if (c === '{' || c === '(' || c === '[') depth++;
    else if (c === '}' || c === ')' || c === ']') {
      depth--;
      if (depth === 0) return c === close ? src.slice(start + 1, i) : null;
    }
  }
  return null;
}

/** The top-level property names of an object literal's body, or null when a
 *  spread makes them unknowable. */
function literalNames(body: string): Set<string> | null {
  const names = new Set<string>();
  let depth = 0;
  let quote: string | null = null;
  let part = '';
  const parts: string[] = [];
  for (let i = 0; i < body.length; i++) {
    const c = body[i];
    if (quote) {
      part += c;
      if (c === '\\') part += body[++i] ?? '';
      else if (c === quote) quote = null;
      continue;
    }
    if (c === "'" || c === '"' || c === '`') quote = c;
    else if (c === '{' || c === '(' || c === '[') depth++;
    else if (c === '}' || c === ')' || c === ']') depth--;
    if (c === ',' && depth === 0) {
      parts.push(part);
      part = '';
    } else part += c;
  }
  parts.push(part);
  for (const raw of parts) {
    const p = raw.trim();
    if (!p) continue;
    if (p.startsWith('...')) return null;
    const m = /^(?:['"]([^'"]+)['"]|([A-Za-z_$][\w$]*))\s*(?::|$)/.exec(p);
    if (m) names.add(m[1] ?? m[2]);
  }
  return names;
}

interface TCallSite {
  key: string;
  file: string;
  /** Names the options literal passes; empty when there are no options. */
  passed: Set<string>;
}

function collectCallSites(): TCallSite[] {
  const sites: TCallSite[] = [];
  const call = /\bt\(\s*['"]([a-zA-Z0-9_.]+)['"]\s*/g;
  for (const file of walk(SRC_DIR)) {
    const src = readFileSync(file, 'utf8');
    call.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = call.exec(src)) !== null) {
      if (!isLocaleKey(m[1])) continue;
      let i = call.lastIndex;
      let passed: Set<string> | null = new Set();
      if (src[i] === ',') {
        i++;
        while (/\s/.test(src[i] ?? '')) i++;
        if (src[i] !== '{') continue; // options held in a variable
        const body = balanced(src, i);
        if (body === null) continue;
        passed = literalNames(body);
      } else if (src[i] !== ')') {
        continue;
      }
      if (passed) sites.push({ key: m[1], file: file.replace(REPO_ROOT + '/', ''), passed });
    }
  }
  return sites;
}

describe('locale interpolation (src-wide)', () => {
  const sites = collectCallSites();

  it('finds call sites to check (sanity check)', () => {
    expect(sites.filter((s) => s.passed.size > 0).length).toBeGreaterThan(50);
  });

  it('every {{variable}} in a key is passed where the key is used', () => {
    const offenders: string[] = [];
    for (const site of sites) {
      for (const [name, catalog] of [['en-US', enUS], ['zh-CN', zhCN]] as const) {
        const missing = new Set<string>();
        for (const text of stringVariants(catalog, site.key)) {
          VAR.lastIndex = 0;
          let v: RegExpExecArray | null;
          while ((v = VAR.exec(text)) !== null) if (!site.passed.has(v[1])) missing.add(v[1]);
        }
        if (missing.size) offenders.push(`  - ${site.key} (${name}) needs ${[...missing].join(', ')} in ${site.file}`);
      }
    }
    if (offenders.length > 0) throw new Error(`Interpolation variables not passed (${offenders.length}):\n${offenders.join('\n')}`);
  });
});

describe('locale key parity (src-wide)', () => {
  const { keys, perFile } = collectKeys();

  it('discovers a non-trivial number of keys (sanity check)', () => {
    expect(keys.size).toBeGreaterThan(200);
  });

  it('every referenced key resolves in en-US.json', () => {
    const missing: { key: string; files: string[] }[] = [];
    for (const key of keys) {
      if (resolveAnyVariant(enUS, key)) continue;
      const where: string[] = [];
      for (const [file, fileKeys] of perFile) {
        if (fileKeys.includes(key)) where.push(file.replace(REPO_ROOT + '/', ''));
      }
      missing.push({ key, files: where });
    }
    if (missing.length > 0) {
      const report = missing.map((m) => `  - ${m.key}\n    referenced in: ${m.files.join(', ')}`).join('\n');
      throw new Error(`Missing en-US keys (${missing.length}):\n${report}`);
    }
  });

  it('every referenced key resolves in zh-CN.json', () => {
    const missing: string[] = [];
    for (const key of keys) {
      if (!resolveAnyVariant(zhCN, key)) missing.push(key);
    }
    if (missing.length > 0) {
      throw new Error(`Missing zh-CN keys (${missing.length}):\n${missing.map((k) => '  - ' + k).join('\n')}`);
    }
  });

  it('zh-CN entries are non-empty strings', () => {
    // Keys that haven't been translated yet are flagged with the
    // `__pending: <english>` prefix (string, not object) — translators can
    // grep for `__pending:` to find work to do. We don't enforce any keys
    // are fully translated, just that the slot is non-empty.
    const offenders: string[] = [];
    for (const key of keys) {
      const value = lookup(zhCN, key);
      if (typeof value === 'string' && value.length === 0) offenders.push(key);
    }
    expect(offenders).toEqual([]);
  });
});

// ── Whole-catalog coverage ────────────────────────────────
//
// i18n.ts loads no fallback language, so a zh-CN visitor sees a raw key for
// anything zh-CN.json cannot resolve, including keys built at runtime that
// the sweep above cannot see. So every en-US key has to resolve in zh-CN the
// way i18next resolves it there. zh has one plural form, `other`, so a plural
// family is covered by its `_other` variant or by the bare key.

function leaves(obj: unknown, prefix = ''): string[] {
  if (!obj || typeof obj !== 'object') return [prefix];
  return Object.entries(obj as Record<string, unknown>).flatMap(([k, v]) => leaves(v, prefix ? `${prefix}.${k}` : k));
}

const PLURAL = /^(.*?)(_ordinal)?_(zero|one|two|few|many|other)$/;

describe('zh-CN covers the whole en-US catalog', () => {
  it('every en-US key resolves in zh-CN', () => {
    const missing = leaves(enUS).filter((key) => {
      const plural = PLURAL.exec(key);
      if (!plural) return typeof lookup(zhCN, key) !== 'string';
      const [, base, ordinal = ''] = plural;
      return typeof lookup(zhCN, `${base}${ordinal}_other`) !== 'string' && typeof lookup(zhCN, base) !== 'string';
    });
    if (missing.length > 0) {
      throw new Error(`en-US keys zh-CN cannot resolve (${missing.length}):\n${missing.map((k) => '  - ' + k).join('\n')}`);
    }
  });
});

// ── Trans slots ───────────────────────────────────────────
//
// `<Trans>` fills a paired tag with the catalog's content between the tags, so
// `<kbd></kbd>` renders its component empty and drops the children the call
// site gave it (a keycap glyph, an icon). A slot whose component carries its
// own content has to be self-closing, `<kbd/>`.
describe('Trans slots', () => {
  it('no catalog value has an empty paired tag', () => {
    const EMPTY_PAIR = /<(\w+)><\/\1>/;
    const offenders = ([['en-US', enUS], ['zh-CN', zhCN]] as const).flatMap(([name, catalog]) =>
      leaves(catalog)
        .filter((key) => EMPTY_PAIR.test(String(lookup(catalog, key))))
        .map((key) => `${name}: ${key}`),
    );
    expect(offenders).toEqual([]);
  });
});

// ── zh-CN house rules ─────────────────────────────────────
//
// Two rules a translation pass keeps breaking, checked over the whole catalog
// since a new key is where they slip in. A product name or a code term beside
// Chinese takes a space on each side ("在 E2B 上"), and "Agent" and "Chief of
// Staff" are product names, left in English rather than rendered as 智能体 or
// 幕僚长.
describe('zh-CN house rules', () => {
  const values = leaves(zhCN).map((key) => [key, String(lookup(zhCN, key))] as const);

  it('puts a space between CJK and Latin letters', () => {
    const TOUCHING = /[一-鿿][A-Za-z]|[A-Za-z][一-鿿]/;
    expect(values.filter(([, v]) => TOUCHING.test(v)).map(([k]) => k)).toEqual([]);
  });

  it('never translates Agent or Chief of Staff', () => {
    expect(values.filter(([, v]) => /智能体|幕僚长/.test(v)).map(([k]) => k)).toEqual([]);
  });
});

// ── No finance vocabulary ──────────────────────────────────
//
// Conductor ships the Super-agent only, so neither catalog may carry the
// market-data, brokerage or equity-research vocabulary the product used to
// have. Checked over the whole catalog (path and value, both languages) since
// a new key is exactly where a finance string slips back in — the sweep above
// cannot see it because it only looks at keys code references.
//
// The allowlist is the set of hits that are deliberately kept, each for a
// reason that is not finance:
//   plugins.discoverPage.marketsLabel  kept key; the value reads "Filter by region"
//   plugins.discoverPage.factMarkets   kept key; the value reads "Coverage"
//   plugins.skillUpload.problem.unsafePath
//                                      "quote" is a quotation mark here
//   vault.security.encrypted           "pgcrypto" merely contains "crypto"
//
// "price" is bare on purpose: the automation price-trigger keys
// (priceCondition, priceValue, priceReference, priceRetrigger, priceCooldown)
// carried their finance only in the key path, so the "price alert" and
// "share price" phrases this list used to carry let them through.
const FINANCE_TERMS =
  /finance|financ|ticker|watchlist|stock|market|portfolio|trading|price|quote|crypto|equit|dividend|earnings|valuation|fundamental|\bipo\b|\betf\b|forex|commodit|bull market|bear market|xueqiu|雪球|财经|股票|股市|行情|自选|持仓|投资|板块|研报|证券|\bsec\b|filings|\bdcf\b|\bwacc\b|thesis|catalyst|sector|moat|screening|estimates|consensus|macro|options chain|daily prices|company overview|balance sheet|income statement|cash flow|share price|invest|3-statement/i;

const KEPT_FINANCE_HITS = [
  'plugins.discoverPage.factMarkets',
  'plugins.discoverPage.marketsLabel',
  'plugins.skillUpload.problem.unsafePath',
  'vault.security.encrypted',
];

describe('no finance vocabulary in either catalog', () => {
  const hits = (['en-US', 'zh-CN'] as const).map((name) => {
    const catalog = name === 'en-US' ? enUS : zhCN;
    return {
      name,
      keys: leaves(catalog).filter((key) => FINANCE_TERMS.test(key) || FINANCE_TERMS.test(String(lookup(catalog, key)))),
    };
  });

  it('finds finance terms to sweep (sanity check)', () => {
    // The term list has to be live: a regex that matches nothing would make
    // the guard below pass no matter what the catalogs grow into.
    expect(hits.reduce((n, h) => n + h.keys.length, 0)).toBeGreaterThan(0);
  });

  it('holds nothing outside the documented kept hits', () => {
    const offenders = hits.flatMap(({ name, keys }) =>
      keys.filter((key) => !KEPT_FINANCE_HITS.includes(key)).map((key) => `${name}: ${key}`),
    );
    expect(offenders).toEqual([]);
  });

  it('still holds every documented kept hit', () => {
    // A kept hit that quietly disappears is as much a drift as a new one.
    const kept = hits.flatMap(({ keys }) => keys).filter((key) => KEPT_FINANCE_HITS.includes(key));
    expect([...new Set(kept)].sort()).toEqual([...KEPT_FINANCE_HITS].sort());
  });
});

// ── Feature locale sweep ──────────────────────────────────
describe('feature locale files (tn*)', () => {
  it('finds the terminal-native feature pairs (sanity check)', () => {
    expect(FEATURES.length).toBeGreaterThanOrEqual(10);
  });

  it('every feature file has both an en-US and a zh-CN side', () => {
    const lonely = FEATURES.filter((p) => !p.en || !p.zh).map((p) => `${p.dir}/${p.namespace}`);
    expect(lonely).toEqual([]);
  });

  it('each file holds exactly its own tn* namespace', () => {
    const offenders: string[] = [];
    for (const pair of FEATURES) {
      for (const [side, bundle] of [['en-US', pair.en], ['zh-CN', pair.zh]] as const) {
        if (!bundle) continue;
        const keys = Object.keys(bundle);
        if (keys.length !== 1 || keys[0] !== pair.namespace) offenders.push(`${pair.dir}/${pair.namespace}.${side}.json: ${keys.join(', ')}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it('en-US and zh-CN agree on every key of every pair', () => {
    const offenders: string[] = [];
    for (const pair of FEATURES) {
      if (!pair.en || !pair.zh) continue;
      const en = new Set(leaves(pair.en));
      const zh = new Set(leaves(pair.zh));
      for (const k of en) if (!zh.has(k)) offenders.push(`${pair.namespace}: zh-CN lacks ${k}`);
      for (const k of zh) if (!en.has(k)) offenders.push(`${pair.namespace}: en-US lacks ${k}`);
    }
    expect(offenders).toEqual([]);
  });

  it('no zh-CN feature value is empty', () => {
    const offenders = FEATURES.flatMap((pair) =>
      pair.zh ? leaves(pair.zh).filter((k) => lookup(pair.zh, k) === '').map((k) => `${pair.namespace}: ${k}`) : [],
    );
    expect(offenders).toEqual([]);
  });

  it('no tn* namespace collides with a main-catalog namespace', () => {
    const main = new Set([...Object.keys(enMain), ...Object.keys(zhMain)]);
    expect(FEATURES.map((p) => p.namespace).filter((ns) => main.has(ns))).toEqual([]);
  });

  it('no two feature pairs share a namespace', () => {
    const seen = new Map<string, string>();
    const dupes: string[] = [];
    for (const pair of FEATURES) {
      const prev = seen.get(pair.namespace);
      if (prev) dupes.push(`${pair.namespace}: ${prev} and ${pair.dir}`);
      seen.set(pair.namespace, pair.dir);
    }
    expect(dupes).toEqual([]);
  });

  it('the main catalogs no longer carry the unused commandPalette block', () => {
    expect('commandPalette' in enMain).toBe(false);
    expect('commandPalette' in zhMain).toBe(false);
  });
});

// ── Unknown and retired namespaces ────────────────────────
//
// The sweeps above only count a literal as a key when its first segment is a
// namespace the catalog still has, so a `t('nav.foo')` written after a whole
// namespace was deleted would slip past them and render the raw key. Two
// guards close that: whatever `t()` or `i18nKey` is handed is a key by
// construction, so a dotted literal there must name a live namespace; and the
// namespaces the S30 sweep deleted outright stay retired, so no literal
// anywhere (key tables included) may start with one.
const RETIRED_NAMESPACES = Object.freeze([
  'agentFleet', 'automation', 'browser', 'commandPalette', 'computer', 'context',
  'dispatch', 'excelViewer', 'filePanel', 'memoPanel', 'memoryPanel', 'nav',
  'onboarding', 'pdfViewer', 'researchBrief', 'savedProjects', 'share', 'shareLink',
  'superagentProgress', 'thread', 'timezone', 'workspace',
]);

/** Dotted literals passed to t() / i18nKey whose namespace the catalog lacks. */
function unknownNamespaceRefs(src: string, namespaces: Set<string>): string[] {
  const out: string[] = [];
  for (const re of [T_CALL, I18N_KEY]) {
    re.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = re.exec(src)) !== null) {
      const dot = m[1].indexOf('.');
      if (dot > 0 && !namespaces.has(m[1].slice(0, dot))) out.push(m[1]);
    }
  }
  return out;
}

// Dotted literals in a retired namespace that are not locale keys.
//   dispatch.history  localStorage key for the dispatch history (dispatch/model.ts)
const RETIRED_NON_KEYS = ['dispatch.history'];

/** Key-shaped literals (t(), i18nKey, key props, quoted strings) in a retired namespace. */
function retiredNamespaceRefs(src: string): string[] {
  const retired = new RegExp(`['"\`]((?:${RETIRED_NAMESPACES.join('|')})\\.[A-Za-z0-9_$.{]*)`, 'g');
  return [...src.matchAll(retired)].map((m) => m[1]).filter((k) => !RETIRED_NON_KEYS.includes(k));
}

describe('unknown and retired namespaces', () => {
  it('flags a t() or i18nKey key whose namespace is gone (self-test)', () => {
    const fixture = `t('nav.inbox'); <Trans i18nKey="workspace.title" />; t('settings.title'); t('tnInbox.title');`;
    expect(unknownNamespaceRefs(fixture, NAMESPACES)).toEqual(['nav.inbox', 'workspace.title']);
    // The parity sweep alone does not see them: it drops unknown namespaces.
    expect(isLocaleKey('nav.inbox')).toBe(false);
  });

  it('flags a literal in a retired namespace, static or built (self-test)', () => {
    const fixture = "const K = { a: 'thread.title' }; t(`shareLink.${x}`); 'settings.thread.x'";
    expect(retiredNamespaceRefs(fixture)).toEqual(['thread.title', 'shareLink.${x']);
  });

  it('no t() or i18nKey key names a namespace missing from the catalogs', () => {
    const offenders = walk(SRC_DIR).flatMap((file) =>
      unknownNamespaceRefs(readFileSync(file, 'utf8'), NAMESPACES).map((k) => `${k} in ${file.replace(REPO_ROOT + '/', '')}`),
    );
    expect(offenders).toEqual([]);
  });

  it('the retired namespaces stay out of both catalogs and out of the source', () => {
    expect(RETIRED_NAMESPACES.filter((ns) => ns in enMain || ns in zhMain)).toEqual([]);
    expect(RETIRED_NAMESPACES.filter((ns) => FEATURES.some((p) => p.namespace === ns))).toEqual([]);
    const offenders = walk(SRC_DIR).flatMap((file) =>
      retiredNamespaceRefs(readFileSync(file, 'utf8')).map((k) => `${k} in ${file.replace(REPO_ROOT + '/', '')}`),
    );
    expect(offenders).toEqual([]);
  });
});
