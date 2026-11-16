import { useId, useState } from 'react';
import { Sun, Moon, Monitor } from 'lucide-react';
import { Select } from '@/components/ui/select';
import { ToggleSwitch } from '@/components/ui/switch';
import { SegmentedControl } from '@/components/ui/segmented-control';
import { usePreferences } from '@/hooks/usePreferences';
import { useUpdatePreferences } from '@/hooks/useUpdatePreferences';
import { useTheme } from '@/contexts/ThemeContext';
import { FONT_SCALES, getFontScale, setFontScale, type FontScale } from '@/lib/fontScale';
import { turnEndScrollPatch, readTurnEndScroll, type TurnEndScroll } from '@/lib/turnEndScroll';
import {
  readStreamingMode,
  readTurnDisplay,
  streamingModePatch,
  turnDisplayPatch,
  type StreamingMode,
  type TurnDisplay,
} from '@/lib/transcriptDisplay';
import { useTranslation } from 'react-i18next';
import { useToast } from '@/components/ui/use-toast';
import { useLocale } from '@/hooks/useLocale';
import { applyLocale } from '@/hooks/useLocaleChoice';
import { isSupported } from '@/lib/locale';
import type { Preferences } from './types';

/** Local settings: appearance, language and transcript behavior. The local
 *  build has no account identity to edit, so avatar, email, name and logout are
 *  deliberately absent. */
