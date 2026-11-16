import { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from 'react';
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useSearchParams } from 'react-router';
import { useAllWorkspaces } from '@/hooks/useAllWorkspaces';
import { useNow } from '@/hooks/useNow';
import { useNativeAgents } from '@/hooks/useNativeAgents';
import { nativeRuntimeLabel } from '@/lib/nativeRuntimeLabels';
import { queryKeys } from '@/lib/queryKeys';
import { registerFeatureLocales } from '@/lib/featureLocales';
import { getComposerMode, subscribeComposerMode } from '@/features/palette/composerMode';
import { openPalette } from '@/features/palette/PaletteHost';
import { useTranslation } from 'react-i18next';
import tnInboxEn from './locales/tnInbox.en-US.json';
import tnInboxZh from './locales/tnInbox.zh-CN.json';
import {
  createManualTask,
  getAutonomyTicket,
  listAutonomySources,
  listAutonomyDaemons,
  listAutonomyTickets,
  type AutonomyTicket,
} from '@/api/autonomy';
import './superagent.css';

// The inbox keeps its copy in its own tnInbox pair beside the main catalogs;
// registering here (module load) means every consumer resolves t('tnInbox.…').
registerFeatureLocales({ 'en-US': tnInboxEn, 'zh-CN': tnInboxZh });

/**
 * Terminal-native state grammar (tokens.json `status`): one glyph, one word and
 * one token ink per state. The glyph is a fixed design token; the word is
 * localized copy from tnInbox.
 */
const STATE_TOKENS = {
  queued: { glyph: '○', ink: 'muted' },
  running: { glyph: '●', ink: 'tempo' },
  blocked: { glyph: '▣', ink: 'hold' },
  'needs-you': { glyph: '◆', ink: 'hold' },
  failed: { glyph: '✖', ink: 'error' },
  completed: { glyph: '✔', ink: 'ok' },
  archived: { glyph: '⊘', ink: 'muted' },
} as const;

export type TicketStateId = keyof typeof STATE_TOKENS;

const STATE_WORD_KEY: Record<TicketStateId, string> = {
  queued: 'queued',
  running: 'running',
  blocked: 'blocked',
  'needs-you': 'needsYou',
  failed: 'failed',
  completed: 'completed',
  archived: 'archived',
};

/** A state glyph + word in the state's token ink; `spin` animates while running. */
export function StateMark({ state, spin = false }: { state: TicketStateId; spin?: boolean }) {
  const { t } = useTranslation();
  const token = STATE_TOKENS[state];
  return (
    <span className={`sa-state sa-state--${state}`}>
      <span className={`sa-state-glyph${spin ? ' sa-state-glyph--spin' : ''}`} aria-hidden="true">
        {token.glyph}
      </span>
      <span className="sa-state-word">{t(`tnInbox.state.${STATE_WORD_KEY[state]}`)}</span>
    </span>
  );
}

/**
 * IME guard: an Enter that arrives while an input method is composing
 * (`isComposing`) or carries the 229 keyCode must never run, send or insert.
 */
function isComposingEvent(e: {
  isComposing?: boolean;
  keyCode?: number;
  nativeEvent?: { isComposing?: boolean; keyCode?: number };
}): boolean {
  if (e.isComposing || e.keyCode === 229) return true;
  const native = e.nativeEvent;
  return !!(native && (native.isComposing || native.keyCode === 229));
}

/**
 * Composer key contract: a bare Enter sends, Shift+Enter and Ctrl+J insert a
 * newline, every other chord is inert. The IME guard is checked before this.
 */
function composerKeyAction(e: {
  key: string;
  shiftKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  metaKey: boolean;
}): 'send' | 'newline' | null {
  if (e.key === 'Enter') {
    if (e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey) return 'newline';
    if (!e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey) return 'send';
    return null;
  }
  if (e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey && (e.key === 'j' || e.key === 'J')) {
    return 'newline';
  }
  return null;
}

/**
 * Whether a `/` should open the command palette: only a bare `/` (no modifier),
 * with the caret collapsed, sitting at column 0 or right after a newline — the
 * boundary home.html advertises ("`/` at column 0 opens the palette"). A `/`
 * typed mid-word is ordinary text.
 */
function slashOpensPalette(
  e: { key: string; ctrlKey: boolean; altKey: boolean; metaKey: boolean },
  value: string,
  caret: number,
  caretEnd: number,
): boolean {
  if (e.key !== '/') return false;
  if (e.ctrlKey || e.altKey || e.metaKey) return false;
  if (caret !== caretEnd) return false;
  return caret === 0 || value.charAt(caret - 1) === '\n';
}

