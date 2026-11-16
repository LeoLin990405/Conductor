/** Local Preferences tab: it exposes only settings that affect this device. */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const h = vi.hoisted(() => ({ clearPreferences: vi.fn() }));

vi.mock('@/lib/dashboardApi/api', () => ({
  clearPreferences: h.clearPreferences,
}));

const prefsStub = { preferences: { agent_preference: { output_format: 'html' } }, isLoading: false };
vi.mock('@/hooks/usePreferences', () => ({ usePreferences: () => prefsStub }));

const mutationStub = { mutateAsync: vi.fn(async () => ({})) };
vi.mock('@/hooks/useUpdatePreferences', () => ({ useUpdatePreferences: () => mutationStub }));

const toastStub = { toast: vi.fn() };
vi.mock('@/components/ui/use-toast', () => ({ useToast: () => toastStub }));

vi.mock('../panels/AutoApproveSettings', () => ({
  AutoApproveSettings: () => <div data-testid="auto-approve-settings" />,
}));

import { PreferencesTab } from '../panels/PreferencesTab';

function renderPreferencesTab() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/settings?tab=preferences']}>
        <PreferencesTab />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  h.clearPreferences.mockReset();
  h.clearPreferences.mockImplementation(async () => ({ success: true }));
});

describe('Settings: local preferences', () => {
  it('shows only the local settings groups', () => {
    renderPreferencesTab();
    expect(screen.getByText('Answer format')).toBeInTheDocument();
    expect(screen.getByText('Agent behavior')).toBeInTheDocument();
    expect(screen.getByText('Local data')).toBeInTheDocument();
    expect(screen.getByTestId('auto-approve-settings')).toBeInTheDocument();
  });

  it('resets preferences through the confirmation dialog', async () => {
    renderPreferencesTab();
    fireEvent.click(screen.getByRole('button', { name: 'Reset Preferences' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Reset Preferences' }));

    await waitFor(() => expect(h.clearPreferences).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });
});
