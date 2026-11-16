import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { PageHeader } from '../PageHeader';

/**
 * Refresh is the page's one manual re-read of the section on screen: the
 * lists hold their answers for a while so a rail switch spends no request,
 * and this is how a user asks again after changing something elsewhere.
 */

function header(over: Partial<React.ComponentProps<typeof PageHeader>> = {}) {
  return (
    <PageHeader
      section="plugins"
      view="yours"
      summary={null}
      query=""
      hasCatalog={false}
      refreshing={false}
      onRefresh={vi.fn()}
      onViewChange={vi.fn()}
      onQueryChange={vi.fn()}
      onAdd={vi.fn()}
      {...over}
    />
  );
}

describe('PageHeader refresh', () => {
  it('asks for a re-read when pressed', () => {
    const onRefresh = vi.fn();
    render(header({ onRefresh }));

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it('holds and says so while the re-read runs', () => {
    const onRefresh = vi.fn();
    render(header({ refreshing: true, onRefresh }));

    const button = screen.getByRole('button', { name: 'Refreshing…' });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(onRefresh).not.toHaveBeenCalled();
  });

  it('is there on an Accounts section too, which has no search or Add', () => {
    render(header({ section: 'secrets' }));
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeInTheDocument();
    expect(screen.queryByRole('searchbox')).toBeNull();
  });
});
