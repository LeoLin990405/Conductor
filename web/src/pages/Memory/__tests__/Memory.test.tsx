import { describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import Memory from '../Memory';

const h = vi.hoisted(() => ({
  concepts: {
    enabled: true,
    concepts: [
      { class: 'Preference', about: 'person:leo', entries: 2 },
      { class: null, about: null, entries: 1 },
    ],
  },
  entries: {
    enabled: true,
    entries: [
      {
        entry_id: 'e1',
        tier: 'user',
        entry_key: 'prefs/language',
        index_text: 'prefers Chinese',
        status: 'active',
        class: 'Preference',
        about: 'person:leo',
        updated_at: '2026-11-12T00:00:00Z',
      },
    ],
  },
  moreConcepts: { enabled: true, concepts: [] as { class: string | null; about: string | null; entries: number }[] },
  moreEntries: { enabled: true, entries: [] as Record<string, unknown>[] },
  conceptCalls: [] as unknown[],
  entryCalls: [] as unknown[],
}));

vi.mock('@/api/memoryConcepts', () => ({
  getMemoryConcepts: vi.fn(async (page: { offset?: number } = {}) => {
    h.conceptCalls.push(page);
    return page.offset ? h.moreConcepts : h.concepts;
  }),
  getMemoryConceptEntries: vi.fn(async (filters: { offset?: number } = {}) => {
    h.entryCalls.push(filters);
    return filters.offset ? h.moreEntries : h.entries;
  }),
}));

/** The class rail, so the "Unclassified" class button is not confused with
 *  the unnamed group of the concept list. */
function classRail() {
  return screen.getByRole('group', { name: 'Class' });
}

/** A page that is exactly full: there may be more behind it. */
function fullPage(count: number, prefix: string) {
  return Array.from({ length: count }, (_, index) => ({
    entry_id: `${prefix}-${index}`,
    tier: 'user',
    entry_key: `${prefix}/${index}`,
    index_text: `${prefix} ${index}`,
    status: 'active',
    class: 'Preference',
    about: 'person:leo',
    updated_at: '2026-11-12T00:00:00Z',
  }));
}

describe('Memory page', () => {
  it('lists the concepts and the memory under the selected one', async () => {
    renderWithProviders(<Memory />, { route: '/memory?class=Preference&about=person:leo' });

    expect(screen.getByRole('heading', { name: 'Browse by concept' })).toBeInTheDocument();
    expect(await screen.findByText('person:leo')).toBeInTheDocument();
    expect(await screen.findByText('prefs/language')).toBeInTheDocument();
    expect(screen.getByText('prefers Chinese')).toBeInTheDocument();

    await waitFor(() => {
      expect(h.entryCalls).toContainEqual({
        class: 'Preference',
        about: 'person:leo',
        classIsNull: false,
        aboutIsNull: false,
        limit: 100,
        offset: 0,
      });
    });
  });

  it('shows a memory that names no concept as unclassified', async () => {
    renderWithProviders(<Memory />, { route: '/memory' });

    expect(await screen.findByText('No concept')).toBeInTheDocument();
    // The unnamed class is a group of its own, so the rail offers it.
    expect(within(classRail()).getByText('Unclassified')).toBeInTheDocument();
  });

  it('reads only the unnamed group when it is selected', async () => {
    h.entryCalls.length = 0;
    renderWithProviders(<Memory />, { route: '/memory' });

    const unnamed = await screen.findByText('No concept');
    const button = unnamed.closest('button');
    expect(button).not.toBeNull();
    button?.click();

    // The unnamed group is a selection, not the absence of one: it asks for
    // the memories that name no class and no concept, not for all of them.
    await waitFor(() => {
      expect(h.entryCalls).toContainEqual({
        class: null,
        about: null,
        classIsNull: true,
        aboutIsNull: true,
        limit: 100,
        offset: 0,
      });
    });
  });

  it('reads only the partly unnamed group when it is selected', async () => {
    h.entryCalls.length = 0;
    renderWithProviders(<Memory />, { route: '/memory?class=Preference&about_is_null=true' });

    await waitFor(() => {
      expect(h.entryCalls).toContainEqual({
        class: 'Preference',
        about: null,
        classIsNull: false,
        aboutIsNull: true,
        limit: 100,
        offset: 0,
      });
    });
  });

  it('reads every memory when no group is selected', async () => {
    h.entryCalls.length = 0;
    renderWithProviders(<Memory />, { route: '/memory' });

    await waitFor(() => {
      expect(h.entryCalls).toContainEqual({
        class: undefined,
        about: undefined,
        classIsNull: false,
        aboutIsNull: false,
        limit: 100,
        offset: 0,
      });
    });
  });

  it('says so when there is no memory to browse', async () => {
    h.concepts.concepts = [];
    renderWithProviders(<Memory />, { route: '/memory' });

    expect(await screen.findByText('No memory names a concept yet.')).toBeInTheDocument();
    h.concepts.concepts = [
      { class: 'Preference', about: 'person:leo', entries: 2 },
      { class: null, about: null, entries: 1 },
    ];
  });

  it('reports the projection being switched off', async () => {
    h.concepts.enabled = false;
    renderWithProviders(<Memory />, { route: '/memory' });

    expect(
      await screen.findByText('The memory projection is switched off for this user.'),
    ).toBeInTheDocument();
    h.concepts.enabled = true;
  });

  it('reads the memories beyond the first hundred', async () => {
    // A page that is full may have more behind it, so the page offers to
    // read on instead of stopping at the hundredth memory.
    const first = fullPage(100, 'memory');
    h.entries.entries = first;
    h.moreEntries.entries = fullPage(2, 'older');
    h.entryCalls.length = 0;

    renderWithProviders(<Memory />, { route: '/memory' });

    const more = await screen.findByRole('button', { name: 'Show more memories' });
    more.click();

    await waitFor(() => {
      expect(h.entryCalls).toContainEqual(
        expect.objectContaining({ limit: 100, offset: 100 }),
      );
    });
    // The older memories are on the page too, not only fetched.
    expect(await screen.findByText('older/0')).toBeInTheDocument();

    h.entries.entries = [
      {
        entry_id: 'e1',
        tier: 'user',
        entry_key: 'prefs/language',
        index_text: 'prefers Chinese',
        status: 'active',
        class: 'Preference',
        about: 'person:leo',
        updated_at: '2026-11-12T00:00:00Z',
      },
    ];
    h.moreEntries.entries = [];
  });

  it('reads the concepts beyond the first two hundred', async () => {
    // The same for the concept rail: the pairs past the first page are only
    // reachable by asking for the page that holds them.
    h.concepts.concepts = Array.from({ length: 200 }, (_, index) => ({
      class: 'Preference',
      about: `concept:${index}`,
      entries: 1,
    }));
    h.moreConcepts.concepts = [
      { class: 'Preference', about: 'concept:200', entries: 1 },
    ];
    h.conceptCalls.length = 0;

    renderWithProviders(<Memory />, { route: '/memory' });

    const more = await screen.findByRole('button', { name: 'Show more concepts' });
    more.click();

    await waitFor(() => {
      expect(h.conceptCalls).toContainEqual({ limit: 200, offset: 200 });
    });
    expect(await screen.findByText('concept:200')).toBeInTheDocument();

    h.concepts.concepts = [
      { class: 'Preference', about: 'person:leo', entries: 2 },
      { class: null, about: null, entries: 1 },
    ];
    h.moreConcepts.concepts = [];
  });

  it('does not offer to read on when a page is not full', async () => {
    renderWithProviders(<Memory />, { route: '/memory' });

    await screen.findByText('person:leo');
    expect(screen.queryByRole('button', { name: 'Show more concepts' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Show more memories' })).toBeNull();
  });
});
