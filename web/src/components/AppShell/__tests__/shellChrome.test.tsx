import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MemoryRouter } from 'react-router';

const openPalette = vi.fn();
vi.mock('@/features/palette/PaletteHost', () => ({
  openPalette: (...args: unknown[]) => openPalette(...args),
}));
vi.mock('../../Sidebar/AccountMenu', () => ({ default: () => null }));

import AppNavSidebar from '../AppNavSidebar';
import BottomTabBar from '../../BottomTabBar/BottomTabBar';
import { NAV_ITEMS, SETTINGS_ITEM } from '../../nav/navItems';

function renderAt(ui: React.ReactElement, path = '/inbox') {
  return render(<MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>);
}

describe('shell palette trigger', () => {
  beforeEach(() => openPalette.mockClear());

  it('the rail trigger calls openPalette with itself as the opener', () => {
    renderAt(<AppNavSidebar />);
    const trigger = screen.getByTestId('shell-palette-trigger');
    expect(trigger).toHaveAttribute('aria-keyshortcuts', expect.stringContaining('Control+K'));
    fireEvent.click(trigger);
    expect(openPalette).toHaveBeenCalledTimes(1);
    expect(openPalette.mock.calls[0][0]).toBe(trigger);
  });

  it('the tab-bar trigger calls openPalette with itself as the opener', () => {
    renderAt(<BottomTabBar />);
    const trigger = screen.getByTestId('tab-palette-trigger');
    fireEvent.click(trigger);
    expect(openPalette).toHaveBeenCalledTimes(1);
    expect(openPalette.mock.calls[0][0]).toBe(trigger);
  });
});

describe('shell nav rows', () => {
  it('the rail renders every destination, Settings included, as a focusable button', () => {
    renderAt(<AppNavSidebar />);
    const keys = screen
      .getAllByTestId('app-nav-item')
      .map((el) => el.getAttribute('data-nav-key'));
    expect(keys).toEqual([...NAV_ITEMS, SETTINGS_ITEM].map((item) => item.key));
  });

  it('the rail marks the active destination', () => {
    renderAt(<AppNavSidebar />, '/dispatch/abc');
    const active = screen
      .getAllByTestId('app-nav-item')
      .filter((el) => el.getAttribute('aria-current') === 'page');
    expect(active.map((el) => el.getAttribute('data-nav-key'))).toEqual(['/dispatch']);
  });

  it('the tab bar shows a text label under each icon', () => {
    renderAt(<BottomTabBar />);
    for (const el of screen.getAllByTestId('tab-nav-item')) {
      expect(el.querySelector('.bottom-tab-label')?.textContent?.trim()).toBeTruthy();
    }
  });
});
