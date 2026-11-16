import { useTranslation } from 'react-i18next';
import '../AppShell/shellLocales';
import './PageLoading.css';

interface PageLoadingProps {
  /**
   * `screen` fills the viewport on the page background — for the auth gates and
   * top-level route Suspense. `pane` fills its parent with a transparent
   * background — for Suspense fallbacks inside the app shell.
   */
  variant?: 'screen' | 'pane';
}

/**
 * The terminal warming up. Deliberately quiet: a mark, a single progress
 * hairline, and the one label. A route fallback should never compete with the
 * page it is replacing, and the pane variant must fill the routed area rather
 * than draw its own viewport box inside it.
 */
function PageLoading({ variant = 'screen' }: PageLoadingProps) {
  const { t } = useTranslation();
  return (
    <div
      className={`page-loading${variant === 'pane' ? ' page-loading--pane' : ''}`}
      role="status"
      aria-live="polite"
    >
      <div className="page-loading__center">
        <div className="page-loading__brand" aria-hidden="true">
          <span className="page-loading__mark">▍</span>
          <span className="page-loading__wordmark">conductor</span>
          <span className="page-loading__slash">/</span>
          <span className="page-loading__desk">terminal</span>
        </div>
        <div className="page-loading__track" aria-hidden="true">
          <span className="page-loading__bar" />
        </div>
        <p className="page-loading__label">{t('tnShell.loading')}</p>
      </div>
    </div>
  );
}

export default PageLoading;
