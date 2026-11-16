import { useMemo } from 'react';
import { useInfiniteQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import {
  getMemoryConceptEntries,
  getMemoryConcepts,
  type MemoryConcept,
  type MemoryConceptEntry,
} from '@/api/memoryConcepts';
import { queryKeys } from '@/lib/queryKeys';
import './Memory.css';

/**
 * Memory, browsed by concept.
 *
 * A memory names what kind of claim it is (`class`) and the thing it is about
 * (`about`, a concept id such as `project:conductor`). The rail lists the
 * concepts the owner has memory for; the column shows the memories under the
 * selected one. The selection lives in the URL, so a link to a concept is a
 * link to what the Worker would have read about it.
 *
 * A memory that names neither is shown as unclassified rather than hidden:
 * the projection never invents a class or a concept for it, and the page says
 * the same.
 *
 * The unnamed groups are selections of their own. A memory that names no
 * class is a group like any other, and selecting it must read that group --
 * not every memory. So the selection keeps three states per field: no
 * narrowing at all, a value, and the explicit "names nothing" case, which
 * travels to the API as `class_is_null` / `about_is_null` rather than as an
 * absent filter.
 *
 * Both listings are read a page at a time. An owner with more memories or
 * more concepts than one page holds can still reach the rest: the page ends
 * with a button that asks for the next one.
 */

/** One ontology narrowing: nothing selected, a value, or the unnamed group. */
type ConceptFilter = string | null | undefined;

/** How many concepts one page of the rail reads. */
const CONCEPT_PAGE_SIZE = 200;

/** How many memories one page of the column reads. */
const ENTRY_PAGE_SIZE = 100;

export default function MemoryPage() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();

  const classParam = searchParams.get('class');
  const aboutParam = searchParams.get('about');
  const classIsNull = searchParams.get('class_is_null') === 'true';
  const aboutIsNull = searchParams.get('about_is_null') === 'true';

  // undefined: no narrowing. null: the unnamed group. A string: that value.
  const classFilter: ConceptFilter = classIsNull ? null : (classParam ?? undefined);
  const aboutFilter: ConceptFilter = aboutIsNull ? null : (aboutParam ?? undefined);

  const concepts = useInfiniteQuery({
    queryKey: queryKeys.memory.concepts(),
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      getMemoryConcepts({ limit: CONCEPT_PAGE_SIZE, offset: pageParam }),
    getNextPageParam: (last, _pages, lastPageParam) =>
      last.concepts.length === CONCEPT_PAGE_SIZE
        ? lastPageParam + CONCEPT_PAGE_SIZE
        : undefined,
  });

  const filters = useMemo(
    () => ({
      class: classFilter === undefined ? undefined : classFilter,
      about: aboutFilter === undefined ? undefined : aboutFilter,
      classIsNull: classFilter === null,
      aboutIsNull: aboutFilter === null,
      limit: ENTRY_PAGE_SIZE,
    }),
    [classFilter, aboutFilter],
  );
  const entries = useInfiniteQuery({
    queryKey: queryKeys.memory.conceptEntries(filters),
    initialPageParam: 0,
    queryFn: ({ pageParam }) => getMemoryConceptEntries({ ...filters, offset: pageParam }),
    getNextPageParam: (last, _pages, lastPageParam) =>
      last.entries.length === ENTRY_PAGE_SIZE ? lastPageParam + ENTRY_PAGE_SIZE : undefined,
  });

  const select = (concept: MemoryConcept) => {
    const next = new URLSearchParams(searchParams);
    // A null field is a selection of the unnamed group, not the absence of
    // one: the flag says so explicitly, and the value is left out.
    if (concept.class) {
      next.set('class', concept.class);
      next.delete('class_is_null');
    } else {
      next.delete('class');
      next.set('class_is_null', 'true');
    }
    if (concept.about) {
      next.set('about', concept.about);
      next.delete('about_is_null');
    } else {
      next.delete('about');
      next.set('about_is_null', 'true');
    }
    setSearchParams(next, { replace: true });
  };

  const selectClass = (value: ConceptFilter) => {
    const next = new URLSearchParams(searchParams);
    if (value === undefined) {
      next.delete('class');
      next.delete('class_is_null');
    } else if (value === null) {
      next.delete('class');
      next.set('class_is_null', 'true');
    } else {
      next.set('class', value);
      next.delete('class_is_null');
    }
    // The concept of the old class may not belong to the new one.
    next.delete('about');
    next.delete('about_is_null');
    setSearchParams(next, { replace: true });
  };

  const rows = useMemo(
    () => concepts.data?.pages.flatMap((page) => page.concepts) ?? [],
    [concepts.data],
  );
  const classes = useMemo(() => {
    const seen: string[] = [];
    for (const row of rows) {
      if (row.class && !seen.includes(row.class)) seen.push(row.class);
    }
    return seen;
  }, [rows]);
  const hasUnnamedClass = useMemo(() => rows.some((row) => row.class === null), [rows]);
  const visible = useMemo(
    () =>
      classFilter === undefined
        ? rows
        : rows.filter((row) => (row.class ?? null) === classFilter),
    [rows, classFilter],
  );
  const entryRows = useMemo(
    () => entries.data?.pages.flatMap((page) => page.entries) ?? [],
    [entries.data],
  );

  const disabled =
    concepts.data?.pages[0]?.enabled === false || entries.data?.pages[0]?.enabled === false;

  return (
    <div className="memory-page">
      <header className="memory-head">
        <div>
          <h1>{t('memory.concepts.title')}</h1>
          <p>{t('memory.concepts.subtitle')}</p>
        </div>
      </header>

      <div className="memory-body">
        <aside className="memory-rail" aria-label={t('memory.conceptsLabel')}>
          <div className="memory-rail-head">
            <span className="memory-rail-title">{t('memory.conceptsLabel')}</span>
          </div>
          <div className="memory-classes" role="group" aria-label={t('memory.concept.class')}>
            <button
              type="button"
              className={`memory-class ${classFilter === undefined ? 'active' : ''}`}
              onClick={() => selectClass(undefined)}
              aria-pressed={classFilter === undefined}
            >
              {t('memory.concepts.all')}
            </button>
            {hasUnnamedClass ? (
              <button
                type="button"
                className={`memory-class ${classFilter === null ? 'active' : ''}`}
                onClick={() => selectClass(null)}
                aria-pressed={classFilter === null}
              >
                {t('memory.concept.unclassified')}
              </button>
            ) : null}
            {classes.map((name) => (
              <button
                key={name}
                type="button"
                className={`memory-class ${classFilter === name ? 'active' : ''}`}
                onClick={() => selectClass(name)}
                aria-pressed={classFilter === name}
              >
                {name}
              </button>
            ))}
          </div>

          {concepts.isPending ? <p className="memory-muted">{t('memory.listLoading')}</p> : null}
          {concepts.isError ? (
            <p className="memory-error" role="alert">
              {t('memory.concepts.loadError')}
            </p>
          ) : null}
          {concepts.isSuccess && rows.length === 0 ? (
            <p className="memory-muted">{t('memory.concepts.empty')}</p>
          ) : null}

          <ul className="memory-concepts">
            {visible.map((concept) => {
              const key = `${concept.class ?? ''}|${concept.about ?? ''}`;
              const active =
                (concept.class ?? null) === classFilter &&
                (concept.about ?? null) === aboutFilter;
              return (
                <li key={key}>
                  <button
                    type="button"
                    className={`memory-concept ${active ? 'active' : ''}`}
                    onClick={() => select(concept)}
                    aria-current={active ? 'true' : undefined}
                  >
                    <span className="memory-concept-class">
                      {concept.class ?? t('memory.concept.unclassified')}
                    </span>
                    <span className="memory-concept-about">
                      {concept.about ?? t('memory.concept.unnamed')}
                    </span>
                    <span className="memory-concept-count">{concept.entries}</span>
                  </button>
                </li>
              );
            })}
          </ul>
          {concepts.hasNextPage ? (
            <button
              type="button"
              className="memory-load-more"
              onClick={() => void concepts.fetchNextPage()}
              disabled={concepts.isFetchingNextPage}
            >
              {t('memory.concepts.loadMore')}
            </button>
          ) : null}
        </aside>

        <section className="memory-entries" aria-label={t('memory.listLabel')}>
          {disabled ? (
            <p className="memory-muted">{t('memory.concepts.disabled')}</p>
          ) : null}
          {!disabled && entries.isPending ? (
            <p className="memory-muted">{t('memory.listLoading')}</p>
          ) : null}
          {!disabled && entries.isError ? (
            <p className="memory-error" role="alert">
              {t('memory.listError')}
            </p>
          ) : null}
          {!disabled && entries.isSuccess && entryRows.length === 0 ? (
            <p className="memory-muted">{t('memory.concepts.entriesEmpty')}</p>
          ) : null}
          <ul className="memory-entry-list">
            {entryRows.map((entry) => (
              <MemoryEntryRow key={entry.entry_id} entry={entry} />
            ))}
          </ul>
          {!disabled && entries.hasNextPage ? (
            <button
              type="button"
              className="memory-load-more"
              onClick={() => void entries.fetchNextPage()}
              disabled={entries.isFetchingNextPage}
            >
              {t('memory.concepts.loadMoreEntries')}
            </button>
          ) : null}
        </section>
      </div>
    </div>
  );
}

function MemoryEntryRow({ entry }: { entry: MemoryConceptEntry }) {
  const { t } = useTranslation();
  const concepts = entry.concepts?.length ? entry.concepts : entry.about ? [entry.about] : [];
  return (
    <li className="memory-entry">
      <div className="memory-entry-head">
        <span className="memory-entry-key">{entry.entry_key}</span>
        <span className="memory-entry-tier">{entry.tier}</span>
      </div>
      <p className="memory-entry-text">{entry.index_text}</p>
      <div className="memory-entry-tags">
        <span className="memory-tag">
          {t('memory.concept.class')}: {entry.class ?? t('memory.concept.unclassified')}
        </span>
        <span className="memory-tag">
          {t('memory.concept.about')}:{' '}
          {concepts.length ? concepts.join(', ') : t('memory.concept.unnamed')}
        </span>
      </div>
    </li>
  );
}
