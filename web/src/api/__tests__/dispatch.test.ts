import { describe, expect, it, vi } from 'vitest';
import { ApiError, api } from '@/api/client';
import {
  approveDispatch,
  getDispatch,
  getProviderCatalog,
  listActiveDispatches,
  reassignDispatchLine,
  rejectDispatch,
  startDispatch,
} from '../dispatch';

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof import('@/api/client')>('@/api/client');
  return {
    ...actual,
    api: { ...actual.api, get: vi.fn(), post: vi.fn() },
  };
});

const get = api.get as unknown as ReturnType<typeof vi.fn>;
const post = api.post as unknown as ReturnType<typeof vi.fn>;

describe('dispatch api', () => {
  it('lists active rows from either envelope', async () => {
    get.mockResolvedValueOnce({ data: { dispatches: [{ id: 'd1' }] } });
    await expect(listActiveDispatches()).resolves.toEqual([{ id: 'd1' }]);
    expect(get).toHaveBeenCalledWith('/api/v1/dispatch/status/active');

    get.mockResolvedValueOnce({ data: [{ id: 'd2' }] });
    await expect(listActiveDispatches()).resolves.toEqual([{ id: 'd2' }]);
  });

  it('turns 404 into an empty detail', async () => {
    get.mockRejectedValueOnce(new ApiError('missing', 'ERR_BAD_REQUEST', {
      status: 404,
      statusText: 'Not Found',
      data: {},
      headers: {},
    }));
    await expect(getDispatch('missing')).resolves.toBeNull();
  });

  it('reads one dispatch from its own path', async () => {
    get.mockResolvedValueOnce({ data: { id: 'd3adbeef' } });

    await expect(getDispatch('d3adbeef')).resolves.toEqual({ id: 'd3adbeef' });
    expect(get).toHaveBeenCalledWith('/api/v1/dispatch/d3adbeef');
  });

  it('posts the note and nothing else', async () => {
    post.mockResolvedValue({ data: {} });
    await approveDispatch('d3adbeef', 'looks good');
    await rejectDispatch('d3adbeef', '');
    expect(post).toHaveBeenNthCalledWith(1, '/api/v1/dispatch/d3adbeef/approve', { note: 'looks good' });
    expect(post).toHaveBeenNthCalledWith(2, '/api/v1/dispatch/d3adbeef/reject', { note: '' });
  });

  it('starts a dispatch with the goal and returns it', async () => {
    post.mockResolvedValueOnce({ data: { id: 'd9', goal: 'ship it', state: 'SUBMITTED' } });

    await expect(startDispatch('ship it')).resolves.toEqual({
      id: 'd9',
      goal: 'ship it',
      state: 'SUBMITTED',
    });
    expect(post).toHaveBeenCalledWith('/api/v1/dispatch', { goal: 'ship it' });
  });

  it('reassigns one line to a model', async () => {
    post.mockResolvedValueOnce({ data: { id: 'd3adbeef' } });

    await expect(reassignDispatchLine('d3adbeef', 'line-0', 'kimi-k3-coding')).resolves.toEqual({
      id: 'd3adbeef',
    });
    expect(post).toHaveBeenCalledWith('/api/v1/dispatch/d3adbeef/lines/line-0/reassign', {
      model: 'kimi-k3-coding',
    });
  });

  it('escapes an id that names a path segment', async () => {
    post.mockResolvedValueOnce({ data: {} });

    await reassignDispatchLine('a b', 'line/0', 'kimi');

    expect(post).toHaveBeenCalledWith('/api/v1/dispatch/a%20b/lines/line%2F0/reassign', {
      model: 'kimi',
    });
  });

  it('reads the provider of a model from the model catalog', async () => {
    get.mockResolvedValueOnce({
      data: {
        models: {
          kimi: { display_name: 'Kimi', models: ['kimi-k3-coding'] },
          codex: { display_name: 'Codex', models: ['gpt-5.6-terra'] },
        },
        model_metadata: {
          'kimi-k3-coding': { provider: 'kimi' },
          'gpt-5.6-terra': { provider: 'codex' },
          'orphan-model': { provider: 'kimi' },
        },
      },
    });

    const catalog = await getProviderCatalog();

    expect(catalog.byModel['kimi-k3-coding']).toBe('kimi');
    expect(catalog.byModel['gpt-5.6-terra']).toBe('codex');
    expect(catalog.byModel['orphan-model']).toBe('kimi');
    expect(catalog.providers).toEqual(expect.arrayContaining(['kimi', 'codex']));
    expect(get).toHaveBeenCalledWith('/api/v1/models');
  });

  it('reads an empty catalog from a body that names no model', async () => {
    get.mockResolvedValueOnce({ data: {} });

    await expect(getProviderCatalog()).resolves.toEqual({ byModel: {}, providers: [] });
  });
});
