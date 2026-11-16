/**
 * S03 palette screenshot matrix over /inbox for idle, dsp, approve args, zzz.
 */
import { test, expect } from '../fixtures.js';
import { shootMatrix } from './matrix.js';

async function openPalette(page, query) {
  // Blur any focused field, then fire the same KeyK chord PaletteHost listens for.
  await page.locator('body').click({ position: { x: 2, y: 2 }, force: true });
  await page.evaluate(() => {
    document.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'k',
        code: 'KeyK',
        ctrlKey: true,
        bubbles: true,
        cancelable: true,
      }),
    );
  });
  const input = page.getByTestId('tn-palette-input');
  await expect(input).toBeVisible({ timeout: 10_000 });
  if (query) {
    // Rows rebuild asynchronously after a keystroke. Wait for the count to
    // leave its idle value, or the shot shows the unfiltered list.
    const count = page.getByTestId('tn-palette-count');
    await expect(count).not.toHaveText('');
    const idle = (await count.textContent()) ?? '';
    await input.fill(query);
    await expect(count).not.toHaveText(idle);
  }
}

test.describe('terminal-native palette', () => {
  test('matrix: idle / dsp / approve args / zzz', async ({ page }) => {
    // 4 states × 8 matrix cells; keep headroom above the ~1–2 min warm run.
    test.setTimeout(300_000);

    // shootMatrix opens a fresh page per cell, and page routes do not carry
    // over, so the overrides go to every cell through mockOverrides.
    const overrides = {
      'GET /features': { features: [] },
      'GET /autonomy/overview': {
        counts: { attention: 0, running: 0, queued: 0, completed: 0, failed: 0 },
        sources: 0,
        enabled_sources: 0,
      },
      'GET /autonomy/sources': { sources: [] },
      'GET /autonomy/tickets': { tickets: [] },
      'GET /native-agents': { runtimes: [] },
      'GET /dispatch/status/active': [
        { id: '7f3a91c2', phase: 'awaiting_human' },
        { id: '4b8e0d17', phase: 'secondary_review' },
        { id: '91cc6a40', phase: 'g2' },
        { id: '7c77be05', phase: 'fixing' },
      ],
    };

    const states = [
      { name: 'idle', query: '' },
      { name: 'dsp', query: 'dsp' },
      { name: 'approve-args', query: 'approve ' },
      { name: 'zzz', query: 'zzz' },
    ];

    for (const state of states) {
      await shootMatrix(page, {
        slice: 'palette',
        name: state.name,
        path: '/inbox',
        mockOverrides: overrides,
        prepare: async (p) => {
          await openPalette(p, state.query);
          if (state.query === 'zzz') {
            await expect(p.getByTestId('tn-palette-empty')).toBeVisible();
          }
          // Long zh-CN hints must wrap, not clip the dialog at 390.
          const box = await p.getByTestId('tn-palette').boundingBox();
          expect(box).toBeTruthy();
          if (box && p.viewportSize()?.width === 390) {
            expect(box.width).toBeLessThanOrEqual(390);
          }
        },
      });
    }
  });
});