/**
 * A controlled composer textarea: value, caret-preserving newline insertion and
 * the shared key contract wired to an `onSend`. The caller owns the textarea
 * ref (so the ref is attached, never read during render); the IME guard runs
 * first, so a composing Enter neither sends nor inserts.
 *
 * `returnFocusId` names the element Escape hands focus to (the reply trap → the
 * main composer), matching home.html's `data-return`. The draft is left intact.
 */
function useComposerText(
  ref: RefObject<HTMLTextAreaElement | null>,
  initial = '',
  returnFocusId?: string,
) {
  const [value, setValue] = useState(initial);
  const pendingCaret = useRef<number | null>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (el && pendingCaret.current != null) {
      const pos = pendingCaret.current;
      el.selectionStart = el.selectionEnd = pos;
      pendingCaret.current = null;
    }
  }, [value, ref]);

  const insertNewline = useCallback(() => {
    const el = ref.current;
    const start = el?.selectionStart ?? value.length;
    const end = el?.selectionEnd ?? value.length;
    pendingCaret.current = start + 1;
    setValue(value.slice(0, start) + '\n' + value.slice(end));
  }, [ref, value]);

  const onKeyDown = useCallback(
    (e: ReactKeyboardEvent<HTMLTextAreaElement>, onSend: () => void) => {
      if (isComposingEvent(e)) return;
      // Escape hands focus to the return target (the reply trap returns to the
      // composer) without clearing the draft the operator already typed.
      if (e.key === 'Escape' && returnFocusId) {
        e.preventDefault();
        document.getElementById(returnFocusId)?.focus();
        return;
      }
      const action = composerKeyAction(e);
      if (action === 'newline') {
        e.preventDefault();
        insertNewline();
      } else if (action === 'send') {
        e.preventDefault();
        onSend();
      } else if (slashOpensPalette(e, value, ref.current?.selectionStart ?? value.length, ref.current?.selectionEnd ?? value.length)) {
        e.preventDefault();
        openPalette(ref.current, '/');
      }
    },
    [insertNewline, ref, returnFocusId, value],
  );

  return { value, setValue, insertNewline, onKeyDown };
}

export type TrapCopy = 'reply' | 'clarify';

/**
 * The reply trap: a per-Ticket composer that sits in front of the main
 * composer, so a pending question is answered before new work is started.
 * Enter sends, Shift+Enter / Ctrl+J newline, IME inert, Escape returns to the
 * composer with the draft kept. `submit` is the Ticket's own answer route: an
 * instruction for a question, the clarification confirm for a drafted request.
 */
