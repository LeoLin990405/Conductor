/**
 * Read-only panel: what every fleet CLI has in its skill directory.
 *
 * The agent-fleet dashboard's skills view. It only reads — installs,
 * uninstalls and rollbacks belong to the `conductor skills` CLI, which owns
 * the tracked manifest and the snapshot history this inventory reports on.
 */
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { fleetSkillsErrorMessage, getFleetSkillsInventory } from '@/api/fleetSkills';
import { registerFeatureLocales } from '@/lib/featureLocales';
import tnAgentsEn from './locales/tnAgents.en-US.json';
import tnAgentsZh from './locales/tnAgents.zh-CN.json';
import './agents.css';

// The panel is a lazy chunk of the Agents page; it registers the same pair
// (overwrite=false) so it also renders its copy when mounted on its own.
registerFeatureLocales({ 'en-US': tnAgentsEn, 'zh-CN': tnAgentsZh });

export function FleetSkillsPanel() {
  const { t } = useTranslation();
  const inventory = useQuery({
    queryKey: ['fleet-skills'],
    queryFn: () => getFleetSkillsInventory(),
    staleTime: 30_000,
  });

  if (inventory.isPending) {
    return <p className="sa-muted" data-testid="fleet-skills-loading">{t('tnAgents.fleet.loading')}</p>;
  }
  if (inventory.isError) {
    return (
      <p className="sa-error" data-testid="fleet-skills-error" role="alert">
        {t('tnAgents.fleet.unavailable')} {fleetSkillsErrorMessage(inventory.error)}
      </p>
    );
  }

  const { repo, skills, agents, presets, projects } = inventory.data;
  const agentNames = Object.keys(agents).sort();
  return (
    <div className="ag-fleet" data-testid="fleet-skills-panel">
      <p className="ag-lede">
        {t('tnAgents.fleet.readOnly')} <code className="ag-code">conductor skills</code>.
      </p>
      <p className="ag-statusline">
        {t('tnAgents.fleet.repo')} <code className="ag-code">{repo}</code>
      </p>

      <h3 className="ag-subtitle">{t('tnAgents.fleet.repoTitle')} <span className="ag-count">{skills.length}</span></h3>
      {skills.length === 0 ? (
        <p className="sa-muted">{t('tnAgents.fleet.repoEmpty')}</p>
      ) : (
        <ul className="ag-rows ag-rows--plain" data-testid="fleet-skills-repo">
          {skills.map((skill) => (
            <li key={skill.name} className="ag-row ag-row--skill">
              <span className="ag-name">{skill.name}</span>
              <span className="ag-meta">
                {skill.description ? skill.description : null}
                {skill.installed.length > 0 ? (
                  <span className="ag-dim"> ({t('tnAgents.fleet.installed')} {skill.installed.join(', ')})</span>
                ) : null}
              </span>
            </li>
          ))}
        </ul>
      )}

      <h3 className="ag-subtitle">{t('tnAgents.fleet.agentsTitle')} <span className="ag-count">{agentNames.length}</span></h3>
      {agentNames.map((agent) => {
        const state = agents[agent];
        return (
          <div key={agent} className="ag-fleet-agent" data-testid={`fleet-skills-agent-${agent}`}>
            <h4 className="ag-fleet-agent-name">{agent}</h4>
            <p className="ag-meta">
              <code className="ag-code">{state.dir}</code>
              {state.exists ? '' : ` ${t('tnAgents.fleet.dirMissing')}`}
            </p>
            {state.error ? (
              <p className="sa-error" role="alert">{t('tnAgents.fleet.dirError')} {state.error}</p>
            ) : null}
            {state.broken_symlinks.length > 0 ? (
              <p className="sa-error" role="alert">{t('tnAgents.fleet.brokenLinks')} {state.broken_symlinks.join(', ')}</p>
            ) : null}
            {state.entries.length === 0 ? (
              <p className="sa-muted">{t('tnAgents.fleet.noEntries')}</p>
            ) : (
              <ul className="ag-rows ag-rows--plain">
                {state.entries.map((entry) => (
                  <li key={entry.name} className="ag-row ag-row--skill">
                    <span className="ag-name">{entry.name}</span>
                    <span className="ag-meta">
                      {entry.kind}
                      {' · '}
                      <span className={entry.managed ? 'ag-tag ag-tag--ok' : 'ag-tag ag-tag--idle'}>
                        {entry.managed ? t('tnAgents.fleet.managed') : t('tnAgents.fleet.foreign')}
                      </span>
                      {entry.healthy ? null : (
                        <>
                          {' '}
                          <span className="ag-tag ag-tag--bad">{t('tnAgents.fleet.broken')}</span>
                        </>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        );
      })}

      <h3 className="ag-subtitle">{t('tnAgents.fleet.presetsTitle')} <span className="ag-count">{presets.length}</span></h3>
      {presets.length === 0 ? (
        <p className="sa-muted">{t('tnAgents.fleet.presetsEmpty')}</p>
      ) : (
        <ul className="ag-rows ag-rows--plain">
          {presets.map((preset) => (
            <li key={preset.name} className="ag-row ag-row--skill">
              <span className="ag-name">{preset.name}</span>
              <span className="ag-meta">{preset.skills.join(', ')}</span>
            </li>
          ))}
        </ul>
      )}

      <h3 className="ag-subtitle">{t('tnAgents.fleet.projectsTitle')} <span className="ag-count">{projects.length}</span></h3>
      {projects.length === 0 ? (
        <p className="sa-muted">{t('tnAgents.fleet.projectsEmpty')}</p>
      ) : (
        <ul className="ag-rows ag-rows--plain">
          {projects.map((project) => (
            <li key={project.project} className="ag-row ag-row--skill">
              <span className="ag-name">{project.project}</span>
              <span className="ag-meta">{project.lines.join(', ')}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default FleetSkillsPanel;
