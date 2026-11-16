import { Trans, useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { PRIVACY_URL } from '../../config/supportLinks';
import { registerFeatureLocales } from '@/lib/featureLocales';
import tnLegalEn from './locales/tnLegal.en-US.json';
import tnLegalZh from './locales/tnLegal.zh-CN.json';
import './legal.css';

registerFeatureLocales({ 'en-US': tnLegalEn, 'zh-CN': tnLegalZh });

export const REPOSITORY_URL = 'https://github.com/LeoLin990405/Conductor';

/**
 * /legal — public (mounted outside the authenticated shell). Terminal-native:
 * mono chrome for the back link and headings, the proportional prose face for
 * body copy, token surfaces, and links that carry the 2px ink.focus ring.
 */
function Legal() {
  const { t } = useTranslation();
  return (
    <div className="tn-legal" data-testid="legal-page">
      <article className="tn-legal-article">
        <header className="tn-legal-header">
          <Link to="/" className="tn-legal-back">
            <span aria-hidden="true">← </span>
            {t('tnLegal.back')}
          </Link>
          <h1 className="tn-legal-title">{t('tnLegal.title')}</h1>
          <p className="tn-legal-intro">{t('tnLegal.intro')}</p>
        </header>

        <section className="tn-legal-section" aria-labelledby="tn-legal-credits">
          <h2 id="tn-legal-credits" className="tn-legal-heading">
            {t('tnLegal.creditsHeading')}
          </h2>
          <p className="tn-legal-prose">
            <Trans
              i18nKey="tnLegal.creditsBody"
              components={{
                repo: (
                  <a
                    href={REPOSITORY_URL}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="tn-legal-link"
                  />
                ),
              }}
            />
          </p>
          {PRIVACY_URL && (
            <p className="tn-legal-prose">
              <Trans
                i18nKey="tnLegal.privacyBody"
                components={{
                  privacy: <a href={PRIVACY_URL} className="tn-legal-link" />,
                }}
              />
            </p>
          )}
        </section>
      </article>
    </div>
  );
}

export default Legal;