export function ReplyComposer({
  ticket,
  copy,
  initial = '',
  submit,
}: {
  ticket: AutonomyTicket;
  copy: TrapCopy;
  initial?: string;
  submit: (text: string) => Promise<unknown>;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const replyRef = useRef<HTMLTextAreaElement>(null);
  const composer = useComposerText(replyRef, initial, 'composer-input');
  const [status, setStatus] = useState('');
  // Synchronous in-flight guard: `answer.isPending` is stale inside the Enter
  // keydown until React re-renders, so a second Enter could send twice.
  const sendingRef = useRef(false);
  const answer = useMutation({
    mutationFn: (text: string) => submit(text),
    onSuccess: () => {
      sendingRef.current = false;
      composer.setValue('');
      setStatus(t(`tnInbox.${copy}.sent`));
      // A confirmed clarification is gone server-side (the GET now 404s), so
      // refetching it would flash "unavailable" before the card leaves the
      // list. Refresh everything else; the Ticket list drops the card.
      void qc.invalidateQueries({
        queryKey: queryKeys.autonomy.all,
        predicate: (query) => !(copy === 'clarify' && query.queryKey.at(-1) === 'clarification'),
      });
    },
    onError: () => {
      sendingRef.current = false;
      setStatus(t(`tnInbox.${copy}.failed`));
    },
  });
  const send = () => {
    if (sendingRef.current || answer.isPending) return;
    const text = composer.value.trim();
    if (!text) {
      setStatus(t(`tnInbox.${copy}.empty`));
      return;
    }
    sendingRef.current = true;
    answer.mutate(text);
  };
  const shortId = ticket.ticket_id.slice(0, 8);
  const label = t(`tnInbox.${copy}.label`, { id: shortId });
  return (
    <div className="sa-reply-trap" role="group" aria-label={label} data-composer-box data-trap={copy}>
      <label className="sa-reply-trap-label" htmlFor={`reply-${ticket.ticket_id}`}>
        {label}
      </label>
      <textarea
        id={`reply-${ticket.ticket_id}`}
        ref={replyRef}
        data-testid="inbox-reply-input"
        className="sa-composer-input"
        value={composer.value}
        onChange={(e) => composer.setValue(e.target.value)}
        placeholder={t(`tnInbox.${copy}.placeholder`)}
        rows={copy === 'clarify' ? 3 : 2}
        onKeyDown={(e) => composer.onKeyDown(e, send)}
      />
      <button type="button" className="sa-primary sa-btn-sm" disabled={answer.isPending} onClick={send}>
        {t('tnInbox.reply.send')}
      </button>
      <span className="sa-mono-id" role="status" data-testid="inbox-reply-status">
        {status || t(`tnInbox.${copy}.status`)}
      </span>
    </div>
  );
}

export function useTickets(filters: {
  status?: string;
  source_id?: string;
  schedule_id?: string;
  failed?: boolean;
  attention?: boolean;
  q?: string;
  sort?: 'updated' | 'created' | 'title';
} = {}) {
  return useQuery({
    queryKey: [...queryKeys.autonomy.tickets(), filters],
    queryFn: () => listAutonomyTickets(filters),
    refetchInterval: 2_500,
  });
}

export function useInfiniteTickets(filters: {
  status?: string;
  source_id?: string;
  schedule_id?: string;
  failed?: boolean;
  attention?: boolean;
  q?: string;
  sort?: 'updated' | 'created' | 'title';
} = {}) {
  const pageSize = 50;
  return useInfiniteQuery({
    queryKey: [...queryKeys.autonomy.tickets(), 'infinite', filters],
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      listAutonomyTickets({ ...filters, limit: pageSize, offset: pageParam }),
    getNextPageParam: (last, pages) =>
      last.tickets.length === pageSize ? pages.length * pageSize : undefined,
    refetchInterval: 2_500,
  });
}

export function useSources() {
  return useQuery({
    queryKey: queryKeys.autonomy.sources(),
    queryFn: listAutonomySources,
    refetchInterval: 10_000,
  });
}

export function useTicket(ticketId: string | undefined) {
  return useQuery({
    queryKey: ticketId ? queryKeys.autonomy.ticket(ticketId) : ['autonomy', 'ticket', 'none'],
    queryFn: () => getAutonomyTicket(ticketId!),
    enabled: Boolean(ticketId),
    refetchInterval: 2_500,
  });
}

export function TaskComposer({ compact = false }: { compact?: boolean }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const requestedProvider = params.get('provider') ?? '';
  const providerSeed = requestedProvider === 'ptc' ? '' : requestedProvider;
  const qc = useQueryClient();
  const workspaces = useAllWorkspaces({ includeHome: true });
  const { data: runtimes } = useNativeAgents(true);
  const daemons = useQuery({ queryKey: queryKeys.autonomy.daemons(), queryFn: listAutonomyDaemons, staleTime: 15_000 });
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const composer = useComposerText(composerRef, '');
  // A synchronous in-flight guard: `create.isPending` is stale in the Enter
  // keydown handler until React re-renders, so a second Enter could still fire
  // a duplicate create. The ref flips before mutate() and clears on settle.
  const submittingRef = useRef(false);
  const [title, setTitle] = useState('');
  const [workspaceId, setWorkspaceId] = useState(() => localStorage.getItem('sa.workspace') ?? '');
  const [provider, setProvider] = useState(() => providerSeed || localStorage.getItem('sa.provider') || '');
  const [daemon, setDaemon] = useState(() => localStorage.getItem('sa.daemon') || '');
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState('');
  // The mode label follows S03's composerMode (Alt+M); PaletteHost owns the cycle.
  const mode = useSyncExternalStore(subscribeComposerMode, getComposerMode, getComposerMode);

  const nativeOptions = (runtimes ?? []).filter((r) => r.installed && r.live_verified);
  useEffect(() => {
    localStorage.setItem('sa.workspace', workspaceId);
  }, [workspaceId]);
  useEffect(() => {
    localStorage.setItem('sa.provider', provider);
  }, [provider]);
  useEffect(() => {
    localStorage.setItem('sa.daemon', daemon);
  }, [daemon]);
  useEffect(() => {
    const rows = workspaces.data?.workspaces;
    if (workspaceId && rows && !rows.some((ws) => ws.workspace_id === workspaceId)) {
      setWorkspaceId('');
    }
  }, [workspaceId, workspaces.data]);
  useEffect(() => {
    if (provider && runtimes !== undefined && !nativeOptions.some((r) => r.provider === provider)) setProvider('');
  }, [nativeOptions, provider]);
  const create = useMutation({
    mutationFn: () =>
      createManualTask({
        prompt: composer.value.trim(),
        title: title.trim(),
        workspace_id: workspaceId,
        worker_provider: provider,
        worker_daemon_id: daemon || undefined,
      }),
    onSuccess: (ticket) => {
      submittingRef.current = false;
      composer.setValue('');
      setTitle('');
      setError(null);
      setStatus('');
      void qc.invalidateQueries({ queryKey: queryKeys.autonomy.all });
      navigate(`/tasks/${ticket.ticket_id}`);
    },
    onError: (err: Error) => {
      submittingRef.current = false;
      setError(err.message);
    },
  });

  const submit = () => {
    // Enter (or the send button) while a create is already in flight must not
    // fire a second mutation: the button is disabled, but the key path is not.
    if (submittingRef.current || create.isPending) return;
    if (!workspaceId) {
      setError(t('tnInbox.composer.needWorkspace'));
      setStatus(t('tnInbox.composer.needWorkspace'));
      return;
    }
    if (!composer.value.trim()) {
      setError(t('tnInbox.composer.needPrompt'));
      setStatus(t('tnInbox.composer.needPrompt'));
      return;
    }
    setError(null);
    setStatus(t('tnInbox.composer.status'));
    submittingRef.current = true;
    create.mutate();
  };

  return (
    <section
      className={`sa-composer ${compact ? 'sa-composer--compact' : ''}`}
      data-composer-box
      aria-label={t('tnInbox.composerHeading')}
    >
      <div className="sa-composer-line">
        <span className="sa-composer-prompt" aria-hidden="true">›</span>
        <textarea
          id="composer-input"
          ref={composerRef}
          data-testid="inbox-composer-input"
          className="sa-composer-input"
          value={composer.value}
          onChange={(e) => composer.setValue(e.target.value)}
          placeholder={t('tnInbox.composer.prompt')}
          rows={compact ? 3 : 2}
          onKeyDown={(e) => composer.onKeyDown(e, submit)}
        />
        <button type="button" className="sa-primary" disabled={create.isPending} onClick={submit}>
          {create.isPending ? t('tnInbox.composer.creating') : t('tnInbox.composer.create')}
        </button>
        <span className="sa-mode-chip" title="Alt+M">
          {t('tnInbox.composer.mode')} <b data-testid="inbox-composer-mode">{t(`tnInbox.composer.modes.${mode}`)}</b>
        </span>
      </div>
      <div className="sa-composer-row">
        <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder={t('tnInbox.composer.title')} />
        <select value={workspaceId} onChange={(e) => setWorkspaceId(e.target.value)} aria-label={t('tnInbox.composer.workspace')}>
          <option value="">{t('tnInbox.composer.workspace')}</option>
          {(workspaces.data?.workspaces ?? []).map((ws) => (
            <option key={ws.workspace_id} value={ws.workspace_id}>{ws.name}{ws.is_home ? ' (Home)' : ''}</option>
          ))}
        </select>
        <select value={provider} onChange={(e) => setProvider(e.target.value)} aria-label={t('tnInbox.composer.provider')}>
          <option value="">{t('tnInbox.composer.provider')}</option>
          {nativeOptions.map((runtime) => (
            <option key={runtime.provider} value={runtime.provider}>{nativeRuntimeLabel(runtime.provider)}</option>
          ))}
        </select>
        <select value={daemon} onChange={(e) => setDaemon(e.target.value)} aria-label={t('tnInbox.composer.daemon')}>
          <option value="">{t('tnInbox.composer.daemon')}</option>
          {(daemons.data?.daemons ?? []).map((row) => (
            <option key={row.daemon_id} value={row.daemon_id}>
              Local daemon · {row.name || row.daemon_id.slice(0, 8)}
            </option>
          ))}
        </select>
      </div>
      <span className="sa-mono-id" role="status" data-testid="inbox-composer-status">
        {status || t('tnInbox.composer.status')}
      </span>
      {error ? <div className="sa-error" role="alert">{error}</div> : null}
    </section>
  );
}

/**
 * A compact, localized age ("9h ago" / "9 小时前") for terminal-native rows.
 * The clock is a render input from useNow, never Date.now() in render.
 */
export function Age({ iso }: { iso: string }) {
  const { t } = useTranslation();
  const now = useNow();
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const [unit, n] =
    seconds < 60 ? ['s', seconds] : minutes < 60 ? ['m', minutes] : hours < 24 ? ['h', hours] : ['d', Math.floor(hours / 24)];
  return <>{t(`tnInbox.age.${unit}`, { n })}</>;
}

function textId(value: unknown): string {
  if (typeof value === 'string') return value.trim();
  if (value == null || typeof value === 'object') return '';
  return String(value).trim();
}

/** Top-level ids win. Older payloads only stored them on the mandate. */
export function ticketSourceId(ticket: {
  source_id?: string | null;
  mandate?: Record<string, unknown> | null;
}): string {
  return textId(ticket.source_id) || textId(ticket.mandate?.source_id);
}

export function ticketTriggerId(ticket: {
  trigger_id?: string | null;
  origin_trigger_id?: string | null;
  mandate?: Record<string, unknown> | null;
}): string {
  return (
    textId(ticket.trigger_id) ||
    textId(ticket.origin_trigger_id) ||
    textId(ticket.mandate?.trigger_id)
  );
}
