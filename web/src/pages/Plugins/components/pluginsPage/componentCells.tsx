import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { BrandMark } from '@/components/mcp/BrandMark';
import type { SkillInfo, SubagentView } from '@/api/core';
import { isEffectivelyEnabled } from '../../utils/provenance';
import { zhDescription, zhName } from '../../utils/zhText';
import { CellState, CommandChip, SurfaceCell } from '../surface/Surface';

/**
 * The cells a plugin's page lists its skills and subagents with, and the bare
 * cell for a component whose row is not in the lists (yet). A plugin's
 * connectors use the Connectors grid's own cells without their menu.
 */

/** A component the plugin lists whose row is not in the lists (yet). */
export function PluginBareCell({ name, onOpen }: { name: string; onOpen: () => void }) {
  return <SurfaceCell mark={<BrandMark name={name} kind="server" size="md" />} name={name} onOpen={onOpen} />;
}

/** A skill as a cell: no mark, its command only when it is not its name. */
export function SkillCell({
  name,
  skill,
  meta,
  trailing,
  menu,
  onOpen,
}: {
  name: string;
  skill: SkillInfo | null;
  /** Extra words after the command ("2 of 5 workspaces"). */
  meta?: ReactNode;
  /** Overrides the on/off state ("Plugin off"). */
  trailing?: ReactNode;
  menu?: ReactNode;
  onOpen: () => void;
}) {
  const { i18n } = useTranslation();
  const displayName = zhName('skill', skill?.name ?? name, name, i18n.language);
  const command = skill?.command && skill.command !== name ? skill.command : null;
  const on = skill ? isEffectivelyEnabled(skill) : true;
  return (
    <SurfaceCell
      mark={<BrandMark name={name} kind="skill" size="md" />}
      name={displayName}
      meta={
        command || meta ? (
          <>
            {command && <CommandChip command={command} />}
            {meta && <span className="pp-cell-meta">{meta}</span>}
          </>
        ) : null
      }
      line={zhDescription('skill', skill?.name, skill?.description, i18n.language) || null}
      off={!on}
      trailing={trailing ?? (skill ? <CellState state={on ? 'on' : 'off'} /> : null)}
      menu={menu}
      onOpen={onOpen}
    />
  );
}

/** A subagent as a cell, saying whether the user has changed it: uninstall
 *  takes those changes with it. One without a view, which the Subagents list
 *  leaves out while its plugin is off, has no page there to open. */
export function SubagentCell({
  name,
  view,
  onOpen,
}: {
  name: string;
  view: SubagentView | null;
  onOpen: () => void;
}) {
  const { t, i18n } = useTranslation();
  const on = view ? view.enabled : true;
  const displayName = zhName('subagent', view?.name ?? name, name, i18n.language);
  return (
    <SurfaceCell
      mark={<BrandMark name={name} kind="subagent" size="md" />}
      name={displayName}
      meta={view?.edited ? <span className="pp-cell-meta">{t('plugins.subagents.edited')}</span> : null}
      line={zhDescription('subagent', view?.name, view?.description, i18n.language) || null}
      off={!on}
      trailing={view ? <CellState state={on ? 'on' : 'off'} /> : null}
      onOpen={view ? onOpen : undefined}
    />
  );
}
