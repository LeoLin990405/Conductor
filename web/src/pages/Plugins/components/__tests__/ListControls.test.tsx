import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import { ListControls, type StateCounts } from '../ListControls';

// Inline dropdown, as in ScopeControl.test: jsdom does not drive Radix portals.
vi.mock('@/components/ui/dropdown-menu', () => ({
  DropdownMenu: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuTrigger: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  DropdownMenuContent: ({ children }: { children: React.ReactNode }) => (
    <div role="menu">{children}</div>
  ),
  DropdownMenuItem: ({ children }: { children: React.ReactNode }) => (
    <button role="menuitem">{children}</button>
  ),
  DropdownMenuCheckboxItem: ({
    children,
    checked,
    onSelect,
  }: {
    children: React.ReactNode;
    checked?: boolean;
    onSelect?: (e?: { preventDefault: () => void }) => void;
  }) => (
    <button
      role="menuitemcheckbox"
      aria-checked={checked}
      onClick={() => onSelect?.({ preventDefault: () => {} })}
    >
      {children}
    </button>
  ),
}));

const COUNTS: StateCounts = { all: 6, on: 3, off: 3, attention: 1 };

const SORTS = [
  { key: 'recommended' as const, labelKey: 'plugins.sort.recommended' },
  { key: 'name' as const, labelKey: 'plugins.sort.name' },
  { key: 'recent' as const, labelKey: 'plugins.sort.recent' },
];

function controls(over: Partial<React.ComponentProps<typeof ListControls>> = {}) {
  return (
    <ListControls
      counts={COUNTS}
      stateFilter="all"
      onStateFilterChange={vi.fn()}
      selecting={false}
      onStartSelect={vi.fn()}
      shownCount={COUNTS.all}
      allShownSelected={false}
      onSelectShown={vi.fn()}
      {...over}
    />
  );
}

describe('ListControls order', () => {
  it('offers no order control on a list without one', () => {
    render(controls());
    expect(screen.queryByRole('button', { name: /Sort by/ })).toBeNull();
  });

  it('names the order in force and offers the others', () => {
    render(controls({ sort: { value: 'name', options: SORTS, onChange: vi.fn() } }));

    const trigger = screen.getByRole('button', { name: 'Sort by: Name' });
    expect(within(trigger).getByText('Name')).toBeInTheDocument();
    expect(screen.getByRole('menu')).toBeInTheDocument();
    const items = screen.getAllByRole('menuitemcheckbox');
    expect(items.map((item) => item.textContent)).toEqual(['Recommended', 'Name', 'Recently added']);
    expect(items[1]).toHaveAttribute('aria-checked', 'true');
  });

  it('asks for another order when one is picked', () => {
    const onChange = vi.fn();
    render(controls({ sort: { value: 'recommended', options: SORTS, onChange } }));

    fireEvent.click(screen.getByRole('menuitemcheckbox', { name: 'Recently added' }));
    expect(onChange).toHaveBeenCalledWith('recent');
  });

  it('does not ask again for the order already in force', () => {
    const onChange = vi.fn();
    render(controls({ sort: { value: 'recent', options: SORTS, onChange } }));

    fireEvent.click(screen.getByRole('menuitemcheckbox', { name: 'Recently added' }));
    expect(onChange).not.toHaveBeenCalled();
  });
});
