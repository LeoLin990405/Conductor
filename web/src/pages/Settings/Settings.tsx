import { useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { useScrollMemory } from '@/lib/scrollMemory';
import { useUser } from '@/hooks/useUser';
import { usePreferences } from '@/hooks/usePreferences';
import { useTranslation } from 'react-i18next';
import { UserInfoTab } from './panels/UserInfoTab';
import { PreferencesTab } from './panels/PreferencesTab';
import { ModelTab } from './panels/ModelTab';
import { ExperimentsTab } from './panels/ExperimentsTab';
import { NativeRuntimeInventory } from './panels/NativeRuntimeInventory';
import { AboutTab } from './panels/AboutTab';
import { RoleRegistryTab } from './panels/RoleRegistryTab';
import './locales/register';
import './Settings.css';

const TABS = [
  { id: 'userInfo', labelKey: 'settings.localSettings', fallback: '本机设置' },
  { id: 'preferences', labelKey: 'settings.preferences', fallback: '偏好设置' },
  { id: 'model', labelKey: 'settings.model', fallback: '模型' },
  { id: 'native', labelKey: 'settings.nativeCli', fallback: '原生 CLI' },
  { id: 'roles', labelKey: 'settings.roles', fallback: '角色' },
  { id: 'experiments', labelKey: 'settings.experiments', fallback: '实验功能' },
  { id: 'about', labelKey: 'settings.about', fallback: '关于' },
] as const;

type TabId = (typeof TABS)[number]['id'];

function Settings() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { isLoading: isUserLoading } = useUser();
  const { isLoading: isPrefsLoading } = usePreferences();
  const { t } = useTranslation();

  const tabParam = searchParams.get('tab') || 'userInfo';
  const [activeTab, setActiveTab] = useState(tabParam);
  const pageRef = useRef<HTMLDivElement>(null);
  useScrollMemory(pageRef, 'page:settings');
  const isLoading = isUserLoading || isPrefsLoading;

  // Sync tab with URL search params
  const handleTabChange = (tab: string) => {
    setActiveTab(tab);
    setSearchParams({ tab }, { replace: true });
  };

  // Sync from URL on mount / back-forward navigation
  useEffect(() => {
    const urlTab = searchParams.get('tab');
    if (urlTab && urlTab !== activeTab) {
      setActiveTab(urlTab);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  const active = TABS.find((tab) => tab.id === activeTab) ?? TABS[0];

  return (
    <div className="settings-page">
      <div className="chrome-drag-strip" aria-hidden="true" />
      <div ref={pageRef} className="settings-scroll">
        <div className="settings-console">
          <aside className="settings-console-nav" aria-label={t('settings.title')}>
            <header className="settings-nav-brand">
              <span className="settings-eyebrow">{t('tnSettings.eyebrow.app')}</span>
              <h2 className="settings-nav-title">{t('settings.title')}</h2>
              <p className="settings-nav-subtitle">
                {t('settings.subtitle', '管理本机模型与偏好设置')}
              </p>
            </header>
            <nav className="settings-tab-list" aria-label={t('tnSettings.sectionsNav')}>
              {TABS.map((tab) => {
                const selected = tab.id === activeTab;
                return (
                  <button
                    key={tab.id}
                    type="button"
                    onClick={() => handleTabChange(tab.id)}
                    aria-current={selected ? 'page' : undefined}
                    className={`settings-tab ${selected ? 'active' : ''}`}
                  >
                    <span className="settings-tab-label">{t(tab.labelKey, tab.fallback)}</span>
                  </button>
                );
              })}
            </nav>
          </aside>

          <section className="settings-console-main" aria-labelledby="settings-section-title">
            <header className="settings-main-head">
              <span className="settings-eyebrow">
                {t('tnSettings.eyebrow.section', { section: active.id })}
              </span>
              <h1 id="settings-section-title" className="settings-main-title">
                {t(active.labelKey, active.fallback)}
              </h1>
              <p className="settings-main-desc">{t('settings.subtitle', '管理本机模型与偏好设置')}</p>
            </header>

            <div className="settings-content">
              {isLoading && (
                <div className="settings-loading">
                  <span className="terminal-meta">{t('common.loading')}</span>
                </div>
              )}

              {!isLoading && activeTab === 'userInfo' && <UserInfoTab />}
              {!isLoading && activeTab === 'preferences' && <PreferencesTab />}
              {!isLoading && activeTab === 'model' && <ModelTab />}
              {!isLoading && activeTab === 'native' && <NativeRuntimeInventory />}
              {!isLoading && activeTab === 'roles' && <RoleRegistryTab />}
              {!isLoading && activeTab === 'experiments' && <ExperimentsTab />}
              {!isLoading && activeTab === 'about' && <AboutTab />}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}

export type { TabId };
export default Settings;
