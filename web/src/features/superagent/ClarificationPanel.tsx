import {
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type TextareaHTMLAttributes,
} from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { confirmTicketClarification, getTicketClarification } from '@/api/autonomy';
import { openPalette } from '@/features/palette/PaletteHost';
import { registerFeatureLocales } from '@/lib/featureLocales';
import { queryKeys } from '@/lib/queryKeys';
import tnTicketEn from './locales/tnTicket.en-US.json';
import tnTicketZh from './locales/tnTicket.zh-CN.json';
import './ticket.css';

// The Ticket page keeps its copy in its own tnTicket pair. Registering here
// (module load) covers every importer: TaskPage and the shared detail view.
registerFeatureLocales({ 'en-US': tnTicketEn, 'zh-CN': tnTicketZh });

/** The element a trap hands focus to on Escape: the Ticket page composer. */
export const TICKET_COMPOSER_ID = 'composer-input';

type ComposerKey = Pick<
  ReactKeyboardEvent<HTMLTextAreaElement>,
  'key' | 'shiftKey' | 'ctrlKey' | 'altKey' | 'metaKey' | 'keyCode' | 'nativeEvent'
>;

/** An Enter (or any key) while an input method is composing never runs, sends or inserts. */
function composing(e: ComposerKey): boolean {
  if (e.keyCode === 229) return true;
  const native = e.nativeEvent as { isComposing?: boolean; keyCode?: number } | undefined;
  return !!native && (native.isComposing === true || native.keyCode === 229);
}

/** Bare Enter sends; Shift+Enter and Ctrl+J insert a newline; other chords are inert. */
function keyAction(e: ComposerKey): 'send' | 'newline' | null {
  const plain = !e.ctrlKey && !e.altKey && !e.metaKey;
  if (e.key === 'Enter') {
    if (plain && e.shiftKey) return 'newline';
    if (plain && !e.shiftKey) return 'send';
    return null;
  }
  if (e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey && (e.key === 'j' || e.key === 'J')) {
    return 'newline';
  }
  return null;
}

type TerminalTextareaProps = Omit<
  TextareaHTMLAttributes<HTMLTextAreaElement>,
  'value' | 'onChange' | 'onKeyDown'
> & {
  value: string;
  onValueChange: (value: string) => void;
  onSend: () => void;
  /** Escape moves focus here and keeps the draft (a trap returns to the composer). */
  returnFocusId?: string;
  /** `/` at column 0 opens the command palette prefilled with `/`. */
  slashOpensPalette?: boolean;
};

/**
 * The terminal-native composer field: Enter sends, Shift+Enter / Ctrl+J insert
 * a newline at the caret, the IME guard runs before everything, Escape hands
 * focus to `returnFocusId`.
 */
export function TerminalTextarea({
  value,
  onValueChange,
  onSend,
  returnFocusId,
  slashOpensPalette = false,
  readOnly,
  ...rest
}: TerminalTextareaProps) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const pendingCaret = useRef<number | null>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (el && pendingCaret.current != null) {
      el.selectionStart = el.selectionEnd = pendingCaret.current;
      pendingCaret.current = null;
    }
  }, [value]);

  const onKeyDown = (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (composing(e)) return;
    const el = e.currentTarget;
    if (e.key === 'Escape' && returnFocusId) {
      const back = document.getElementById(returnFocusId);
      if (back && back !== el) {
        e.preventDefault();
        back.focus();
      }
      return;
    }
    const action = keyAction(e);
    if (action === 'newline') {
      e.preventDefault();
      if (readOnly) return;
      const start = el.selectionStart ?? value.length;
      const end = el.selectionEnd ?? value.length;
      pendingCaret.current = start + 1;
      onValueChange(value.slice(0, start) + '\n' + value.slice(end));
      return;
    }
    if (action === 'send') {
      e.preventDefault();
      onSend();
      return;
    }
    if (
      slashOpensPalette &&
      e.key === '/' &&
      !e.ctrlKey &&
      !e.altKey &&
      !e.metaKey &&
      el.selectionStart === el.selectionEnd &&
      (el.selectionStart === 0 || value.charAt(el.selectionStart - 1) === '\n')
    ) {
      e.preventDefault();
      openPalette(el, '/');
    }
  };

  return (
    <textarea
      {...rest}
      ref={ref}
      value={value}
      readOnly={readOnly}
      aria-readonly={readOnly || undefined}
      onChange={(e) => onValueChange(e.target.value)}
      onKeyDown={onKeyDown}
    />
  );
}

