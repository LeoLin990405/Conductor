import { WifiOff } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useNetworkStatus } from '@/hooks/useNetworkStatus';
import '../AppShell/shellLocales';
import './NetworkBanner.css';

/**
 * App-wide banner surfacing the browser's offline state.
 *
 * Mounted once in `Main`, because losing the link is not a dashboard concern:
 * a chat turn that stalls mid-stream is the case where the user most needs to
 * know it is their wifi and not the agent.
 *
 * In flow rather than fixed: `.main` is a flex column whose route child is
 * `height:100%`, and flex shrinks that child by the banner's height instead of
 * letting it overflow. Fixed positioning would land the bar on top of the page
 * header and the rail's brand.
 */
export default function NetworkBanner() {
  const { t } = useTranslation();
  const { online } = useNetworkStatus();
  if (online) return null;

  return (
    <div role="status" aria-live="polite" className="network-banner">
      <WifiOff size={14} strokeWidth={1.8} aria-hidden="true" />
      <span>{t('tnShell.network.offline')}</span>
    </div>
  );
}
