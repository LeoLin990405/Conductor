import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import { DetailHeader, DetailPage, DetailSection } from '../DetailPage';
import './SwarmDetail.css';

/** The one Agent-collaboration capability: no role presets, no per-thread
 *  mode. The main model decides whether a task is done directly, delegated
 *  to a sub-agent, or fanned out as parallel work. */
export function AgentTeamsDetail({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const labelId = useId();

  return (
    <DetailPage
      labelId={labelId}
      onClose={onClose}
      crumb={t('plugins.subagents.agentTeams.title', 'Agent 协作')}
      header={
        <DetailHeader
          name={t('plugins.subagents.agentTeams.title', 'Agent 协作')}
          labelId={labelId}
          kind="subagent"
          meta={<span className="pp-cell-meta">{t('plugins.subagents.agentTeams.meta', '主模型统一调度')}</span>}
          description={t(
            'plugins.subagents.agentTeams.description',
            '不再提供固定角色预设。主模型根据任务自行决定单 Agent、子 Agent 团队或并行执行；用户只看到一个能力开关，具体分工由主模型完成。',
          )}
        />
      }
    >
      <DetailSection title={t('plugins.subagents.agentTeams.modesTitle', '三种调度方式')}>
        <ol className="swarm-steps">
          <li>{t('plugins.subagents.agentTeams.modeDirect', '直接完成：任务足够简单，不需要拆分。')}</li>
          <li>{t('plugins.subagents.agentTeams.modeDelegate', '子 Agent 团队：需要研究、数据、报告等不同能力时，主模型按需派发。')}</li>
          <li>{t('plugins.subagents.agentTeams.modeParallel', '并行 Swarm：多个独立方向同时执行，最后统一汇总。')}</li>
        </ol>
      </DetailSection>
      <DetailSection title={t('plugins.subagents.agentTeams.boundaryTitle', '边界')}>
        <p className="pp-note">
          {t(
            'plugins.subagents.agentTeams.boundary',
            '用户不选择角色，也不为每个对话单独配置团队。子 Agent 的进度、停止和结果仍可在对话中查看；费用与并发上限由运行时统一控制。',
          )}
        </p>
      </DetailSection>
    </DetailPage>
  );
}
