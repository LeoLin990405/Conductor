import { describe, expect, it, vi } from 'vitest';
import { api } from '@/api/client';
import { getMemoryConceptEntries, getMemoryConcepts } from '../memoryConcepts';

vi.mock('@/api/client', () => ({
  api: {
    get: vi.fn(),
  },
}));

const get = api.get as unknown as ReturnType<typeof vi.fn>;

describe('getMemoryConcepts', () => {
  it('unwraps the concepts envelope', async () => {
    const payload = {
      enabled: true,
      concepts: [{ class: 'Preference', about: 'person:leo', entries: 3 }],
    };
    get.mockResolvedValue({ data: payload });

    await expect(getMemoryConcepts()).resolves.toEqual(payload);
    expect(get).toHaveBeenCalledWith('/api/v1/memory/concepts', {
      params: { limit: undefined, offset: undefined },
    });
  });

  it('keeps a memory that names no concept', async () => {
    const payload = {
      enabled: true,
      concepts: [{ class: null, about: null, entries: 1 }],
    };
    get.mockResolvedValue({ data: payload });

    await expect(getMemoryConcepts()).resolves.toEqual(payload);
  });

  it('asks for the page after the first one', async () => {
    get.mockResolvedValue({ data: { enabled: true, concepts: [] } });

    await getMemoryConcepts({ limit: 200, offset: 200 });

    // The concepts beyond the first page are only reachable by asking for
    // the page that holds them.
    expect(get).toHaveBeenCalledWith('/api/v1/memory/concepts', {
      params: { limit: 200, offset: 200 },
    });
  });
});

describe('getMemoryConceptEntries', () => {
  it('sends the class and about filters', async () => {
    get.mockResolvedValue({ data: { enabled: true, entries: [] } });

    await getMemoryConceptEntries({ class: 'Preference', about: 'person:leo' });

    expect(get).toHaveBeenCalledWith('/api/v1/memory/entries', {
      params: {
        class: 'Preference',
        about: 'person:leo',
        class_is_null: undefined,
        about_is_null: undefined,
        limit: undefined,
      },
    });
  });

  it('omits a filter that is not set', async () => {
    get.mockResolvedValue({ data: { enabled: true, entries: [] } });

    await getMemoryConceptEntries({ class: 'Preference', limit: 50 });

    expect(get).toHaveBeenCalledWith('/api/v1/memory/entries', {
      params: {
        class: 'Preference',
        about: undefined,
        class_is_null: undefined,
        about_is_null: undefined,
        limit: 50,
      },
    });
  });

  it('sends the unnamed group as an explicit null filter', async () => {
    get.mockResolvedValue({ data: { enabled: true, entries: [] } });

    await getMemoryConceptEntries({ classIsNull: true, aboutIsNull: true });

    // The unnamed group is a selection of its own. Sending it as an absent
    // filter would ask for every memory instead of the unnamed ones.
    expect(get).toHaveBeenCalledWith('/api/v1/memory/entries', {
      params: {
        class: undefined,
        about: undefined,
        class_is_null: true,
        about_is_null: true,
        limit: undefined,
        offset: undefined,
      },
    });
  });

  it('asks for the page after the first one', async () => {
    get.mockResolvedValue({ data: { enabled: true, entries: [] } });

    await getMemoryConceptEntries({ class: 'Preference', limit: 100, offset: 100 });

    // The memories beyond the first page are only reachable by asking for
    // the page that holds them.
    expect(get).toHaveBeenCalledWith('/api/v1/memory/entries', {
      params: {
        class: 'Preference',
        about: undefined,
        class_is_null: undefined,
        about_is_null: undefined,
        limit: 100,
        offset: 100,
      },
    });
  });
});
