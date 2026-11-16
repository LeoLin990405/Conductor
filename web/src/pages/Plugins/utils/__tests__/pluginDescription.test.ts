import { describe, expect, it } from 'vitest';
import { pluginDescription } from '../pluginDescription';

describe('pluginDescription', () => {
  it('shows the Chinese line for a bundled plugin in a Chinese locale', () => {
    expect(pluginDescription('conductor-deliverables', 'English manifest copy', 'zh-CN')).toContain('报告');
    expect(pluginDescription('conductor-service', 'English manifest copy', 'zh-Hans')).toContain('定时任务');
  });

  it('drops the upstream Conductor brand from Chinese plugin names', async () => {
    const { zhName } = await import('../zhText');
    for (const id of ['conductor-deliverables', 'conductor-service']) {
      expect(zhName('plugin', id, id, 'zh-CN')).not.toContain('Conductor');
    }
  });

  it('falls back to the manifest description for unknown plugins or other locales', () => {
    expect(pluginDescription('third-party', 'Its own words', 'zh-CN')).toBe('Its own words');
    expect(pluginDescription('conductor-deliverables', 'English manifest copy', 'en-US')).toBe('English manifest copy');
    expect(pluginDescription('conductor-service', null, 'zh-CN')).toContain('定时任务');
    expect(pluginDescription(null, undefined, 'zh-CN')).toBe('');
  });
});
