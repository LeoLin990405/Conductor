import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import ConfirmDialog from '@/components/ConfirmDialog';
import { CONTROL_RADIUS, FOCUS_RING, MONO_CHROME } from '../tokens';

/**
 * The confirmation dialog's contract.
 *
 * It is the one dialog a user meets most often, so the two things it owes a
 * keyboard user are worth locking: focus stays inside it while it is open,
 * and Escape closes it and hands focus back to whatever opened it. The rest
 * is the token geometry the restyle gave every primitive — the one floating
 * fill, the radius cap, the ring, and a footer whose buttons wrap instead of
 * overflowing a phone.
 */

const TITLE = 'Delete the thread?';
const MESSAGE = 'The thread and its messages are removed. This cannot be undone.';

/**
 * The dialog is opened by a button, the way a page opens it. The harness owns
 * the open state, so an escape really closes it: a dialog left open would
 * keep its focus trap armed and nothing could leave it.
 */
function Harness({
  initialOpen = false,
  onOpenChange,
  focusConfirm,
  confirmLabel,
}: {
  initialOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
  focusConfirm?: boolean;
  confirmLabel?: string;
}) {
  const [open, setOpen] = React.useState(initialOpen);
  const handleOpenChange = (next: boolean) => {
    setOpen(next);
    onOpenChange?.(next);
  };
  return (
    <>
      <button type="button" onClick={() => handleOpenChange(true)}>
        Open
      </button>
      <ConfirmDialog
        open={open}
        title={TITLE}
        message={MESSAGE}
        confirmLabel={confirmLabel}
        focusConfirm={focusConfirm}
        onOpenChange={handleOpenChange}
        onConfirm={() => {}}
      />
    </>
  );
}

const panel = () => document.querySelector('[role="dialog"]');

const classOf = (el: Element | null) => el?.getAttribute('class') ?? '';

describe('the confirmation dialog', () => {
  it('wears the one floating fill, the token shadow and the capped radius', () => {
    render(<Harness initialOpen onOpenChange={() => {}} />);
    const dialog = panel();
    expect(dialog).not.toBeNull();
    const classes = classOf(dialog);
    expect(classes).toContain('bg-(--color-bg-elevated)');
    expect(classes).toContain('shadow-(--shadow-elevated)');
    expect(classes).toContain('rounded-[var(--radius-overlay)]');
  });

  it('gives its buttons the control geometry, the ring and the mono face', () => {
    render(<Harness initialOpen onOpenChange={() => {}} />);
    const cancel = screen.getByRole('button', { name: 'Cancel' });
    const confirm = screen.getByRole('button', { name: 'Delete' });
    for (const button of [cancel, confirm]) {
      const classes = classOf(button);
      expect(classes).toContain(CONTROL_RADIUS);
      expect(classes).toContain(MONO_CHROME);
      expect(classes).toContain(FOCUS_RING);
      // A long label wraps inside its own button rather than pushing the row
      // past the edge of a phone.
      expect(classes).toContain('whitespace-normal');
      expect(classes).toContain('break-words');
      expect(classes).toContain('min-w-0');
    }
  });

  it('wraps its footer so the buttons never overflow', () => {
    render(<Harness initialOpen onOpenChange={() => {}} />);
    const footer = screen.getByRole('button', { name: 'Cancel' }).parentElement;
    expect(classOf(footer)).toContain('flex-wrap');
    expect(classOf(footer)).toContain('gap-2');
  });

  it('keeps the accessible names it always had', () => {
    render(<Harness initialOpen onOpenChange={() => {}} confirmLabel="Delete thread" />);
    expect(screen.getByRole('button', { name: 'Delete thread' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
    expect(screen.getByText(TITLE)).toBeInTheDocument();
    expect(screen.getByText(MESSAGE)).toBeInTheDocument();
  });

  it('adds no test id of its own', () => {
    render(<Harness initialOpen onOpenChange={() => {}} />);
    // The dialog never carried one; a test id added by the restyle would be a
    // new contract nobody asked for.
    expect(panel()?.querySelectorAll('[data-testid]')).toHaveLength(0);
  });

  it('opens on the confirm button when it is asked to', async () => {
    render(<Harness initialOpen onOpenChange={() => {}} focusConfirm />);
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Delete' }));
  });
});

describe('the focus trap', () => {
  it('keeps focus inside the dialog, wrapping at both ends', async () => {
    const user = userEvent.setup();
    render(<Harness initialOpen onOpenChange={() => {}} />);

    const cancel = screen.getByRole('button', { name: 'Cancel' });
    const confirm = screen.getByRole('button', { name: 'Delete' });
    const close = screen.getByRole('button', { name: 'Close' });

    // Focus starts inside the dialog.
    expect(panel()?.contains(document.activeElement)).toBe(true);

    // Tab from the last focusable wraps to the first, and Shift+Tab from the
    // first wraps to the last: the trap closes behind the user.
    close.focus();
    await user.tab();
    expect(document.activeElement).toBe(cancel);

    cancel.focus();
    await user.tab({ shift: true });
    expect(document.activeElement).toBe(close);

    // A walk through the whole order never leaves the dialog.
    for (let i = 0; i < 8; i += 1) {
      await user.tab();
      expect(panel()?.contains(document.activeElement)).toBe(true);
    }
    expect(confirm).toBeInTheDocument();
  });
});

describe('the escape', () => {
  it('closes the dialog and hands focus back to whatever opened it', async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    render(<Harness onOpenChange={onOpenChange} />);

    // Opened from the keyboard: a pointer opening it would (rightly) leave
    // the focus where the click left it, and the restore under test is the
    // keyboard one.
    const opener = screen.getByRole('button', { name: 'Open' });
    opener.focus();
    await user.keyboard('{Enter}');
    expect(panel()).not.toBeNull();
    expect(panel()?.contains(document.activeElement)).toBe(true);

    await user.keyboard('{Escape}');

    expect(onOpenChange).toHaveBeenCalledWith(false);
    // Focus goes back where it came from, not to the top of the document.
    // Radix restores it a tick after the panel unmounts, so the wait is part
    // of the contract rather than a race.
    await waitFor(() => expect(document.activeElement).toBe(opener));
  });

  it('does not close on an escape that another layer already handled', async () => {
    const onOpenChange = vi.fn();
    render(<Harness initialOpen onOpenChange={onOpenChange} />);

    // A key the layer below the dialog owns: Radix only closes when the
    // event was not already dealt with.
    await act(async () => {
      screen.getByRole('button', { name: 'Delete' }).focus();
      const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
      event.preventDefault();
      screen.getByRole('button', { name: 'Delete' }).dispatchEvent(event);
    });

    expect(onOpenChange).not.toHaveBeenCalledWith(false);
    expect(panel()).not.toBeNull();
  });
});
