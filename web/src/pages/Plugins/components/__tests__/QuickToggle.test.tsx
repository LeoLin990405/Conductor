import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { QuickToggle } from '../pluginsPage/QuickToggle';

/**
 * The cell's one-click switch: one press, no menu. While the write is in
 * flight it holds, so the same row cannot be sent two opposite ways at once.
 */

describe('QuickToggle', () => {
  it('switches on one press', () => {
    const onToggle = vi.fn();
    render(<QuickToggle on={false} pending={false} label="Switch globex on" onToggle={onToggle} />);

    fireEvent.click(screen.getByRole('button', { name: 'Switch globex on' }));
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it('carries the verb for the state it is in', () => {
    render(<QuickToggle on pending={false} label="Switch globex off" onToggle={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Switch globex off' })).toBeInTheDocument();
  });

  it('holds while the write is in flight', () => {
    const onToggle = vi.fn();
    render(<QuickToggle on pending label="Switch globex off" onToggle={onToggle} />);

    const button = screen.getByRole('button', { name: 'Switch globex off' });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(onToggle).not.toHaveBeenCalled();
  });
});
