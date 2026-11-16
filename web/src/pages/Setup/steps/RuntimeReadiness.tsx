/**
 * Runtime readiness inside the setup wizard's last screen.
 *
 * The server's `ready` is a statement about required configuration and local
 * dependency checks only — the top-level verification is `local_checks_only`,
 * so nothing here may ever claim that a model call or sandbox round trip was
 * verified. Observed checks, checks that only found configuration/credentials
 * present, and checks that did not run are rendered as three distinct states.
 *
 * The overall status comes from the server; this section never computes it from
 * the individual checks. That is what lets an optional, unobserved
 * `sandbox_session` stay neutral instead of turning the section red.
 */
import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { useLocale } from '@/hooks/useLocale';
import { formatTimestamp } from '@/lib/format';
import type {
  ReadinessCheck,
  ReadinessCheckStatus,
  ReadinessStatus,
  ReadinessVerification,
} from '@/pages/Setup/utils/api';
import { useReadiness } from '@/hooks/useReadiness';

interface ChipMeta {
  labelKey: string;
  /** A one-line explanation rendered under the row (where one exists). */
  descriptionKey?: string;
  color: string;
  background: string;
  border?: string;
}

/** Overall verdict wording + its hint. Color annotates, the text explains. */
const OVERALL_META: Record<ReadinessStatus, ChipMeta> = {
  ready: {
    labelKey: 'setup.readiness.overall.ready',
    descriptionKey: 'setup.readiness.overallHint.ready',
    color: 'var(--color-success)',
    background: 'var(--color-success-soft)',
  },
  degraded: {
    labelKey: 'setup.readiness.overall.degraded',
    descriptionKey: 'setup.readiness.overallHint.degraded',
    color: 'var(--color-warning)',
    background: 'var(--color-warning-soft)',
  },
  needs_action: {
    labelKey: 'setup.readiness.overall.needs_action',
    descriptionKey: 'setup.readiness.overallHint.needs_action',
    color: 'var(--color-icon-danger)',
    background: 'var(--color-danger-soft)',
  },
};

/**
 * One chip per check status. `missing`, `degraded` and `unknown` each carry
 * their own explanation: the summary text is server copy, never a cause this
 * client infers.
 */
const CHECK_STATUS_META: Record<ReadinessCheckStatus, ChipMeta> = {
  ok: {
    labelKey: 'setup.readiness.checkStatus.ok',
    color: 'var(--color-success)',
    background: 'var(--color-success-soft)',
  },
  missing: {
    labelKey: 'setup.readiness.checkStatus.missing',
    descriptionKey: 'setup.readiness.checkStatusHint.missing',
    color: 'var(--color-icon-danger)',
    background: 'var(--color-danger-soft)',
  },
  degraded: {
    labelKey: 'setup.readiness.checkStatus.degraded',
    descriptionKey: 'setup.readiness.checkStatusHint.degraded',
    color: 'var(--color-warning)',
    background: 'var(--color-warning-soft)',
  },
  unknown: {
    labelKey: 'setup.readiness.checkStatus.unknown',
    descriptionKey: 'setup.readiness.checkStatusHint.unknown',
    color: 'var(--color-text-secondary)',
    background: 'var(--color-bg-subtle)',
    border: 'var(--color-border-subtle)',
  },
};

/** What the check established, never a stronger claim than the server made. */
const VERIFICATION_META: Record<ReadinessVerification, ChipMeta> = {
  observed: {
    labelKey: 'setup.readiness.verification.observed',
    descriptionKey: 'setup.readiness.verificationHint.observed',
    color: 'var(--color-text-secondary)',
    background: 'var(--color-bg-subtle)',
    border: 'var(--color-border-subtle)',
  },
  configuration_only: {
    labelKey: 'setup.readiness.verification.configuration_only',
    descriptionKey: 'setup.readiness.verificationHint.configuration_only',
    color: 'var(--color-text-secondary)',
    background: 'var(--color-bg-subtle)',
    border: 'var(--color-border-subtle)',
  },
  not_checked: {
    labelKey: 'setup.readiness.verification.not_checked',
    descriptionKey: 'setup.readiness.verificationHint.not_checked',
    color: 'var(--color-text-tertiary)',
    background: 'var(--color-bg-subtle)',
    border: 'var(--color-border-subtle)',
  },
};

