import type { ReactNode } from 'react';
import type { TFunction } from 'i18next';
import { Trash2 } from 'lucide-react';

/**
 * The verbs at the foot of a connector's page, as a list so the page can
 * leave the footer out when there are none.
 */
export function serverDetailVerbs(
  t: TFunction,
  {
    builtin,
    plugin,
    owned,
    onOpenPlugin,
    edit,
    onRemove,
  }: {
    builtin: boolean;
    plugin: string | null;
    /** The row is its plugin's, which leaves with it and never on its own. */
    owned: boolean;
    onOpenPlugin: (plugin: string) => void;
    edit: ReactNode;
    onRemove?: () => void;
  },
): ReactNode[] {
  const verbs: ReactNode[] = [];
  if (owned && plugin) {
    verbs.push(
      <button key="plugin" type="button" className="connector-btn is-full" onClick={() => onOpenPlugin(plugin)}>
        {t('plugins.connectorsPage.openPluginButton', { plugin })}
      </button>,
      <p key="owned" className="connector-aside-note" style={{ marginTop: 0 }}>
        {t('plugins.connectorsPage.pluginOwnedNote', { plugin })}
      </p>,
    );
  }
  const remove = onRemove && !owned;
  if (edit || remove) {
    verbs.push(
      <div key="edit" className="connector-verbs-row">
        {edit}
        {remove && (
          <button type="button" className="connector-btn is-danger" onClick={onRemove}>
            <Trash2 className="h-3.5 w-3.5" />
            {t('plugins.connectorsPage.remove')}
          </button>
        )}
      </div>,
    );
  }
  // Editing a plugin's row is the same save as any other, and the save detaches
  // it from the plugin: said under the button, before it is pressed.
  if (owned && edit && plugin) {
    verbs.push(
      <p key="detach" className="connector-aside-note" style={{ marginTop: 0 }}>
        {t('plugins.connectorsPage.editDetaches', { plugin })}
      </p>,
    );
  }
  if (builtin) {
    verbs.push(
      <p key="builtin" className="connector-aside-note" style={{ marginTop: 0 }}>
        {t('plugins.connectorsPage.builtinNote')}
      </p>,
    );
  }
  return verbs;
}
