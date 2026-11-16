import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { QueryClient } from '@tanstack/react-query';
import { getSubagents, putSubagent } from '@/api/core/api/subagents';
import type { SubagentAudience, SubagentsResponse, SubagentView } from '@/api/core';
import i18n from '@/i18n';
import { queryKeys } from '@/lib/queryKeys';
import { sendSubagentEdit } from '@/hooks/useSubagents';
import { audienceChoice, audienceFacts } from '../subagentText';
import { AvailableBlock } from '../SubagentAside';

/**
 * `analyst` was renamed `generalist`. A server that predates the rename still
 * answers with the old spelling, which the pane reads as the same audience
 * rather than one no switch names; every write it makes sends the new one.
 */

const get = vi.hoisted(() => vi.fn());
const put = vi.hoisted(() => vi.fn());
vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  return { ...actual, api: { ...actual.api, get, put } };
});

const t = i18n.getFixedT('en-US');

const BASE = {
  model: null,
  effort: null,
  tools: ['web'],
  search_provider: null,
  search_depth: null,
  connectors: false,
  memory: true,
  preferred_connectors: [],
  preferred_skills: [],
  enabled: true,
};

/** A row as a server that still spells the audience the old way answers. */
const LEGACY_ROW = {
  name: 'summarizer',
  kind: 'base',
  source: 'builtin',
  plugin: null,
  edited: false,
  user_fields: [],
  description: 'Summarizes docs.',
  instructions: '',
  enabled: true,
  model: null,
  effort: null,
  tools: ['web'],
  search_provider: null,
  search_depth: null,
  connectors: false,
  memory: true,
  preferred_connectors: [],
  preferred_skills: [],
  available_to: ['analyst'],
  base: BASE,
  problems: [],
  updated_by: null,
  updated_thread_id: null,
  updated_thread_title: null,
  updated_at: null,
  file: 'summarizer.md',
  version: 'v1',
} as unknown as SubagentView;

const LIST = {
  main: { model: null, effort: null, search_provider: null, search_depth: null },
  subagents: [LEGACY_ROW],
  connectors: [],
  skills: [],
  limits: { max_custom: 20, description_max: 400, instructions_max_bytes: 16384 },
} as unknown as SubagentsResponse;

/** The row the pane works with once the list has been read. */
async function readRow(available_to: string[] = ['analyst']): Promise<SubagentView> {
  get.mockResolvedValue({ data: { ...LIST, subagents: [{ ...LEGACY_ROW, available_to }] } });
  const data = await getSubagents();
  return data.subagents[0];
}

describe('a row an older server spells analyst', () => {
  beforeEach(() => {
    get.mockReset();
    put.mockReset();
  });

  it('reads the list as the new name', async () => {
    const row = await readRow();
    expect(row.available_to).toEqual(['generalist']);
  });

  it('reads a save as the new name', async () => {
    put.mockResolvedValue({ data: LEGACY_ROW });
    const view = await putSubagent('summarizer', { version: 'v1', fields: {} });
    expect(view.available_to).toEqual(['generalist']);
  });

  it('leaves an audience the rename does not know as the server spelled it', async () => {
    const row = await readRow(['analyst', 'scribe']);
    expect(row.available_to).toEqual(['generalist', 'scribe']);
  });

  it('saves a row it read as the new name, and reads the answer back as it', async () => {
    const read = await readRow();
    const qc = new QueryClient();
    qc.setQueryData(queryKeys.subagents.list(), {
      ...LIST,
      subagents: [read],
    } as unknown as SubagentsResponse);
    // The server answers with the old spelling again, as one mid-rename does.
    put.mockResolvedValue({ data: { ...LEGACY_ROW, version: 'v2' } });

    const saved = await sendSubagentEdit(qc, 'summarizer', { available_to: read.available_to });

    expect(put).toHaveBeenCalledWith(
      '/api/v1/subagents/summarizer',
      expect.objectContaining({ version: 'v1', fields: { available_to: ['generalist'] } }),
    );
    expect(saved.available_to).toEqual(['generalist']);
  });
});

describe('the availability switch on such a row', () => {
  beforeEach(() => {
    get.mockReset();
    put.mockReset();
  });

  it('ticks the switch the old spelling maps to', async () => {
    render(<AvailableBlock view={await readRow()} write={() => {}} />);
    expect(screen.getByRole('checkbox', { name: 'Workspace generalist' })).toBeChecked();
  });

  it('stays clear for a row that grants no audience at all', async () => {
    render(<AvailableBlock view={await readRow([])} write={() => {}} />);
    expect(screen.getByRole('checkbox', { name: 'Workspace generalist' })).not.toBeChecked();
  });
});

describe('the audience words for the old spelling', () => {
  it('names the generalist for a row an older server sent', () => {
    expect(audienceChoice(t, 'analyst' as SubagentAudience)).toBe('Workspace generalist');
    expect(audienceFacts(t, ['analyst' as SubagentAudience])).toBe('Workspace generalist');
  });
});