/** Literal keys, so the locale-catalog scan sees every one of them. */
const CHECK_LABELS: Record<string, { labelKey: string }> = {
  database: { labelKey: 'setup.readiness.check.database' },
  checkpointer: { labelKey: 'setup.readiness.check.checkpointer' },
  redis: { labelKey: 'setup.readiness.check.redis' },
  mcp: { labelKey: 'setup.readiness.check.mcp' },
  sandbox: { labelKey: 'setup.readiness.check.sandbox' },
  sandbox_session: { labelKey: 'setup.readiness.check.sandbox_session' },
  primary_model: { labelKey: 'setup.readiness.check.primary_model' },
  other: { labelKey: 'setup.readiness.check.other' },
};

const NEUTRAL_CHIP: Pick<ChipMeta, 'color' | 'background' | 'border'> = {
  color: 'var(--color-text-secondary)',
  background: 'var(--color-bg-subtle)',
  border: 'var(--color-border-subtle)',
};

const SECONDARY_BUTTON_CLASS =
  'inline-flex items-center rounded-md border px-3 py-1.5 text-sm font-medium transition-colors '
  + 'disabled:cursor-not-allowed disabled:opacity-60';
const SECONDARY_BUTTON_STYLE = {
  borderColor: 'var(--color-border-default)',
  color: 'var(--color-text-primary)',
  backgroundColor: 'var(--color-bg-subtle)',
} as const;

// ---------------------------------------------------------------------------
// RuntimeReadiness
// ---------------------------------------------------------------------------

export function RuntimeReadiness() {
  const { t } = useTranslation();
  const locale = useLocale();
  const headingId = useId();
  const { readiness, isLoading, isError, refetch, isFetching } = useReadiness();

  const checkedAt = readiness?.checked_at;
  const checkedAtDisplay = !checkedAt || Number.isNaN(Date.parse(checkedAt))
    ? t('setup.readiness.unknownValue')
    : formatTimestamp(checkedAt, locale);
  // The server owns the verdict; this only maps an unexpected one to the
  // mildest wording rather than inventing a state.
  const overallMeta: ChipMeta = readiness ? OVERALL_META[readiness.status] ?? OVERALL_META.degraded : OVERALL_META.degraded;

  return (
    <section
      aria-labelledby={headingId}
      className="mt-6 w-full min-w-0 rounded-lg p-4 sm:p-5"
      style={{
        background: 'var(--color-bg-subtle)',
        border: '1px solid var(--color-border-subtle)',
      }}
    >
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
        <div className="min-w-0">
          <h2
            id={headingId}
            className="title-font text-lg font-semibold leading-tight"
            style={{ color: 'var(--color-text-primary)' }}
          >
            {t('setup.readiness.title')}
          </h2>
          <p className="mt-1 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
            {t('setup.readiness.intro')}
          </p>
        </div>
        <button
          type="button"
          onClick={() => void refetch()}
          disabled={isFetching}
          className={SECONDARY_BUTTON_CLASS}
          style={SECONDARY_BUTTON_STYLE}
        >
          {isFetching ? t('setup.readiness.refreshing') : t('setup.readiness.refresh')}
        </button>
      </div>

      {/* Always shown: it is the scope of the whole section, not a result. */}
      <p className="mt-3 text-xs leading-relaxed" style={{ color: 'var(--color-text-tertiary)' }}>
        {t('setup.readiness.verificationNote')}
      </p>

      {isLoading ? (
        <div role="status" aria-live="polite" className="mt-4">
          <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
            {t('setup.readiness.loading')}
          </p>
          <div aria-hidden="true" data-testid="readiness-skeleton" className="mt-3 flex flex-col gap-2">
            {[0, 1, 2].map((i) => (
              <div
                key={i}
                className="h-3 rounded"
                style={{ background: 'var(--color-bg-hover)', width: `${[70, 88, 55][i]}%` }}
              />
            ))}
          </div>
        </div>
      ) : isError ? (
        <div
          role="status"
          aria-live="polite"
          className="mt-4 rounded-md p-3"
          style={{ background: 'var(--color-bg-subtle)', border: '1px solid var(--color-border-default)' }}
        >
          <p className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {t('setup.readiness.errorTitle')}
          </p>
          <p className="mt-1 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
            {t('setup.readiness.errorDescription')}
          </p>
          <button
            type="button"
            onClick={() => void refetch()}
            className={`mt-3 ${SECONDARY_BUTTON_CLASS}`}
            style={SECONDARY_BUTTON_STYLE}
          >
            {t('setup.readiness.retry')}
          </button>
        </div>
      ) : readiness ? (
        <div className="mt-4">
          <div
            role="status"
            aria-live="polite"
            className="rounded-md px-3 py-2.5"
            style={{ background: 'var(--color-bg-subtle)', border: '1px solid var(--color-border-default)' }}
          >
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
              <span className="text-xs font-medium uppercase tracking-wide" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('setup.readiness.statusLabel')}
              </span>
              <Chip
                label={t(overallMeta.labelKey)}
                color={overallMeta.color}
                background={overallMeta.background}
              />
            </div>
            <p className="mt-2 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
              {t(overallMeta.descriptionKey ?? 'setup.readiness.overallHint.degraded')}
            </p>
          </div>

          <div className="mt-3">
            <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
              {t('setup.readiness.checkedAt', { time: checkedAtDisplay })}
            </p>
            <p className="mt-0.5 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('setup.readiness.checkedAtHint')}
            </p>
          </div>

          <ul className="mt-4 flex flex-col gap-2.5">
            {readiness.checks.map((check, index) => (
              <CheckRow key={`${check.id}-${index}`} check={check} />
            ))}
          </ul>

          <p className="mt-4 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('setup.readiness.keysNote')}
          </p>
        </div>
      ) : null}
    </section>
  );
}

