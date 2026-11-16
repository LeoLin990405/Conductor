import { zhDescription } from './zhText';

/** The plugin row's one-line description, in Chinese for bundled plugins. */
export function pluginDescription(
  name: string | null | undefined,
  description: string | null | undefined,
  language: string | undefined,
): string {
  return zhDescription('plugin', name, description, language);
}
