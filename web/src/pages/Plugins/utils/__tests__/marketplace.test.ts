// @vitest-environment node
import { describe, it, expect } from 'vitest';
import type {
  MarketplaceEntry,
  MarketplaceTerm,
} from '@/api/core/api/marketplace';
import { coverageLine, groupByCategory, matchesQuery, usedRegionTerms } from '../marketplace';

const term = (key: string, label: string, label_zh: string | null = null): MarketplaceTerm => ({
  key,
  label,
  label_zh,
});

const vocabulary = {
  assets: [
    term('docs', 'Docs', '文档'),
    term('guides', 'Guides', '指南'),
    term('templates', 'Templates', '模板'),
    term('datasets', 'Datasets', '数据集'),
    term('samples', 'Samples', '样例'),
  ],
  regions: [
    term('us', 'United States', '美国'),
    term('cn', 'Mainland China', '中国内地'),
    term('hk', 'Hong Kong', '中国香港'),
    term('global', 'Global', '全球'),
  ],
};

function entry(overrides: Partial<MarketplaceEntry> = {}): MarketplaceEntry {
  return {
    name: 'vendor',
    description: '',
    category: 'data',
    license: null,
    publisher: null,
    homepage: null,
    icon_url: null,
    keywords: [],
    auth: null,
    requires: null,
    coverage: null,
    source_url: 'https://github.com/acme/catalog/tree/main/plugins/vendor',
    installed: false,
    plugin: null,
    update_available: false,
    ...overrides,
  };
}

describe('matchesQuery', () => {
  const entries = [
    entry({ name: 'acme', publisher: { name: 'Acme Catalog', url: null, official: true } }),
    entry({ name: 'registry', keywords: ['index', 'search'] }),
    entry({ name: 'readme-builder', description: 'Builds a README from a template' }),
  ];
  const names = (q: string) => entries.filter((e) => matchesQuery(e, q)).map((e) => e.name);

  it('matches name, description, publisher and keywords, ignoring case', () => {
    expect(names('ACME')).toEqual(['acme']);
    expect(names('catalog')).toEqual(['acme']);
    expect(names('index')).toEqual(['registry']);
    expect(names('from a template')).toEqual(['readme-builder']);
    expect(names('  ')).toEqual(['acme', 'registry', 'readme-builder']);
  });
});

describe('chip terms', () => {
  const entries = [
    entry({ coverage: { markets: { docs: ['cn', 'hk'], guides: ['cn'] }, data: [] } }),
    entry({ coverage: { markets: { docs: ['us', 'mx'] }, data: [] } }),
    entry({ coverage: null }),
  ];

  it('offers only used regions, in vocabulary order, with unknown keys last', () => {
    const regions = usedRegionTerms(entries, vocabulary.regions);
    expect(regions.map((t) => t.key)).toEqual(['us', 'cn', 'hk', 'mx']);
    expect(regions[3].label).toBe('MX');
  });
});

describe('coverageLine', () => {
  it('reads kind labels and region codes, in vocabulary order, dropping a lone global', () => {
    const coverage = {
      markets: { datasets: ['global'], docs: ['us', 'hk', 'cn'], guides: ['cn'] },
      data: [],
    };
    expect(coverageLine(coverage, vocabulary, 'en-US')).toBe(
      'Docs US · CN · HK  ·  Guides CN  ·  Datasets',
    );
  });

  it('labels global beside named regions rather than spelling it as a code', () => {
    const coverage = { markets: { docs: ['global', 'us'] }, data: [] };
    expect(coverageLine(coverage, vocabulary, 'en-US')).toBe('Docs US · Global');
    expect(coverageLine(coverage, vocabulary, 'zh-CN')).toBe('文档 US · 全球');
  });

  it('reads zh kind labels in zh-CN and falls back to the label without one', () => {
    const zhVocab = { ...vocabulary, assets: [term('docs', 'Docs'), ...vocabulary.assets.slice(1)] };
    expect(coverageLine({ markets: { guides: ['cn'] }, data: [] }, zhVocab, 'zh-CN')).toBe('指南 CN');
    expect(coverageLine({ markets: { docs: ['cn'] }, data: [] }, zhVocab, 'zh-CN')).toBe(
      'Docs CN',
    );
  });

  it('uppercases a key the vocabulary lacks, and is empty without coverage', () => {
    expect(coverageLine({ markets: { archive: ['kr'] }, data: [] }, vocabulary, 'en-US')).toBe(
      'ARCHIVE KR',
    );
    expect(coverageLine(null, vocabulary, 'en-US')).toBe('');
  });
});

describe('groupByCategory', () => {
  it('stacks known categories in order and folds the rest into Other', () => {
    const entries = [
      entry({ name: 'a', category: 'community' }),
      entry({ name: 'b', category: null }),
      entry({ name: 'c', category: 'research' }),
      entry({ name: 'd', category: 'data' }),
      entry({ name: 'e', category: 'esoteric' }),
      entry({ name: 'f', category: 'data' }),
    ];
    expect(
      groupByCategory(entries).map(([c, rows]) => [c, rows.map((r) => r.name)]),
    ).toEqual([
      ['data', ['d', 'f']],
      ['research', ['c']],
      ['other', ['a', 'b', 'e']],
    ]);
  });
});
