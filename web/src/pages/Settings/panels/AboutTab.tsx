import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader } from '@/components/ui/loader';
import {
  applyLocalUpdate,
  checkLocalUpdate,
  getLocalVersion,
  restartLocalUpdate,
  type LocalUpdateApply,
  type LocalUpdateCheck,
  type LocalVersionInfo,
} from '@/api/systemUpdates';

type Busy = 'check' | 'apply' | 'restart' | null;

async function waitForBackend(timeoutMs = 90_000): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    try {
      await getLocalVersion();
      return true;
    } catch {
      // still restarting
    }
  }
  return false;
}

/**
 * About / local update card. The updater only exists for an OSS git checkout;
 * a packaged build reports that it is unsupported rather than showing a dead
 * button.
 */
export function AboutTab() {
  const { t } = useTranslation();
  const [info, setInfo] = useState<LocalVersionInfo | null>(null);
  const [check, setCheck] = useState<LocalUpdateCheck | null>(null);
  const [applied, setApplied] = useState<LocalUpdateApply | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setInfo(await getLocalVersion());
    } catch {
      setInfo({ supported: false, enabled: false, version: '—', reason: t('about.unavailable') });
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleCheck = async () => {
    setBusy('check');
    setError(null);
    try {
      setCheck(await checkLocalUpdate());
    } catch (caught) {
      const detail = (caught as { response?: { data?: { detail?: unknown } } })?.response
        ?.data?.detail;
      setError(typeof detail === 'string' ? detail : t('about.checkFailed'));
    } finally {
      setBusy(null);
    }
  };

  const handleApply = async () => {
    setBusy('apply');
    setError(null);
    try {
      const result = await applyLocalUpdate();
      setApplied(result);
      if (!result.updated) return;
      setBusy('restart');
      try {
        await restartLocalUpdate();
      } catch {
        // The process may restart before the response reaches the page.
      }
      const healthy = await waitForBackend();
      if (healthy) window.location.reload();
      else setError(t('about.restartTimeout'));
    } catch (caught) {
      const detail = (caught as { response?: { data?: { detail?: unknown } } })?.response
        ?.data?.detail;
      setError(typeof detail === 'string' ? detail : t('about.applyFailed'));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-col gap-5 max-w-2xl">
      <section
        className="rounded-(--radius-card) p-4"
        style={{ background: 'var(--color-bg-card)', border: '1px solid var(--color-border-muted)' }}
      >
        <h3 className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
          {t('about.versionTitle')}
        </h3>
        <dl className="mt-3 grid grid-cols-1 gap-2 text-xs sm:grid-cols-2">
          <div>
            <dt style={{ color: 'var(--color-text-tertiary)' }}>{t('about.version')}</dt>
            <dd className="dashboard-mono" style={{ color: 'var(--color-text-primary)' }}>
              {info?.version ?? '—'}
            </dd>
          </div>
          <div>
            <dt style={{ color: 'var(--color-text-tertiary)' }}>{t('about.commit')}</dt>
            <dd className="dashboard-mono" style={{ color: 'var(--color-text-primary)' }}>
              {info?.commit ?? '—'}
            </dd>
          </div>
          <div>
            <dt style={{ color: 'var(--color-text-tertiary)' }}>{t('about.branch')}</dt>
            <dd className="dashboard-mono" style={{ color: 'var(--color-text-primary)' }}>
              {info?.branch ?? '—'}
            </dd>
          </div>
          <div>
            <dt style={{ color: 'var(--color-text-tertiary)' }}>{t('about.state')}</dt>
            <dd style={{ color: info?.dirty ? 'var(--color-warning)' : 'var(--color-text-primary)' }}>
              {info?.dirty ? t('about.dirty') : info?.supported ? t('about.clean') : '—'}
            </dd>
          </div>
        </dl>
        {info && !info.supported && (
          <p className="mt-3 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('about.unsupported')}
          </p>
        )}
      </section>

      <section
        className="rounded-(--radius-card) p-4"
        style={{ background: 'var(--color-bg-card)', border: '1px solid var(--color-border-muted)' }}
      >
        <h3 className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
          {t('about.updateTitle')}
        </h3>
        <p className="mt-1 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
          {t('about.updateDesc')}
        </p>

        {!check && !applied && (
          <button
            type="button"
            onClick={handleCheck}
            disabled={!info?.supported || !info?.enabled || !!info?.dirty || busy !== null}
            className="mt-3 inline-flex items-center gap-2 rounded-(--radius-control) px-3 py-1.5 text-xs font-medium disabled:opacity-50"
            style={{ background: 'var(--color-btn-primary-bg)', color: 'var(--color-btn-primary-text)' }}
          >
            {busy === 'check' && <Loader size={12} />}
            {t('about.check')}
          </button>
        )}

        {check && !check.update_available && (
          <p className="mt-3 text-xs" style={{ color: 'var(--color-success)' }}>
            {t('about.upToDate', { commit: check.current })}
          </p>
        )}

        {check?.update_available && (
          <div className="mt-3">
            <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
              {t('about.available', { count: check.behind, remote: check.remote })}
            </p>
            {check.commits.length > 0 && (
              <ul className="mt-2 max-h-40 overflow-y-auto rounded-(--radius-control) p-2 text-(length:--size-chrome) dashboard-mono" style={{ background: 'var(--color-bg-surface)' }}>
                {check.commits.map((line) => (
                  <li key={line} style={{ color: 'var(--color-text-tertiary)' }}>{line}</li>
                ))}
              </ul>
            )}
            <button
              type="button"
              onClick={handleApply}
              disabled={busy !== null}
              className="mt-3 inline-flex items-center gap-2 rounded-(--radius-control) px-3 py-1.5 text-xs font-medium disabled:opacity-50"
              style={{ background: 'var(--color-btn-primary-bg)', color: 'var(--color-btn-primary-text)' }}
            >
              {busy !== null && <Loader size={12} />}
              {busy === 'restart' ? t('about.restarting') : t('about.apply')}
            </button>
          </div>
        )}

        {applied && !error && (
          <p className="mt-3 text-xs" style={{ color: 'var(--color-success)' }}>
            {applied.updated
              ? t('about.applied', { from: applied.old_commit, to: applied.new_commit })
              : applied.message}
          </p>
        )}
        {error && (
          <p className="mt-3 whitespace-pre-wrap text-xs" style={{ color: 'var(--color-loss)' }}>
            {error}
          </p>
        )}
      </section>
    </div>
  );
}
