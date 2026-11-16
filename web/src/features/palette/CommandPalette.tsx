/**
 * Command palette dialog: D1 ranking, D2 argument completion, D3 keys,
 * IME-safe Enter. Approve/reject navigate to /dispatch/:id?confirm=… only.
 */
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';
import { completeArgs, type ArgOption } from './argComplete';
import {
  canonicalCommand,
  commandByName,
  COMPLETION_LIMIT,
  type PaletteCommand,
} from './commands';
import { rankCommands, type RankedCommand } from './matcher';
import './palette.css';

export type CommandPaletteProps = {
  open: boolean;
  prefill?: string;
  onClose: () => void;
};

type CmdRow = {
  kind: 'cmd';
  command: PaletteCommand;
  name: string;
  hint: string;
  meta: string;
  onName: boolean;
  idx: number[] | null;
};

type ArgRow = {
  kind: 'arg';
  command: string;
  name: string;
  hint: string;
  idx: number[] | null;
  more: boolean;
  line: string;
};

type Row = CmdRow | ArgRow;

type ParseResult =
  | { mode: 'cmd'; query: string }
  | {
      mode: 'arg';
      raw: string;
      cmd: string | null;
      args: string[];
      partial: string;
      text: string;
    };

function parseQuery(value: string): ParseResult {
  const parts = value.split(/\s+/).filter((s) => s.length > 0);
  const trailing = /\s$/.test(value);
  if (parts.length > 1 || (parts.length === 1 && trailing)) {
    const raw = parts[0]!;
    let cmd = canonicalCommand(raw);
    const args = parts.slice(1);
    if (cmd === 'tickets' && raw.replace(/^\//, '').toLowerCase() === 'attach') {
      args.unshift('watch');
    }
    const partial = args.length && !trailing ? args.pop()! : '';
    return {
      mode: 'arg',
      raw,
      cmd,
      args,
      partial,
      text: value.replace(/\s+$/, ''),
    };
  }
  return { mode: 'cmd', query: value.replace(/^\//, '') };
}

function markup(text: string, idx: number[] | null): ReactNode {
  if (!idx || !idx.length) return text;
  const nodes: ReactNode[] = [];
  let pos = 0;
  for (let i = 0; i < idx.length; i++) {
    let run = 1;
    while (i + run < idx.length && idx[i + run] === idx[i]! + run) run++;
    if (idx[i]! > pos) nodes.push(text.slice(pos, idx[i]));
    nodes.push(<b key={`${idx[i]}-${run}`}>{text.substr(idx[i]!, run)}</b>);
    pos = idx[i]! + run;
    i += run - 1;
  }
  if (pos < text.length) nodes.push(text.slice(pos));
  return nodes;
}

function isImeEnter(e: { isComposing?: boolean; keyCode?: number; nativeEvent?: { isComposing?: boolean; keyCode?: number } }): boolean {
  if (e.isComposing || e.keyCode === 229) return true;
  const native = e.nativeEvent;
  return !!(native && (native.isComposing || native.keyCode === 229));
}

function altOnly(e: { altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean; code: string }, code: string): boolean {
  return e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey && e.code === code;
}

function printable(e: { key: string; ctrlKey: boolean; metaKey: boolean; altKey: boolean }): boolean {
  return e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;
}

function runPath(command: string, args: string[]): string | null {
  switch (command) {
    case 'dispatch':
      return args[0] ? `/dispatch/${encodeURIComponent(args[0])}` : '/dispatch';
    case 'approve':
      return args[0] ? `/dispatch/${encodeURIComponent(args[0])}?confirm=approve` : null;
    case 'reject':
      return args[0] ? `/dispatch/${encodeURIComponent(args[0])}?confirm=reject` : null;
    case 'tickets':
      if (args[0] === 'watch' && args[1]) return `/tasks/${encodeURIComponent(args[1])}`;
      if (args[0] && args[0] !== '--attention' && args[0] !== 'watch') {
        return `/tasks/${encodeURIComponent(args[0])}`;
      }
      return '/tasks';
    case 'agents':
      return '/agents';
    case 'skills':
    case 'mcp':
      return '/skills-mcp';
    case 'signals':
      return '/sources';
    case 'workers':
      return '/agent-fleet';
    case 'memory':
      return '/inbox';
    case 'help':
    case 'status':
    case 'model':
      return '/settings';
    default:
      return '/inbox';
  }
}

export function CommandPalette({ open, prefill = '', onClose }: CommandPaletteProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const listId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const [query, setQuery] = useState(prefill);
  const [index, setIndex] = useState(0);
  const [navMode, setNavMode] = useState(false);
  const [rows, setRows] = useState<Row[]>([]);
  const [note, setNote] = useState('');
  const [errorNote, setErrorNote] = useState(false);
  const [countLabel, setCountLabel] = useState('');
  const [status, setStatus] = useState('');
  const [focusTarget, setFocusTarget] = useState<'query' | 'list'>('query');
  const argCache = useRef<{ key: string; options: ArgOption[] } | null>(null);

  const rebuild = useCallback(async (value: string) => {
    const parsed = parseQuery(value);
    if (parsed.mode === 'cmd') {
      const ranked: RankedCommand[] = rankCommands(parsed.query);
      const next: CmdRow[] = ranked.map((r) => ({
        kind: 'cmd',
        command: r.command,
        name: r.command.name,
        hint: r.command.hint,
        meta: r.onName ? r.command.slash || 'no slash' : r.key,
        onName: r.onName,
        idx: r.score ? r.score.idx : null,
      }));
      setRows(next);
      setCountLabel(
        next.length
          ? t('tnPalette.countOf', { shown: next.length, total: next.length || COMPLETION_LIMIT })
          : '0',
      );
      setErrorNote(false);
      setNote(next.length ? '' : t('tnPalette.noMatchBilingual'));
      argCache.current = null;
      return;
    }

    if (!parsed.cmd) {
      setRows([]);
      setErrorNote(false);
      setNote(t('tnPalette.noMatchBilingual'));
      setCountLabel('unknown');
      return;
    }

    const result = await completeArgs(parsed.cmd, parsed.partial);
    argCache.current = { key: `${parsed.cmd}:${parsed.partial}`, options: result.options };

    if (result.freeText) {
      setRows([]);
      setCountLabel('free text');
      if (!parsed.partial && result.needsId) {
        setErrorNote(true);
        setNote(`${result.needHintEn} · ${result.needHintZh}`);
      } else {
        setErrorNote(false);
        setNote('');
      }
      return;
    }

    if (parsed.cmd === 'approve' || parsed.cmd === 'reject' || parsed.cmd === 'dispatch') {
      const next: ArgRow[] = result.options.map((o) => ({
        kind: 'arg',
        command: parsed.cmd!,
        name: o.id,
        hint: o.hint,
        idx: parsed.partial ? [...Array(parsed.partial.length).keys()] : null,
        more: false,
        line: [parsed.cmd, ...parsed.args, o.id].join(' '),
      }));
      setRows(next);
      setCountLabel(
        next.length
          ? `${next.length} of ${result.options.length}${parsed.cmd !== 'dispatch' ? ` · ${t('tnPalette.needsOne')}` : ''}`
          : '0',
      );
      if (!next.length && result.needsId) {
        setErrorNote(true);
        setNote(`${result.needHintEn} · ${result.needHintZh}`);
      } else if (!next.length && parsed.partial) {
        setErrorNote(false);
        setNote(`nothing starts with ${parsed.partial}`);
      } else {
        setErrorNote(false);
        setNote('');
      }
      return;
    }

    setRows([]);
    setErrorNote(false);
    setNote('');
    setCountLabel('no argument');
  }, [t]);

  useEffect(() => {
    if (!open) return;
    setQuery(prefill);
    setIndex(0);
    setNavMode(false);
    setFocusTarget('query');
    void rebuild(prefill);
  }, [open, prefill, rebuild]);

  useEffect(() => {
    if (!open) return;
    void rebuild(query);
  }, [query, open, rebuild]);

  useLayoutEffect(() => {
    if (!open) return;
    if (focusTarget === 'query') inputRef.current?.focus();
    else listRef.current?.focus();
  }, [open, focusTarget]);

  useEffect(() => {
    if (index >= rows.length) setIndex(rows.length ? rows.length - 1 : 0);
  }, [rows, index]);

  const insertRow = useCallback(
    (row: Row | undefined) => {
      if (!row) {
        setStatus(note || 'nothing to complete');
        return;
      }
      if (row.kind === 'cmd') {
        const more = row.command.requiresArg;
        setQuery(row.command.name + (more ? ' ' : ''));
        setIndex(0);
        setStatus(
          more
            ? `inserted ${row.command.name} — it needs an argument; completion offers them`
            : `inserted ${row.command.name}`,
        );
        return;
      }
      setQuery(row.line + (row.more ? ' ' : ''));
      setIndex(0);
      setStatus(`inserted ${row.name}`);
    },
    [note],
  );

  const runLine = useCallback(
    (command: string, args: string[]) => {
      const path = runPath(command, args);
      onClose();
      if (path) navigate(path);
    },
    [navigate, onClose],
  );

  const lineComplete = useCallback((parsed: Extract<ParseResult, { mode: 'arg' }>): boolean => {
    if (!parsed.cmd) return false;
    const meta = commandByName(parsed.cmd);
    if (!parsed.partial) {
      if (meta?.requiresArg && parsed.args.length === 0) return false;
      return true;
    }
    if (parsed.cmd === 'reply') return true;
    const opts = argCache.current?.options ?? [];
    const exact = opts.some((o) => o.id === parsed.partial);
    if (!exact) return false;
    return !(meta?.requiresArg && parsed.args.length === 0 && !parsed.partial);
  }, []);

  const activateRow = useCallback(
    (on: Row | undefined) => {
      const parsed = parseQuery(query);
      if (parsed.mode === 'cmd') {
        if (!on || on.kind !== 'cmd') {
          setStatus(t('tnPalette.noMatchBilingual'));
          return;
        }
        if (on.command.requiresArg) {
          insertRow(on);
          return;
        }
        runLine(on.command.name, []);
        return;
      }

      if (!parsed.cmd) {
        setStatus(t('tnPalette.noMatchBilingual'));
        return;
      }

      const cmd = parsed.cmd;

      if (lineComplete(parsed)) {
        runLine(cmd, parsed.partial ? [...parsed.args, parsed.partial] : parsed.args);
        return;
      }

      if (on && on.kind === 'arg') {
        insertRow(on);
        return;
      }

      if (cmd === 'approve' || cmd === 'reject') {
        const bilingual =
          cmd === 'approve'
            ? t('tnPalette.hintApproveBilingual')
            : t('tnPalette.hintRejectBilingual');
        setErrorNote(true);
        setNote(bilingual);
        setStatus(bilingual);
        return;
      }

      if (cmd === 'reply') {
        setErrorNote(true);
        setNote(`${t('tnPalette.replyNeedsText')} · reply 需要回复内容`);
        setStatus(t('tnPalette.replyNeedsText'));
        return;
      }

      setStatus(note || 'incomplete');
    },
    [query, insertRow, runLine, lineComplete, t, note],
  );

  const enterKey = useCallback(() => {
    activateRow(rows[index]);
  }, [activateRow, rows, index]);
  const onInputKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
      return;
    }
    if (altOnly(e, 'KeyN')) {
      e.preventDefault();
      setNavMode((v) => !v);
      return;
    }
    if (e.ctrlKey && e.key === 'ArrowDown') {
      e.preventDefault();
      setFocusTarget('list');
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (rows.length) setIndex((i) => Math.min(rows.length - 1, i + 1));
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (rows.length) setIndex((i) => Math.max(0, i - 1));
      return;
    }
    if (navMode && (e.key === 'j' || e.key === 'k') && printable(e)) {
      e.preventDefault();
      setIndex((i) => {
        if (!rows.length) return 0;
        return Math.max(0, Math.min(rows.length - 1, i + (e.key === 'j' ? 1 : -1)));
      });
      return;
    }
    if (navMode && printable(e)) setNavMode(false);
    if (e.key === 'Tab' && e.shiftKey) {
      e.preventDefault();
      return;
    }
    if (e.key === 'Tab') {
      e.preventDefault();
      insertRow(rows[index]);
      return;
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      if (isImeEnter(e)) return;
      e.preventDefault();
      enterKey();
    }
  };

  const onListKeyDown = (e: ReactKeyboardEvent<HTMLUListElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
      return;
    }
    if (e.ctrlKey && e.key === 'ArrowUp') {
      e.preventDefault();
      setFocusTarget('query');
      return;
    }
    if (altOnly(e, 'KeyN')) {
      e.preventDefault();
      setNavMode((v) => !v);
      return;
    }
    if (e.key === 'ArrowDown' || (e.key === 'j' && printable(e))) {
      e.preventDefault();
      if (rows.length) setIndex((i) => Math.min(rows.length - 1, i + 1));
      return;
    }
    if (e.key === 'ArrowUp' || (e.key === 'k' && printable(e))) {
      e.preventDefault();
      if (rows.length) setIndex((i) => Math.max(0, i - 1));
      return;
    }
    if (e.key === 'Tab' && e.shiftKey) {
      e.preventDefault();
      return;
    }
    if (e.key === 'Tab') {
      e.preventDefault();
      insertRow(rows[index]);
      return;
    }
    if (e.key === 'Enter') {
      if (isImeEnter(e)) return;
      e.preventDefault();
      enterKey();
      return;
    }
    if (printable(e)) {
      e.preventDefault();
      setFocusTarget('query');
      setQuery((q) => q + e.key);
      setIndex(0);
    }
  };

  if (!open) return null;

  return (
    <>
      <div className="tn-palette-scrim" data-testid="tn-palette-scrim" onClick={onClose} />
      <div
        className="tn-palette"
        role="dialog"
        aria-modal="true"
        aria-label={t('tnPalette.title')}
        data-testid="tn-palette"
      >
        <div className="tn-palette-query">
          <span className="tn-palette-prompt" aria-hidden="true">
            ›
          </span>
          <input
            ref={inputRef}
            type="text"
            role="combobox"
            aria-expanded="true"
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={rows[index] ? `tn-prow-${index}` : undefined}
            autoComplete="off"
            placeholder={t('tnPalette.placeholder')}
            value={query}
            data-testid="tn-palette-input"
            onChange={(e) => {
              setQuery(e.target.value);
              setIndex(0);
            }}
            onKeyDown={onInputKeyDown}
          />
          {navMode ? (
            <span className="tn-palette-nav" data-testid="tn-palette-nav" title="alt+n navigation mode">
              {t('tnPalette.nav')}
            </span>
          ) : null}
        </div>
        <div className="tn-palette-list-wrap">
          <ul
            ref={listRef}
            id={listId}
            className="tn-palette-list"
            role="listbox"
            tabIndex={-1}
            data-testid="tn-palette-list"
            onKeyDown={onListKeyDown}
            onMouseDown={(e) => {
              const li = (e.target as HTMLElement).closest('li[data-i]');
              if (li) e.preventDefault();
            }}
            onMouseMove={(e) => {
              const li = (e.target as HTMLElement).closest('li[data-i]');
              if (!li) return;
              const i = Number(li.getAttribute('data-i'));
              if (Number.isFinite(i) && i !== index) setIndex(i);
            }}
            onClick={(e) => {
              const li = (e.target as HTMLElement).closest('li[data-i]');
              if (!li) return;
              const i = Number(li.getAttribute('data-i'));
              if (!Number.isFinite(i)) return;
              setIndex(i);
              const row = rows[i];
              if (!row) return;
              if (row.kind === 'cmd') activateRow(row);
              else insertRow(row);
              inputRef.current?.focus();
            }}
          >
            {rows.length === 0 ? (
              <li
                className={`tn-pempty${errorNote ? ' tn-perror' : ''}`}
                role="option"
                aria-disabled="true"
                data-testid="tn-palette-empty"
              >
                {note || t('tnPalette.noMatch')}
              </li>
            ) : (
              rows.map((row, i) => (
                <li
                  key={`${row.kind}-${row.name}-${i}`}
                  id={`tn-prow-${i}`}
                  className="tn-prow"
                  role="option"
                  data-i={String(i)}
                  data-kind={row.kind}
                  aria-selected={i === index}
                >
                  <span className="tn-pname">{markup(row.name, row.idx)}</span>
                  <span className="tn-phint">{row.hint}</span>
                  <span className="tn-pmeta">
                    {row.kind === 'cmd' ? (row.onName ? row.meta : <b>{row.meta}</b>) : row.more ? 'needs more' : ''}
                  </span>
                </li>
              ))
            )}
          </ul>
        </div>
        <div className="tn-palette-foot">
          <span data-testid="tn-palette-count">{countLabel}</span>
          <span data-testid="tn-palette-status">{status || note}</span>
        </div>
      </div>
    </>
  );
}

export default CommandPalette;