/**
 * The clarification gate as a needs-you trap: it sits in front of the Ticket
 * composer, the drafted request is prefilled, Enter confirms it (edited or as
 * drafted), Shift+Enter / Ctrl+J add a newline, Escape returns to the composer.
 */
export function ClarificationPanel({
  ticketId,
  returnFocusId = TICKET_COMPOSER_ID,
}: {
  ticketId: string;
  returnFocusId?: string;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [draft, setDraft] = useState<string | null>(null);
  const [status, setStatus] = useState('');
  // `isPending` is stale inside the Enter keydown until React re-renders, so a
  // second Enter could confirm twice without this synchronous guard.
  const sending = useRef(false);
  const clarification = useQuery({
    queryKey: [...queryKeys.autonomy.ticket(ticketId), 'clarification'],
    queryFn: () => getTicketClarification(ticketId),
    retry: false,
  });
  const confirm = useMutation({
    mutationFn: (text: string) => confirmTicketClarification(ticketId, text),
    onSuccess: () => {
      sending.current = false;
      setStatus(t('tnTicket.clarify.sent'));
      // A confirmed clarification is gone server-side (the GET 404s); do not
      // re-read it, refresh the Ticket instead.
      void qc.invalidateQueries({
        queryKey: queryKeys.autonomy.all,
        predicate: (query) => query.queryKey.at(-1) !== 'clarification',
      });
    },
    onError: () => {
      sending.current = false;
    },
  });

  if (clarification.isLoading) return <p className="sa-muted">{t('tnTicket.clarify.loading')}</p>;
  if (clarification.isError || !clarification.data) return null;
  const data = clarification.data;
  const text = draft ?? data.expanded_request;
  const send = (value: string) => {
    if (sending.current || confirm.isPending) return;
    if (!value.trim()) {
      setStatus(t('tnTicket.clarify.empty'));
      return;
    }
    sending.current = true;
    confirm.mutate(value);
  };
  const score = data.clarity_score;
  const fieldId = `clarify-${ticketId}`;
  const statusId = `${fieldId}-status`;

  return (
    <section
      className="tn-ticket-trap"
      role="group"
      aria-labelledby={`${fieldId}-heading`}
      data-trap="clarify"
      data-composer-box
    >
      <div className="tn-ticket-trap-head">
        <span className="sa-state sa-state--needs-you">
          <span className="sa-state-glyph" aria-hidden="true">◆</span>
          <span className="sa-state-word">{t('tnTicket.clarify.needsYou')}</span>
        </span>
        <h3 id={`${fieldId}-heading`}>{t('tnTicket.clarify.heading')}</h3>
      </div>
      <p className="sa-mono-id">
        {score < 0.6 ? t('tnTicket.clarify.below', { score }) : t('tnTicket.clarify.score', { score })}
      </p>
      <p className="tn-ticket-label">{t('tnTicket.clarify.original')}</p>
      <p className="tn-ticket-prose">{data.original_prompt}</p>
      {data.assumptions.length ? (
        <>
          <p className="tn-ticket-label">{t('tnTicket.clarify.assumptions')}</p>
          <ul className="tn-ticket-list">
            {data.assumptions.map((item) => <li key={item}>{item}</li>)}
          </ul>
        </>
      ) : null}
      {data.open_questions.length ? (
        <>
          <p className="tn-ticket-label">{t('tnTicket.clarify.questions')}</p>
          <ol className="tn-ticket-list">
            {data.open_questions.map((item) => <li key={item}>{item}</li>)}
          </ol>
        </>
      ) : null}
      <label className="tn-ticket-label" htmlFor={fieldId}>
        {t('tnTicket.clarify.review')}
      </label>
      <TerminalTextarea
        id={fieldId}
        data-testid="ticket-clarify-input"
        className="tn-ticket-input"
        rows={4}
        value={text}
        onValueChange={setDraft}
        onSend={() => send(text)}
        returnFocusId={returnFocusId}
        aria-describedby={statusId}
      />
      {confirm.isError ? <div className="sa-error" role="alert">{t('tnTicket.clarify.failed')}</div> : null}
      <div className="tn-ticket-actions">
        <button type="button" className="sa-primary" disabled={confirm.isPending} onClick={() => send(text)}>
          {t('tnTicket.clarify.confirm')}
        </button>
        <button
          type="button"
          className="sa-link-button"
          disabled={confirm.isPending}
          onClick={() => {
            setDraft(data.expanded_request);
            send(data.expanded_request);
          }}
        >
          {t('tnTicket.clarify.asDrafted')}
        </button>
      </div>
      <span className="sa-mono-id" id={statusId} role="status">
        {status || t('tnTicket.clarify.status')}
      </span>
    </section>
  );
}
