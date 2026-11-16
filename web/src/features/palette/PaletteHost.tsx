/**
 * Sole owner of the global Ctrl+K and Alt+M listeners. Other surfaces open
 * the palette through openPalette(opener); nobody else binds KeyK / KeyM.
 */
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { CommandPalette } from './CommandPalette';
import {
  cycleComposerMode,
  getComposerMode,
  subscribeComposerMode,
} from './composerMode';
import './commands'; // register tnPalette locales at module load

type Opener = HTMLElement | null;

type OpenRequest = {
  opener: Opener;
  prefill: string;
};

let externalOpen: ((req: OpenRequest) => void) | null = null;

/** Open the palette and restore focus to `opener` on Esc. */
export function openPalette(opener?: EventTarget | null, prefill = ''): void {
  const el =
    opener && opener instanceof HTMLElement
      ? opener
      : document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
  externalOpen?.({ opener: el, prefill });
}

function altOnlyKeyM(e: KeyboardEvent): boolean {
  return e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey && e.code === 'KeyM';
}

function ctrlOrMetaK(e: KeyboardEvent): boolean {
  return (e.ctrlKey || e.metaKey) && !e.altKey && e.code === 'KeyK';
}

export function PaletteHost() {
  const [open, setOpen] = useState(false);
  const [prefill, setPrefill] = useState('');
  const openerRef = useRef<Opener>(null);
  const mode = useSyncExternalStore(subscribeComposerMode, getComposerMode, getComposerMode);

  const close = useCallback(() => {
    setOpen(false);
    setPrefill('');
    const opener = openerRef.current;
    openerRef.current = null;
    queueMicrotask(() => {
      if (opener && document.contains(opener)) opener.focus();
    });
  }, []);

  const openWith = useCallback((req: OpenRequest) => {
    openerRef.current = req.opener;
    setPrefill(req.prefill);
    setOpen(true);
  }, []);

  useEffect(() => {
    externalOpen = openWith;
    return () => {
      if (externalOpen === openWith) externalOpen = null;
    };
  }, [openWith]);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (altOnlyKeyM(e)) {
        e.preventDefault();
        if (document.querySelector('[data-permission-pending]')) return;
        cycleComposerMode();
        return;
      }
      if (!open && ctrlOrMetaK(e)) {
        e.preventDefault();
        const active = document.activeElement;
        openWith({
          opener: active instanceof HTMLElement ? active : null,
          prefill: '',
        });
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [open, openWith]);

  return (
    <>
      <span data-composer-mode={mode} hidden aria-hidden="true" />
      <CommandPalette open={open} prefill={prefill} onClose={close} />
    </>
  );
}

export default PaletteHost;
