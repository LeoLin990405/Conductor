import React from 'react';
import { useTranslation } from 'react-i18next';
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from './ui/dialog';
import { lastInputWasPointer } from '@/lib/inputModality';
import { cn } from '@/lib/utils';
import { CONTROL_RADIUS, FOCUS_RING, MONO_CHROME } from './ui/tokens';

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  onConfirm?: () => void | Promise<void>;
  onOpenChange?: (open: boolean) => void;
  /** Open on the confirm button, so Enter confirms as a native confirm() does. */
  focusConfirm?: boolean;
}

/**
 * Reusable confirmation dialog. Uses color tokens only.
 *
 * It is a dialog like any other: the panel wears the one floating fill
 * (surface elevation 1 with the token shadow) that `DialogContent` paints,
 * and the two buttons under it wear the terminal's control geometry — a 2px
 * radius, the mono face, and the 2px ink.focus ring. Their labels wrap
 * inside their own buttons, so a long zh-CN or English label folds instead of
 * pushing the row past the edge of a phone.
 */
function ConfirmDialog({ open, title, message, confirmLabel, onConfirm, onOpenChange, focusConfirm = false }: ConfirmDialogProps) {
  const { t } = useTranslation();
  const confirmRef = React.useRef<HTMLButtonElement>(null);
  // Radix hands focus back to a Dialog.Trigger, and this dialog opens from
  // state without one, so it hands focus back to what held it itself.
  const returnFocusRef = React.useRef<HTMLElement | null>(null);
  const handleConfirm = async () => {
    if (onConfirm) await onConfirm();
    onOpenChange?.(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="sm:max-w-sm"
        onOpenAutoFocus={(e) => {
          returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
          if (!focusConfirm) return;
          e.preventDefault();
          confirmRef.current?.focus();
        }}
        onCloseAutoFocus={(e) => {
          // After a click DialogContent keeps focus off, as for every overlay.
          if (lastInputWasPointer()) return;
          e.preventDefault();
          returnFocusRef.current?.focus();
        }}
      >
        <DialogHeader>
          <DialogTitle className="title-font" style={{ color: 'var(--color-text-primary)' }}>
            {title}
          </DialogTitle>
          <p className={cn('text-sm', MONO_CHROME)} style={{ color: 'var(--color-text-secondary)' }}>{message}</p>
        </DialogHeader>
        <DialogFooter className="pt-4">
          <button
            type="button"
            onClick={() => onOpenChange?.(false)}
            className={cn(
              'px-3 py-1.5 text-sm border border-(--color-border-muted) text-(--color-text-primary)',
              'min-w-0 whitespace-normal break-words text-left hover:bg-(--color-bg-hover)',
              CONTROL_RADIUS,
              MONO_CHROME,
              FOCUS_RING,
            )}
          >
            {t('common.cancel')}
          </button>
          <button
            ref={confirmRef}
            type="button"
            onClick={handleConfirm}
            className={cn(
              'px-4 py-1.5 text-sm font-medium bg-(--color-btn-primary-bg) text-(--color-btn-primary-text) hover:opacity-90',
              'min-w-0 whitespace-normal break-words text-left',
              CONTROL_RADIUS,
              MONO_CHROME,
              FOCUS_RING,
            )}
          >
            {confirmLabel || t('common.delete')}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default ConfirmDialog;
