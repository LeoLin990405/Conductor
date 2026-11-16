/**
 * Super-agent app shell: Inbox is the entry, Tasks/Sources are primary pages.
 */
import { mockAPI, test, expect } from './fixtures.js';

test.describe('Super-agent shell', () => {
  test('shows the inbox and source setup', async ({ page }) => {
    await mockAPI(page, {
      'GET /features': { features: [] },
      'GET /autonomy/overview': {
        counts: { attention: 0, running: 0, queued: 0, completed: 0, failed: 0 },
        sources: 0,
        enabled_sources: 0,
      },
      'GET /autonomy/sources': { sources: [] },
      'GET /autonomy/tickets': { tickets: [] },
      'GET /native-agents': { runtimes: [] },
    });
    await page.goto('/inbox');
    await expect(page.getByRole('heading', { name: 'Inbox' })).toBeVisible();
    await expect(page.getByText(/No tasks yet/i)).toBeVisible();
    await page.goto('/sources');
    await expect(page.getByText('Add GitHub Source')).toBeVisible();
    await expect(page.getByPlaceholder('owner/repo')).toBeVisible();
  });

  test('links a source to its filtered tasks', async ({ page }) => {
    const now = '2026-01-01T00:00:00Z';
    const source = {
      source_id: 'src-1',
      user_id: 'u-1',
      kind: 'github',
      config: {
        repository: 'acme/widgets',
        secret_name: 'GITHUB_TOKEN',
        workspace_id: 'ws-1',
        allowed_logins: [],
        poll_seconds: 60,
      },
      state: {},
      enabled: true,
      created_at: now,
      updated_at: now,
    };
    const ticket = {
      ticket_id: 't-1',
      user_id: 'u-1',
      workspace_id: 'ws-1',
      title: 'Fix the widget',
      scope: 'Fix the widget',
      status: 'completed',
      mandate: { source_id: 'src-1' },
      origin_trigger_id: 'tr-1',
      origin_thread_key: 'github:acme/widgets:12',
      router_thread_id: 'r-1',
      worker_thread_id: 'w-1',
      worker_run_id: 'run-1',
      worker_provider: '',
      worker_model: '',
      attempts: 1,
      attention: null,
      summary: 'Done.',
      error: '',
      created_at: now,
      updated_at: now,
    };
    await mockAPI(page, {
      'GET /features': { features: [] },
      'GET /autonomy/overview': {
        counts: { attention: 0, running: 0, queued: 0, completed: 1, failed: 0 },
        sources: 1,
        enabled_sources: 1,
      },
      'GET /autonomy/sources': { sources: [source] },
      'GET /autonomy/schedules': { schedules: [] },
      'GET /autonomy/tickets': { tickets: [ticket] },
      'GET /native-agents': { runtimes: [] },
    });
    await page.goto('/sources');
    await expect(page.getByText('acme/widgets')).toBeVisible();
    await page.getByRole('button', { name: 'Tickets' }).click();
    await expect(page).toHaveURL(/\/tasks\?source=src-1$/);
    await expect(page.getByText(/Showing Tickets from one Source/)).toBeVisible();
    await expect(page.getByText('Fix the widget')).toBeVisible();
  });

  test('edits a scheduled task', async ({ page }) => {
    const now = '2026-01-01T00:00:00Z';
    const schedule = {
      schedule_id: '11111111-1111-4111-8111-111111111111',
      user_id: 'u-1',
      workspace_id: 'ws-1',
      title: 'Morning review',
      prompt: 'Review the repository',
      cron_expression: '0 9 * * 1-5',
      timezone: 'UTC',
      enabled: true,
      worker_provider: '',
      worker_model: '',
      next_run_at: now,
      last_run_at: null,
      last_error: '',
      created_at: now,
      updated_at: now,
    };
    await mockAPI(page, {
      'GET /features': { features: [] },
      'GET /autonomy/overview': {
        counts: { attention: 0, running: 0, queued: 0, completed: 0, failed: 0 },
        sources: 0,
        enabled_sources: 0,
      },
      'GET /autonomy/sources': { sources: [] },
      'GET /autonomy/schedules': { schedules: [schedule] },
      'GET /autonomy/tickets': { tickets: [] },
      'GET /native-agents': { runtimes: [] },
    });
    await page.goto('/sources');
    await page.getByRole('button', { name: 'Edit' }).click();
    await expect(page.getByRole('heading', { name: 'Edit scheduled task' })).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Prompt' })).toHaveValue('Review the repository');
    await expect(page.getByRole('button', { name: 'Save changes' })).toBeVisible();
  });

  test('filters failed and blocked Tickets from the Inbox card', async ({ page }) => {
    const now = '2026-01-01T00:00:00Z';
    const base = {
      user_id: 'u-1',
      workspace_id: 'ws-1',
      scope: 'work',
      mandate: {},
      origin_trigger_id: 'tr-1',
      origin_thread_key: 'manual:u-1:1',
      router_thread_id: null,
      worker_thread_id: null,
      worker_run_id: null,
      worker_provider: '',
      worker_model: '',
      attempts: 1,
      attention: null,
      summary: '',
      error: '',
      created_at: now,
      updated_at: now,
    };
    const tickets = [
      { ...base, ticket_id: 't-failed', title: 'Failed job', status: 'failed' },
      { ...base, ticket_id: 't-blocked', title: 'Blocked job', status: 'blocked' },
      { ...base, ticket_id: 't-done', title: 'Done job', status: 'completed' },
    ];
    await mockAPI(page, {
      'GET /features': { features: [] },
      'GET /autonomy/overview': {
        counts: { attention: 0, running: 0, queued: 0, completed: 1, failed: 1, blocked: 1 },
        sources: 0,
        enabled_sources: 0,
      },
      'GET /autonomy/sources': { sources: [] },
      'GET /autonomy/tickets': { tickets },
      'GET /native-agents': { runtimes: [] },
    });
    await page.goto('/inbox');
    const card = page.getByRole('button', { name: /2 failed \/ blocked/ });
    await expect(card).toBeVisible();
    await card.click();
    await expect(page).toHaveURL(/\/tasks\?failed=1$/);
    await expect(page.getByText(/Showing failed and blocked Tickets/)).toBeVisible();
    await expect(page.getByText('Failed job')).toBeVisible();
    await expect(page.getByText('Blocked job')).toBeVisible();
    await expect(page.getByText('Done job')).not.toBeVisible();
  });

  test('keeps the retired product routes out of the shell', async ({ page }) => {
    await mockAPI(page, {
      'GET /features': { features: [] },
      'GET /autonomy/overview': {
        counts: { attention: 0, running: 0, queued: 0, completed: 0, failed: 0 },
        sources: 0,
        enabled_sources: 0,
      },
      'GET /autonomy/sources': { sources: [] },
      'GET /autonomy/tickets': { tickets: [] },
      'GET /native-agents': { runtimes: [] },
    });
    await page.goto('/projects');
    await expect(page).toHaveURL(/\/inbox$/);
  });
});
