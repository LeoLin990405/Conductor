import { useState } from 'react';
import { Trash2, FileText, Code2 } from 'lucide-react';
import { clearPreferences } from '@/lib/dashboardApi/api';
import { usePreferences } from '@/hooks/usePreferences';
import { useUpdatePreferences } from '@/hooks/useUpdatePreferences';
import { useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import { useTranslation } from 'react-i18next';
import { useToast } from '@/components/ui/use-toast';
import { ToggleSwitch } from '@/components/ui/switch';
import { readSubagentsDefault, subagentsDefaultPatch } from '@/lib/subagentsDefault';
import ConfirmDialog from '@/components/ConfirmDialog';
import type { Preferences } from './types';
import { AutoApproveSettings } from './AutoApproveSettings';

/**
 * Preferences tab for the local build: only settings that have a concrete
 * effect here: answer format, agent behavior, auto-approve and local data.
 * Onboarding, welcome offers and guide replay are intentionally absent.
 */
export function PreferencesTab() {
  const { toast } = useToast();
  const { preferences: prefsData } = usePreferences();
  const updatePrefsMutation = useUpdatePreferences();
  const queryClient = useQueryClient();
  const { t } = useTranslation();

  const [error, setError] = useState<string | null>(null);
  const [showResetConfirm, setShowResetConfirm] = useState(false);
  const [isResetting, setIsResetting] = useState(false);

  const handleOutputFormatChange = async (format: 'markdown' | 'html') => {
    const currentAgentPref = (prefsData as Preferences | null)?.agent_preference || {};
    const nextOutputFormat = format === 'html' ? 'html' : null;
    try {
      await updatePrefsMutation.mutateAsync({
        agent_preference: {
          ...currentAgentPref,
          output_format: nextOutputFormat,
        },
      });
    } catch {
      toast({
        variant: 'destructive',
        title: t('common.error'),
        description: t('settings.failedToSaveSettings'),
      });
    }
  };

  const subagentsDefault = readSubagentsDefault(prefsData);
  const handleSubagentsDefaultChange = async (next: boolean) => {
    try {
      await updatePrefsMutation.mutateAsync(subagentsDefaultPatch(next));
    } catch {
      toast({
        variant: 'destructive',
        title: t('common.error'),
        description: t('settings.failedToSaveSettings'),
      });
    }
  };

  const handleResetConfirm = async () => {
    setIsResetting(true);
    try {
      await clearPreferences();
      queryClient.invalidateQueries({ queryKey: queryKeys.user.preferences() });
      queryClient.invalidateQueries({ queryKey: queryKeys.user.me() });
      queryClient.invalidateQueries({ queryKey: queryKeys.features.all });
      setShowResetConfirm(false);
    } catch {
      setError(t('settings.failedToResetPreferences'));
      setShowResetConfirm(false);
    } finally {
      setIsResetting(false);
    }
  };

  const outputFormat = (prefsData as Preferences | null)?.agent_preference?.output_format === 'html'
    ? 'html'
    : 'markdown';

  return (
    <>
      <div className="flex flex-col gap-4 max-w-2xl">
        <section>
          <h3 className="text-(length:--size-chrome) font-medium uppercase tracking-wide mb-2" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('settings.preferencesOutputSection', 'Answer format')}
          </h3>
          <div className="p-3 rounded-(--radius-card)" style={{ backgroundColor: 'var(--color-bg-card)', border: '1px solid var(--color-border-muted)' }}>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <label className="text-(length:--size-chrome) font-medium" style={{ color: 'var(--color-text-primary)' }}>
                {t('settings.outputFormat')}
              </label>
              <div className="inline-flex rounded-(--radius-card) overflow-hidden clips-focus-ring" style={{ border: '1px solid var(--color-border-muted)' }}>
                <button
                  type="button"
                  onClick={() => handleOutputFormatChange('markdown')}
                  className="flex items-center gap-1.5 px-2.5 py-1 text-(length:--size-chrome) font-medium transition-colors"
                  style={{
                    backgroundColor: outputFormat === 'markdown' ? 'var(--color-accent-soft)' : 'transparent',
                    color: outputFormat === 'markdown' ? 'var(--color-accent-primary)' : 'var(--color-text-tertiary)',
                  }}
                >
                  <FileText className="h-3.5 w-3.5" />
                  {t('settings.outputFormatDefault')}
                </button>
                <button
                  type="button"
                  onClick={() => handleOutputFormatChange('html')}
                  className="flex items-center gap-1.5 px-2.5 py-1 text-(length:--size-chrome) font-medium transition-colors"
                  style={{
                    backgroundColor: outputFormat === 'html' ? 'var(--color-accent-soft)' : 'transparent',
                    color: outputFormat === 'html' ? 'var(--color-accent-primary)' : 'var(--color-text-tertiary)',
                  }}
                >
                  <Code2 className="h-3.5 w-3.5" />
                  {t('settings.outputFormatHtml')}
                </button>
              </div>
            </div>
            <p className="text-xs mt-2" style={{ color: 'var(--color-text-tertiary)' }}>
              {outputFormat === 'html'
                ? t('settings.outputFormatDescriptionHtml')
                : t('settings.outputFormatDescriptionDefault')}
            </p>
          </div>
        </section>

        <section>
          <h3 className="text-(length:--size-chrome) font-medium uppercase tracking-wide mb-2" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('settings.preferencesAgentSection', 'Agent behavior')}
          </h3>
          <div className="flex flex-col gap-3">
            <div className="p-3 rounded-(--radius-card)" style={{ backgroundColor: 'var(--color-bg-card)', border: '1px solid var(--color-border-muted)' }}>
              <div className="flex items-center justify-between gap-3">
                <label className="text-(length:--size-chrome) font-medium" style={{ color: 'var(--color-text-primary)' }}>
                  {t('settings.subagentsDefault')}
                </label>
                <ToggleSwitch
                  checked={subagentsDefault}
                  onChange={() => { void handleSubagentsDefaultChange(!subagentsDefault); }}
                  ariaLabel={t('settings.subagentsDefault')}
                />
              </div>
              <p className="text-xs mt-2" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('settings.subagentsDefaultDesc')}
              </p>
            </div>
            <AutoApproveSettings />
          </div>
        </section>

        <section>
          <h3 className="text-(length:--size-chrome) font-medium uppercase tracking-wide mb-2" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('settings.preferencesDataSection', 'Local data')}
          </h3>
          <div
            className="rounded-(--radius-control) p-3 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between"
            style={{ backgroundColor: 'var(--color-bg-card)', border: '1px solid var(--color-border-muted)' }}
          >
            <div className="min-w-0">
              <p className="text-(length:--size-chrome) font-medium" style={{ color: 'var(--color-text-primary)' }}>
                {t('settings.resetPreferences')}
              </p>
              <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('settings.resetPreferencesDesc', 'Clear preferences saved on this device and restore defaults.')}
              </p>
            </div>
            <button
              type="button"
              onClick={() => setShowResetConfirm(true)}
              className="flex shrink-0 items-center gap-2 px-3 py-1.5 rounded-(--radius-control) text-sm font-medium transition-colors self-start sm:self-auto"
              style={{ color: 'var(--color-loss)', backgroundColor: 'transparent', border: '1px solid var(--color-loss)' }}
            >
              <Trash2 className="h-4 w-4" /> {t('settings.resetPreferences')}
            </button>
          </div>
        </section>

        {error && (
          <div className="p-3 rounded-(--radius-control)" style={{ backgroundColor: 'var(--color-loss-soft)', border: '1px solid var(--color-border-loss)' }}>
            <p className="text-sm" style={{ color: 'var(--color-loss)' }}>{error}</p>
          </div>
        )}

      </div>

      <ConfirmDialog
        open={showResetConfirm}
        title={t('settings.resetPreferences')}
        message={t('settings.resetConfirmMsg')}
        confirmLabel={isResetting ? t('settings.resetting') : t('settings.resetPreferences')}
        onConfirm={handleResetConfirm}
        onOpenChange={setShowResetConfirm}
      />
    </>
  );
}
