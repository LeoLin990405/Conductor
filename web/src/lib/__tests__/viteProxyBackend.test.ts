import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { describe, expect, it } from 'vitest';

type Env = Record<string, string | undefined>;
type ViteConfigModule = {
  proxyBackend: (viteEnv: Env, processEnv: Env) => string;
};

// A non-literal specifier keeps tsc from resolving the untyped JS config.
const CONFIG_URL = pathToFileURL(resolve(__dirname, '../../../vite.config.js')).href;

async function load(): Promise<ViteConfigModule> {
  return (await import(/* @vite-ignore */ CONFIG_URL)) as ViteConfigModule;
}

describe('vite proxy backend', () => {
  it('uses VITE_PROXY_BACKEND when cond passes it', async () => {
    const { proxyBackend } = await load();
    expect(
      proxyBackend({ VITE_PROXY_BACKEND: 'http://127.0.0.1:18123' }, { CONDUCTOR_BACKEND_PORT: '9' }),
    ).toBe('http://127.0.0.1:18123');
  });

  it('follows CONDUCTOR_BACKEND_PORT instead of a hard-coded 8000', async () => {
    const { proxyBackend } = await load();
    expect(proxyBackend({}, { CONDUCTOR_BACKEND_PORT: '18124' })).toBe('http://127.0.0.1:18124');
  });

  it('falls back to 8000 only when no port is configured or it is malformed', async () => {
    const { proxyBackend } = await load();
    expect(proxyBackend({}, {})).toBe('http://127.0.0.1:8000');
    expect(proxyBackend({}, { CONDUCTOR_BACKEND_PORT: 'eighty' })).toBe('http://127.0.0.1:8000');
    expect(proxyBackend({ VITE_PROXY_BACKEND: '  ' }, { CONDUCTOR_BACKEND_PORT: '70000' })).toBe(
      'http://127.0.0.1:8000',
    );
  });
});
