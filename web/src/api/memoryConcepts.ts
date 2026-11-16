/**
 * Memory concepts — the ontology view of the memory projection.
 *
 * A memory names what kind of claim it is (`class`) and the thing it is about
 * (`about`, a concept id such as `project:conductor`). The browse page lists
 * the concepts the owner has memory for and then the memories under one of
 * them, so both reads live here rather than in the page.
 *
 * Both endpoints are flag-shaped: `enabled` is false when the memory
 * projection is switched off, and the lists are then empty rather than absent.
 */
import { api } from '@/api/client';

/** One `(class, about)` pair and how many memories name it. */
export interface MemoryConcept {
  /** The kind of claim, or null for a memory that names no class. */
  class: string | null;
  /** The concept id, or null for a memory that names no concept. */
  about: string | null;
  /** How many memories name this pair. */
  entries: number;
}

export interface MemoryConceptsResponse {
  /** False when the memory projection is switched off for this user. */
  enabled: boolean;
  concepts: MemoryConcept[];
}

/** One memory row as the projection holds it. */
export interface MemoryConceptEntry {
  entry_id: string;
  tier: string;
  entry_key: string;
  index_text: string;
  status: string;
  /** The kind of claim, or null for a memory that names no class. */
  class: string | null;
  /** The primary concept id, or null for a memory that names no concept. */
  about: string | null;
  /**
   * Every concept the memory declares, in order. A memory may be about more
   * than one thing, and the primary one is only the first of them.
   */
  concepts?: string[];
  updated_at?: string | null;
}

export interface MemoryConceptEntriesResponse {
  enabled: boolean;
  entries: MemoryConceptEntry[];
}

/** The narrowings the browse page can ask for. */
export interface MemoryConceptFilters {
  /** The kind of claim to keep. */
  class?: string | null;
  /** The concept id to keep. */
  about?: string | null;
  /**
   * Keep only the memories that name no class at all -- the unnamed class
   * group, which is not the same as asking for every memory.
   */
  classIsNull?: boolean;
  /**
   * Keep only the memories that name no concept at all -- the unnamed
   * concept group, which is not the same as asking for every memory.
   */
  aboutIsNull?: boolean;
  /** How many memories to read at most. */
  limit?: number;
  /** Where to continue the listing past its first page. */
  offset?: number;
}

/** How one page of a listing is asked for. */
export interface MemoryConceptPage {
  /** How many rows to read at most. */
  limit?: number;
  /** Where the page starts. */
  offset?: number;
}

/** Every concept the owner has memory for, most-used first. */
export async function getMemoryConcepts(
  page: MemoryConceptPage = {},
): Promise<MemoryConceptsResponse> {
  const { data } = await api.get<MemoryConceptsResponse>('/api/v1/memory/concepts', {
    params: {
      limit: page.limit,
      offset: page.offset,
    },
  });
  return data;
}

/** The owner's memories, narrowed to one class and/or one concept. */
export async function getMemoryConceptEntries(
  filters: MemoryConceptFilters = {},
): Promise<MemoryConceptEntriesResponse> {
  const { data } = await api.get<MemoryConceptEntriesResponse>('/api/v1/memory/entries', {
    params: {
      class: filters.class ?? undefined,
      about: filters.about ?? undefined,
      class_is_null: filters.classIsNull ? true : undefined,
      about_is_null: filters.aboutIsNull ? true : undefined,
      limit: filters.limit,
      offset: filters.offset,
    },
  });
  return data;
}