export function UserInfoTab() {
  const { toast } = useToast();
  const { preferences: prefsData } = usePreferences();
  const updatePrefsMutation = useUpdatePreferences();
  const { preference, setTheme: setThemePref } = useTheme();
  const [fontScale, setFontScaleState] = useState(getFontScale);
  const themeLabelId = useId();
  const fontSizeLabelId = useId();
  const localeLabelId = useId();
  const turnEndLabelId = useId();
  const turnDisplayLabelId = useId();
  const streamingModeLabelId = useId();
  const { t, i18n } = useTranslation();
  const activeLocale = useLocale();

  const locales = [
    { value: 'en-US', label: 'English (United States)' },
    { value: 'zh-CN', label: '中文（简体）' },
  ];

  const handleLocaleChange = (newLocale: string) => {
    if (isSupported(newLocale)) void applyLocale(i18n, newLocale);
  };

  const handleVoiceInputToggle = async () => {
    const currentEnabled = !!((prefsData as Preferences | null)?.other_preference?.voice_input_enabled);
    try {
      await updatePrefsMutation.mutateAsync({
        other_preference: { voice_input_enabled: !currentEnabled },
      });
    } catch {
      toast({
        variant: 'destructive',
        title: t('common.error'),
        description: t('settings.failedToSaveSettings'),
      });
    }
  };

  const turnEndScroll = readTurnEndScroll(prefsData);
  const handleTurnEndScrollChange = async (next: TurnEndScroll) => {
    if (next === turnEndScroll) return;
    try {
      await updatePrefsMutation.mutateAsync(turnEndScrollPatch(next));
    } catch {
      toast({
        variant: 'destructive',
        title: t('common.error'),
        description: t('settings.failedToSaveSettings'),
      });
    }
  };

  const turnDisplay = readTurnDisplay(prefsData);
  const handleTurnDisplayChange = async (next: TurnDisplay) => {
    if (next === turnDisplay) return;
    try {
      await updatePrefsMutation.mutateAsync(turnDisplayPatch(next));
    } catch {
      toast({
        variant: 'destructive',
        title: t('common.error'),
        description: t('settings.failedToSaveSettings'),
      });
    }
  };

  const streamingMode = readStreamingMode(prefsData);
  const handleStreamingModeChange = async (next: StreamingMode) => {
    if (next === streamingMode) return;
    try {
      await updatePrefsMutation.mutateAsync(streamingModePatch(next));
    } catch {
      toast({
        variant: 'destructive',
        title: t('common.error'),
        description: t('settings.failedToSaveSettings'),
      });
    }
  };

  return (
    <div className="settings-rows">
      <div className="settings-row">
        <div className="space-y-0.5">
          <label id={themeLabelId} className="text-(length:--size-chrome) font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('settings.theme')}</label>
        </div>
        <SegmentedControl
          labelledBy={themeLabelId}
          value={preference}
          onChange={setThemePref}
          options={[
            { value: 'dark', label: <><Moon className="h-3.5 w-3.5" />{t('settings.dark')}</> },
            { value: 'light', label: <><Sun className="h-3.5 w-3.5" />{t('settings.light')}</> },
            { value: 'auto', label: <><Monitor className="h-3.5 w-3.5" />{t('settings.auto', 'Auto')}</> },
          ]}
        />
      </div>

      <div className="settings-row">
        <div className="space-y-0.5">
          <label id={fontSizeLabelId} className="text-(length:--size-chrome) font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('settings.fontSize', 'Font size')}</label>
        </div>
        <SegmentedControl
          labelledBy={fontSizeLabelId}
          value={String(fontScale)}
          onChange={(v) => { const next = Number(v) as FontScale; setFontScale(next); setFontScaleState(next); }}
          options={FONT_SCALES.map((scale) => ({ value: String(scale), label: `${Math.round(scale * 100)}%` }))}
        />
      </div>

      <div className="settings-row flex-wrap">
        <div className="flex-1 basis-64">
          <label id={localeLabelId} className="text-(length:--size-chrome) font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('settings.locale')}</label>
        </div>
        <div className="w-56 shrink-0">
          <Select aria-labelledby={localeLabelId} value={activeLocale} onChange={(e) => handleLocaleChange(e.target.value)}>
            {locales.map((item) => (
              <option key={item.value} value={item.value}>{item.label}</option>
            ))}
          </Select>
        </div>
      </div>

      <div className="settings-row">
        <div>
          <label className="text-(length:--size-chrome) font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('settings.voiceInput')}</label>
          <p className="mt-0.5 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>{t('settings.voiceInputDesc')}</p>
        </div>
        <ToggleSwitch
          checked={(prefsData as Preferences | null)?.other_preference?.voice_input_enabled === true}
          onChange={handleVoiceInputToggle}
          ariaLabel={t('settings.voiceInput')}
        />
      </div>

      <div className="settings-row flex-wrap">
        <div className="flex-1 basis-64">
          <label id={turnEndLabelId} className="text-(length:--size-chrome) font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('settings.turnEndScroll')}</label>
          <p className="mt-0.5 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>{t('settings.turnEndScrollDesc')}</p>
        </div>
        <div className="flex shrink-0">
          <SegmentedControl
            labelledBy={turnEndLabelId}
            value={turnEndScroll}
            onChange={(v) => { void handleTurnEndScrollChange(v); }}
            options={[
              { value: 'bottom', label: t('settings.turnEndScrollBottom') },
              { value: 'reply_start', label: t('settings.turnEndScrollReplyStart') },
            ]}
          />
        </div>
      </div>

      <div className="settings-row flex-wrap">
        <div className="flex-1 basis-64">
          <label id={turnDisplayLabelId} className="text-(length:--size-chrome) font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('settings.turnDisplay')}</label>
          <p className="mt-0.5 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>{t('settings.turnDisplayDesc')}</p>
        </div>
        <div className="flex shrink-0">
          <SegmentedControl
            labelledBy={turnDisplayLabelId}
            value={turnDisplay}
            onChange={(v) => { void handleTurnDisplayChange(v); }}
            options={[
              { value: 'lean', label: t('settings.turnDisplayLean') },
              { value: 'verbose', label: t('settings.turnDisplayVerbose') },
            ]}
          />
        </div>
      </div>

      <div className="settings-row flex-wrap">
        <div className="flex-1 basis-64">
          <label id={streamingModeLabelId} className="text-(length:--size-chrome) font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('settings.streamingMode')}</label>
          <p className="mt-0.5 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>{t('settings.streamingModeDesc')}</p>
        </div>
        <div className="flex shrink-0">
          <SegmentedControl
            labelledBy={streamingModeLabelId}
            value={streamingMode}
            onChange={(v) => { void handleStreamingModeChange(v); }}
            options={[
              { value: 'token', label: t('settings.streamingModeToken') },
              { value: 'paragraph', label: t('settings.streamingModeParagraph') },
            ]}
          />
        </div>
      </div>
    </div>
  );
}
