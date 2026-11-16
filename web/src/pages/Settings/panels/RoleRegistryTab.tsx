import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  ROLE_NAMES,
  getRoleAudit,
  getRoleRegistry,
  updateRole,
  type RoleAuditRow,
  type RoleName,
  type RoleRegistryView,
} from '@/api/roleRegistry';

interface Draft {
  candidates: string;
  default: string;
  reason: string;
}

function draftsFrom(view: RoleRegistryView): Record<RoleName, Draft> {
  const out = {} as Record<RoleName, Draft>;
  for (const role of ROLE_NAMES) {
    const entry = view.roles[role];
    out[role] = {
      candidates: (entry?.candidates ?? []).join(', '),
      default: entry?.default ?? '',
      reason: '',
    };
  }
  return out;
}

/**
 * The role registry (R6 L1): who serves as Router, planner, reviewer and
 * worker. Every change needs a reason and is audited by the server, which
 * also refuses any change that would break a hard rule.
 */
export function RoleRegistryTab() {
  const { t } = useTranslation();
  const [view, setView] = useState<RoleRegistryView | null>(null);
  const [drafts, setDrafts] = useState<Record<RoleName, Draft> | null>(null);
  const [audit, setAudit] = useState<RoleAuditRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<RoleName | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await getRoleRegistry();
      setView(next);
      setDrafts(draftsFrom(next));
      setAudit(await getRoleAudit());
      setError(null);
    } catch {
      setError(t('settings.roleRegistry.loadFailed'));
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async (role: RoleName) => {
    if (!drafts || !view) return;
    const draft = drafts[role];
    if (!draft.reason.trim()) {
      setError(t('settings.roleRegistry.reasonRequired'));
      return;
    }
    setSaving(role);
    try {
      const next = await updateRole(role, {
        candidates: draft.candidates
          .split(',')
          .map((name) => name.trim())
          .filter(Boolean),
        default: draft.default.trim() || undefined,
        reason: draft.reason.trim(),
        expected_revision: view.revision,
      });
      setView(next);
      setDrafts(draftsFrom(next));
      setAudit(await getRoleAudit());
      setError(null);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : String(caught);
      setError(`${t('settings.roleRegistry.saveFailed')}: ${message}`);
    } finally {
      setSaving(null);
    }
  };

  const edit = (role: RoleName, field: keyof Draft, value: string) => {
    setDrafts((current) =>
      current ? { ...current, [role]: { ...current[role], [field]: value } } : current,
    );
  };

  if (!view || !drafts) {
    return (
      <section className="settings-section">
        <h3>{t('settings.roleRegistry.title')}</h3>
        {error ? <p role="alert">{error}</p> : <p>{t('settings.roleRegistry.loading')}</p>}
      </section>
    );
  }

  return (
    <section className="settings-section">
      <h3>{t('settings.roleRegistry.title')}</h3>
      <p>
        {view.source === 'store'
          ? t('settings.roleRegistry.sourceStore', { revision: view.revision })
          : t('settings.roleRegistry.sourceFallback')}
      </p>
      {error && <p role="alert">{error}</p>}
      {ROLE_NAMES.map((role) => (
        <div key={role} className="settings-row" data-role={role}>
          <h4>{t(`settings.roleRegistry.roles.${role}`)}</h4>
          <label>
            {t('settings.roleRegistry.candidates')}
            <input
              aria-label={`${role} candidates`}
              value={drafts[role].candidates}
              onChange={(event) => edit(role, 'candidates', event.target.value)}
            />
          </label>
          <label>
            {t('settings.roleRegistry.default')}
            <input
              aria-label={`${role} default`}
              value={drafts[role].default}
              onChange={(event) => edit(role, 'default', event.target.value)}
            />
          </label>
          <label>
            {t('settings.roleRegistry.reason')}
            <input
              aria-label={`${role} reason`}
              value={drafts[role].reason}
              onChange={(event) => edit(role, 'reason', event.target.value)}
            />
          </label>
          <button type="button" disabled={saving !== null} onClick={() => void save(role)}>
            {t('settings.roleRegistry.save')}
          </button>
        </div>
      ))}
      <h4>{t('settings.roleRegistry.hardRules')}</h4>
      <ul>
        {view.hard_rules.map((rule) => (
          <li key={rule}>{rule}</li>
        ))}
      </ul>
      <h4>{t('settings.roleRegistry.audit')}</h4>
      {audit.length === 0 ? (
        <p>{t('settings.roleRegistry.auditEmpty')}</p>
      ) : (
        <ul>
          {audit.map((row) => (
            <li key={row.audit_id}>
              r{row.revision} · {row.role} · {row.actor} · {row.reason}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
