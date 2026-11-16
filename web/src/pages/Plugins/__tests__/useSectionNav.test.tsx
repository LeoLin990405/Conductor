import { describe, it, expect } from 'vitest';
import { act } from '@testing-library/react';
import { useLocation } from 'react-router';
import { renderHookWithProviders } from '@/test/utils';
import { useSectionNav } from '../hooks/useSectionNav';

function renderNav(route: string) {
  return renderHookWithProviders(
    () => ({ nav: useSectionNav(), search: new URLSearchParams(useLocation().search) }),
    { route },
  );
}

describe('a link from one section of the Plugins page to another', () => {
  // A skill's plugin link carried the skill search onto the Plugins tab, which
  // filtered the plugin list by a skill's name: usually to nothing, with the
  // plugin's page open over an empty list.
  it('leaves the search behind when it opens a plugin from the Skills tab', () => {
    const { result } = renderNav('/plugins?tab=skills&q=recap');

    act(() => result.current.nav.openDetail({ kind: 'plugin', name: 'ledger' }));

    expect(result.current.search.get('tab')).toBe('plugins');
    expect(result.current.search.get('detail')).toBe('plugin:ledger');
    expect(result.current.search.has('q')).toBe(false);
  });

  // The pill and the order were chosen over the Skills list's rows; carried
  // onto Plugins they narrow a list the user did not ask a question about,
  // with the plugin's page open over it.
  it('leaves the state pill and the order behind too', () => {
    const { result } = renderNav('/plugins?tab=skills&q=recap&state=off&sort=name');

    act(() => result.current.nav.openDetail({ kind: 'plugin', name: 'ledger' }));

    expect(result.current.search.get('tab')).toBe('plugins');
    expect(result.current.search.has('state')).toBe(false);
    expect(result.current.search.has('sort')).toBe(false);
  });

  it('keeps them when the link stays in the section', () => {
    const { result } = renderNav('/plugins?tab=plugins&state=off&sort=name');

    act(() => result.current.nav.openDetail({ kind: 'plugin', name: 'ledger' }));

    expect(result.current.search.get('tab')).toBe('plugins');
    expect(result.current.search.get('state')).toBe('off');
    expect(result.current.search.get('sort')).toBe('name');
  });
});