// ---------------------------------------------------------------------------
// CheckRow
// ---------------------------------------------------------------------------

function CheckRow({ check }: { check: ReadinessCheck }) {
  const { t } = useTranslation();
  const label = CHECK_LABELS[check.id] ?? CHECK_LABELS.other;
  const statusMeta = CHECK_STATUS_META[check.status] ?? CHECK_STATUS_META.unknown;
  const verificationMeta = VERIFICATION_META[check.verification] ?? VERIFICATION_META.not_checked;
  const summary = typeof check.summary === 'string' && check.summary.trim()
    ? check.summary
    : t('setup.readiness.unknownSummary');
  // An optional check the server could not observe is a note about this
  // worker, not a failure: the server decided the overall status.
  const showSandboxSessionNote = check.id === 'sandbox_session'
    && check.required === false
    && check.status === 'unknown';
  const showModelSettingsLink = check.id === 'primary_model' && check.status === 'missing';

  return (
    <li
      data-testid={`readiness-check-${check.id}`}
      className="min-w-0 rounded-md px-3 py-2.5"
      style={{ background: 'var(--color-bg-subtle)', border: '1px solid var(--color-border-subtle)' }}
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span
          className="min-w-0 break-words text-sm font-medium"
          style={{ color: 'var(--color-text-primary)' }}
        >
          {t(label.labelKey)}
        </span>
        <Chip label={t(statusMeta.labelKey)} color={statusMeta.color} background={statusMeta.background} border={statusMeta.border} />
        <Chip
          label={t(check.required ? 'setup.readiness.required' : 'setup.readiness.optional')}
          color={check.required ? NEUTRAL_CHIP.color : 'var(--color-text-tertiary)'}
          background={NEUTRAL_CHIP.background}
          border={NEUTRAL_CHIP.border}
        />
        <Chip
          label={t(verificationMeta.labelKey)}
          color={verificationMeta.color}
          background={verificationMeta.background}
          border={verificationMeta.border}
        />
      </div>

      <p className="mt-1.5 break-words text-sm" style={{ color: 'var(--color-text-secondary)' }}>
        {summary}
      </p>

      {statusMeta.descriptionKey && (
        <p className="mt-1 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
          {t(statusMeta.descriptionKey)}
        </p>
      )}
      <p className="mt-1 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
        {t(verificationMeta.descriptionKey ?? 'setup.readiness.verificationHint.not_checked')}
      </p>

      {showSandboxSessionNote && (
        <p className="mt-1 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
          {t('setup.readiness.sandboxSessionNote')}
        </p>
      )}

      {showModelSettingsLink && (
        <Link
          to="/settings?tab=model"
          className="mt-2 inline-flex break-words text-sm font-medium underline underline-offset-4"
          style={{ color: 'var(--color-text-primary)' }}
        >
          {t('setup.readiness.modelSettings')}
        </Link>
      )}
    </li>
  );
}

// ---------------------------------------------------------------------------
// Chip
// ---------------------------------------------------------------------------

function Chip({
  label,
  color,
  background,
  border,
}: {
  label: string;
  color: string;
  background: string;
  border?: string;
}) {
  return (
    <span
      className="inline-flex items-center whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium"
      style={{ color, backgroundColor: background, border: border ? `1px solid ${border}` : undefined }}
    >
      {label}
    </span>
  );
}
