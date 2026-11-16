import { useTranslation } from 'react-i18next';
import { List, RefreshCw } from 'lucide-react';
import { Loader } from '@/components/ui/loader';
import { useBuiltinMcpServerTools, useMcpCatalogServerTools } from '@/hooks/useMcpServers';
import { useLocale } from '@/hooks/useLocale';
import { cn } from '@/lib/utils';
import { mediumDate } from '@/lib/format';
import type { BuiltinMcpServer, CatalogServer, McpToolSummary } from '@/api/core';
import { DetailSection } from '../DetailPage';
import { ToolAccessSwitches, ToolBindingControl } from '../ToolAccess';
import { ToolList } from '../ToolList';
import { BULK, SERVER, type BindingWrites, type WriteScope } from './useBindingWrites';

/**
 * A connector's tools, with a control per tool on a row the user configures.
 * The tools come after the consent groups and row-wide switches, which
 * decide which of them are reachable.
 */
export function ServerToolsSection({
  catalog,
  builtin,
  signInFirst,
  writes,
  onRefresh,
  refreshing = false,
}: {
  catalog: CatalogServer | null;
  builtin: BuiltinMcpServer | null;
  /** The switch waits for a sign-in, which is also what lists the tools. */
  signInFirst: boolean;
  writes: BindingWrites;
  onRefresh?: () => void;
  refreshing?: boolean;
}) {
  const { t } = useTranslation();
  const locale = useLocale();
  // Two sources for the same section, because a server's tools are discovered
  // by whoever owns the server: the user's rows carry the snapshot taken when
  // they added or refreshed one, and a builtin's schemas are what this process
  // froze at startup. Only one of the two ever runs.
  const catalogTools = useMcpCatalogServerTools(catalog?.name ?? null);
  const builtinTools = useBuiltinMcpServerTools(builtin?.name ?? null);
  const toolsQuery = builtin ? builtinTools : catalogTools;

  const bindingControl = catalog
    ? (tool: McpToolSummary) => {
        const scope: WriteScope = { kind: 'tool', name: tool.name };
        return (
          <ToolBindingControl
            tool={tool}
            // A row-wide or bulk write is rewriting the value this control is
            // drawing, so it goes inert for those too.
            busy={writes.busy(BULK) || writes.busy(SERVER) || writes.busy(scope)}
            error={writes.refusalOn(scope)}
            onPatch={(body) => writes.write(scope, body)}
          />
        );
      }
    : undefined;

  const serverRefusal = writes.refusalOn(SERVER);

  return (
    <>
      {(catalog || builtin) && (
        <DetailSection
          title={t('plugins.detail.tools')}
          count={toolsQuery.data?.tools.length || undefined}
          action={
            onRefresh && (
              <button
                type="button"
                className="surface-section-link"
                onClick={onRefresh}
                disabled={refreshing}
                aria-busy={refreshing}
              >
                <RefreshCw className={cn('h-3.5 w-3.5', refreshing && 'animate-spin')} aria-hidden />
                {t('plugins.oauth.refreshSchemas')}
              </button>
            )
          }
        >
          {toolsQuery.isLoading ? (
            <div className="flex items-center gap-2 py-3">
              <Loader size={14} className="text-current" />
              <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('common.loading')}
              </span>
            </div>
          ) : toolsQuery.isError ? (
            // Distinct from the empty case below: a failed request wants a
            // retry, where "no tools yet" tells the user to wait.
            <p className="text-xs" style={{ color: 'var(--color-loss)' }}>
              {t('plugins.detail.toolsFailed')}
            </p>
          ) : builtinTools.data?.connected === false ? (
            // This worker has no snapshot, because its startup connect failed
            // and a frozen registry is never repaired. "No tools" here would
            // report one process's gap as the server's shape.
            <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('plugins.detail.toolsUnavailable')}
            </p>
          ) : !toolsQuery.data || toolsQuery.data.tools.length === 0 ? (
            <div className="connector-empty-box">
              <List className="h-3.5 w-3.5" aria-hidden />
              {signInFirst ? t('plugins.connectorsPage.toolsAfterSignIn') : t('plugins.detail.toolsEmpty')}
            </div>
          ) : (
            <ToolList
              tools={toolsQuery.data.tools}
              renderControl={bindingControl}
              onBulkPatch={catalog ? (body) => writes.write(BULK, body) : undefined}
              bulkBusy={writes.busy(BULK)}
              bulkError={writes.refusalOn(BULK)}
            >
              {toolsQuery.data.discovered_at && (
                <span className="text-[0.6875rem]" style={{ color: 'var(--color-text-quaternary)' }}>
                  {t('plugins.detail.discovered', {
                    date: mediumDate(new Date(toolsQuery.data.discovered_at), locale),
                  })}
                </span>
              )}
            </ToolList>
          )}
        </DetailSection>
      )}
    </>
  );
}
