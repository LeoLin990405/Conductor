import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router';
import { PaletteHost, openPalette } from '../PaletteHost';
import { __resetComposerModeForTests, getComposerMode } from '../composerMode';

const listActiveDispatches = vi.hoisted(() =>
  vi.fn(async () => [
    { id: '7f3a91c2', phase: 'awaiting_human' },
    { id: '4b8e0d17', phase: 'secondary_review' },
    { id: '91cc6a40', phase: 'g2' },
  ]),
);
const approveDispatch = vi.hoisted(() => vi.fn(async () => undefined));
const rejectDispatch = vi.hoisted(() => vi.fn(async () => undefined));

vi.mock('@/api/dispatch', () => ({
  listActiveDispatches,
  approveDispatch,
  rejectDispatch,
}));

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
}

function renderHost(initial = '/inbox') {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <LocationProbe />
      <button type="button" data-testid="opener">
        opener
      </button>
      <PaletteHost />
    </MemoryRouter>,
  );
}

async function openWithCtrlK() {
  const opener = screen.getByTestId('opener');
  opener.focus();
  await act(async () => {
    fireEvent.keyDown(document, { key: 'k', code: 'KeyK', ctrlKey: true });
  });
  await screen.findByTestId('tn-palette-input');
}

describe('CommandPalette keyboard', () => {
  beforeEach(() => {
    __resetComposerModeForTests();
    listActiveDispatches.mockClear();
    approveDispatch.mockClear();
    rejectDispatch.mockClear();
  });

  afterEach(() => {
    __resetComposerModeForTests();
  });

  it('Ctrl+K opens with the query focused', async () => {
    renderHost();
    await openWithCtrlK();
    expect(document.activeElement).toBe(screen.getByTestId('tn-palette-input'));
  });

  it('Tab inserts the highlighted token (+ space when an argument is needed); Shift+Tab is inert', async () => {
    const user = userEvent.setup();
    renderHost();
    await openWithCtrlK();
    const input = screen.getByTestId('tn-palette-input');
    await user.type(input, 'appr');
    await waitFor(() => expect(screen.getByRole('option', { selected: true }).textContent).toMatch(/approve/));
    fireEvent.keyDown(input, { key: 'Tab' });
    expect(input).toHaveValue('approve ');
    expect(document.activeElement).toBe(input);
    fireEvent.keyDown(input, { key: 'Tab', shiftKey: true });
    expect(input).toHaveValue('approve ');
  });

  it('Enter on approve with an offered id inserts; with none shows bilingual needs-id hint', async () => {
    const user = userEvent.setup();
    renderHost();
    await openWithCtrlK();
    const input = screen.getByTestId('tn-palette-input');
    await user.clear(input);
    await user.type(input, 'approve ');
    await waitFor(() => expect(screen.getByText('7f3a91c2')).toBeInTheDocument());
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(input).toHaveValue('approve 7f3a91c2'));
    expect(screen.getByTestId('location')).toHaveTextContent('/inbox');
    expect(approveDispatch).not.toHaveBeenCalled();
    expect(rejectDispatch).not.toHaveBeenCalled();

    await user.clear(input);
    await user.type(input, 'approve 4');
    await waitFor(() => expect(screen.getByTestId('tn-palette-empty')).toBeInTheDocument());
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => {
      const status = screen.getByTestId('tn-palette-status').textContent || '';
      expect(status).toMatch(/approve needs a dispatch id/);
      expect(status).toMatch(/approve 需要一个 dispatch id/);
    });
    expect(approveDispatch).not.toHaveBeenCalled();
    expect(rejectDispatch).not.toHaveBeenCalled();
  });

  it('complete approve/reject open /dispatch/:id?confirm=… and never call the API', async () => {
    const user = userEvent.setup();
    renderHost();
    await openWithCtrlK();
    const input = screen.getByTestId('tn-palette-input');

    await user.clear(input);
    await user.type(input, 'approve 7f3a91c2');
    await waitFor(() => expect(screen.getByText('7f3a91c2')).toBeInTheDocument());
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() =>
      expect(screen.getByTestId('location')).toHaveTextContent('/dispatch/7f3a91c2?confirm=approve'),
    );
    expect(screen.queryByTestId('tn-palette')).not.toBeInTheDocument();
    expect(approveDispatch).not.toHaveBeenCalled();
    expect(rejectDispatch).not.toHaveBeenCalled();

    openPalette(screen.getByTestId('opener'));
    const again = await screen.findByTestId('tn-palette-input');
    await user.clear(again);
    await user.type(again, 'reject 7f3a91c2');
    await waitFor(() => expect(screen.getByText('7f3a91c2')).toBeInTheDocument());
    fireEvent.keyDown(again, { key: 'Enter' });
    await waitFor(() =>
      expect(screen.getByTestId('location')).toHaveTextContent('/dispatch/7f3a91c2?confirm=reject'),
    );
    expect(approveDispatch).not.toHaveBeenCalled();
    expect(rejectDispatch).not.toHaveBeenCalled();
  });

  it('Enter on a complete form navigates; Esc restores the opener', async () => {
    const user = userEvent.setup();
    renderHost();
    const opener = screen.getByTestId('opener');
    opener.focus();
    openPalette(opener);
    const input = await screen.findByTestId('tn-palette-input');
    await user.type(input, 'status');
    await waitFor(() => expect(screen.getByText('status')).toBeInTheDocument());
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(screen.queryByTestId('tn-palette')).not.toBeInTheDocument());

    openPalette(opener);
    await screen.findByTestId('tn-palette-input');
    fireEvent.keyDown(screen.getByTestId('tn-palette-input'), { key: 'Escape' });
    await waitFor(() => expect(document.activeElement).toBe(opener));
  });

  it('IME-composing Enter never acts', async () => {
    const user = userEvent.setup();
    renderHost();
    await openWithCtrlK();
    const input = screen.getByTestId('tn-palette-input');
    await user.type(input, 'status');
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true, keyCode: 229 });
    expect(screen.getByTestId('tn-palette')).toBeInTheDocument();
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 229 });
    expect(screen.getByTestId('tn-palette')).toBeInTheDocument();
  });

  it('Ctrl+Down / Ctrl+Up move query ↔ list; j/k insert unless Alt+N', async () => {
    const user = userEvent.setup();
    renderHost();
    await openWithCtrlK();
    const input = screen.getByTestId('tn-palette-input');
    fireEvent.keyDown(input, { key: 'ArrowDown', ctrlKey: true });
    expect(document.activeElement).toBe(screen.getByTestId('tn-palette-list'));
    fireEvent.keyDown(screen.getByTestId('tn-palette-list'), { key: 'ArrowUp', ctrlKey: true });
    expect(document.activeElement).toBe(input);

    await user.type(input, 'j');
    expect(input).toHaveValue('j');
    fireEvent.keyDown(input, { key: 'n', code: 'KeyN', altKey: true });
    expect(screen.getByTestId('tn-palette-nav')).toBeInTheDocument();
    const before = (input as HTMLInputElement).value;
    fireEvent.keyDown(input, { key: 'j' });
    expect(input).toHaveValue(before);
  });

  it('clicking a row activates it like Enter; Alt+M cycles composer mode', async () => {
    renderHost();
    await openWithCtrlK();
    await waitFor(() => expect(screen.getAllByRole('option').length).toBeGreaterThan(2));
    const approve = screen.getByText('approve');
    fireEvent.click(approve.closest('li')!);
    await waitFor(() => expect(screen.getByTestId('tn-palette-input')).toHaveValue('approve '));

    expect(getComposerMode()).toBe('auto');
    fireEvent.keyDown(document, { key: 'm', code: 'KeyM', altKey: true });
    expect(getComposerMode()).toBe('dispatch');
    fireEvent.keyDown(document, { key: 'm', code: 'KeyM', altKey: true });
    expect(getComposerMode()).toBe('read-only');
    fireEvent.keyDown(document, { key: 'm', code: 'KeyM', altKey: true });
    expect(getComposerMode()).toBe('auto');
  });

  it('zzz shows no command matches bilingual empty state', async () => {
    const user = userEvent.setup();
    renderHost();
    await openWithCtrlK();
    await user.type(screen.getByTestId('tn-palette-input'), 'zzz');
    const empty = await screen.findByTestId('tn-palette-empty');
    expect(empty.textContent).toMatch(/no command matches/);
    expect(empty.textContent).toMatch(/没有匹配的命令/);
  });
});
