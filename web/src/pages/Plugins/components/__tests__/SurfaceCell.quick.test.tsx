import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { CellModeProvider, SurfaceCell } from '../surface/Surface';

/**
 * A cell can carry one verb of its own beside the trailing state (a plugin's
 * switch). It has to be inside the cell's end slot — above the stretched open
 * button, which would otherwise swallow the press — and it goes away with the
 * menu in select mode, where the whole cell is a checkbox.
 */

function cell(over: Partial<React.ComponentProps<typeof SurfaceCell>> = {}) {
  return <SurfaceCell name="globex" onOpen={vi.fn()} {...over} />;
}

describe('SurfaceCell quick verb', () => {
  it('draws the verb in the end slot', () => {
    render(
      cell({
        quick: (
          <button type="button" aria-label="Switch globex on">
            power
          </button>
        ),
        trailing: <span>Off</span>,
      }),
    );

    const quick = screen.getByRole('button', { name: 'Switch globex on' });
    expect(quick.closest('.surface-cell-end')).not.toBeNull();
    expect(screen.getByText('Off')).toBeInTheDocument();
  });

  it('keeps the verb pressable above the open button', () => {
    const onOpen = vi.fn();
    const onQuick = vi.fn();
    render(
      cell({
        onOpen,
        quick: (
          <button type="button" aria-label="Switch globex on" onClick={onQuick}>
            power
          </button>
        ),
      }),
    );

    fireEvent.click(screen.getByRole('button', { name: 'Switch globex on' }));
    expect(onQuick).toHaveBeenCalledTimes(1);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('drops the verb in select mode, where the cell is the checkbox', () => {
    render(
      <CellModeProvider value={{ select: { checked: false, onToggle: vi.fn() } }}>
        <SurfaceCell
          name="globex"
          quick={<button type="button" aria-label="Switch globex on">power</button>}
          trailing={<span>Off</span>}
        />
      </CellModeProvider>,
    );

    expect(screen.queryByRole('button', { name: 'Switch globex on' })).toBeNull();
    expect(screen.getByRole('checkbox', { name: 'globex' })).toBeInTheDocument();
  });
});
