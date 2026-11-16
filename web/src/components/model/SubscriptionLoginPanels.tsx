/**
 * The two waiting panels a subscription sign-in can show, shared by Settings →
 * Connected Accounts and the Setup wizard so neither surface restates the
 * device-code or redirect copy. Both render inside the caller's container: the
 * Settings card and the Setup step keep their own chrome.
 */
import { useCallback, useState, type ReactNode } from 'react';
import { Check, ClipboardCopy, ExternalLink } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Loader } from '@/components/ui/loader';
import type { SubscriptionDeviceCode } from './useSubscriptionLogin';

function CopyCodeButton({ value }: { value: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard blocked (permissions, insecure context) — the code is
      // select-all, so it can still be copied by hand.
    }
  }, [value]);

  return (
    <button
      type="button"
      onClick={handleCopy}
      aria-label={t('common.copy')}
      title={t('common.copy')}
      className="p-1.5 rounded-md transition-colors hover:opacity-80"
      style={{
        backgroundColor: 'var(--color-bg-elevated)',
        border: '1px solid var(--color-border-muted)',
      }}
    >
      {copied ? (
        <Check className="h-3.5 w-3.5" style={{ color: 'var(--color-success)' }} />
      ) : (
        <ClipboardCopy className="h-3.5 w-3.5" style={{ color: 'var(--color-text-tertiary)' }} />
      )}
    </button>
  );
}

function WaitingLine({ children }: { children: ReactNode }) {
  return (
    <span
      role="status"
      className="inline-flex items-center gap-1.5 text-xs"
      style={{ color: 'var(--color-text-tertiary)' }}
    >
      <span aria-hidden="true" className="shrink-0">
        <Loader size={12} className="text-current" />
      </span>
      {children}
    </span>
  );
}

function CancelButton({ onClick }: { onClick: () => void }) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={onClick}
      className="px-3 py-1.5 rounded-md text-xs font-medium self-start"
      style={{ color: 'var(--color-text-tertiary)', backgroundColor: 'transparent' }}
    >
      {t('common.cancel')}
    </button>
  );
}

function FlowError({ message }: { message: string }) {
  return (
    <p role="alert" className="text-xs" style={{ color: 'var(--color-loss)' }}>
      {message}
    </p>
  );
}

/** Where to approve a device code, the code itself, the wait and the way out. */
export function DeviceCodePrompt({
  deviceCode,
  polling,
  error,
  onCancel,
}: {
  deviceCode: SubscriptionDeviceCode;
  polling: boolean;
  error: string | null;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
        {t('subscription.deviceVisit')}{' '}
        <a
          href={deviceCode.verification_url}
          target="_blank"
          rel="noopener noreferrer"
          className="underline break-all"
          style={{ color: 'var(--color-accent-primary)' }}
        >
          {deviceCode.verification_url}
        </a>{' '}
        {t('subscription.deviceEnterCode')}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <code
          className="text-lg font-mono font-bold tracking-widest px-3 py-1.5 rounded-md select-all break-all"
          style={{
            backgroundColor: 'var(--color-bg-elevated)',
            border: '1px solid var(--color-border-muted)',
            color: 'var(--color-text-primary)',
            letterSpacing: '0.15em',
          }}
        >
          {deviceCode.user_code}
        </code>
        <CopyCodeButton value={deviceCode.user_code} />
        {polling && <WaitingLine>{t('subscription.waitingApproval')}</WaitingLine>}
      </div>
      <CancelButton onClick={onCancel} />
      {error && <FlowError message={error} />}
    </div>
  );
}

/** Waiting for a browser sign-in to come back, with the page and a way out. */
export function RedirectLoginPrompt({
  authorizeUrl,
  polling,
  error,
  onCancel,
}: {
  authorizeUrl: string | null;
  polling: boolean;
  error: string | null;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-2">
      {polling && <WaitingLine>{t('subscription.redirectWaiting')}</WaitingLine>}
      {authorizeUrl && (
        <a
          href={authorizeUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1.5 text-xs self-start underline underline-offset-2 hover:opacity-80"
          style={{ color: 'var(--color-text-primary)' }}
        >
          {t('subscription.openLoginAgain')}
          <ExternalLink className="h-3 w-3" />
        </a>
      )}
      <CancelButton onClick={onCancel} />
      {error && <FlowError message={error} />}
    </div>
  );
}
