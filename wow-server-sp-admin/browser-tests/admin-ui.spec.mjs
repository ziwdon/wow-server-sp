import { test, expect } from '@playwright/test';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const axeSource = require.resolve('axe-core/axe.min.js');

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const NativeEventSource = window.EventSource;
    window.EventSource = function RecordingEventSource(...args) {
      const source = new NativeEventSource(...args);
      for (const type of ['progress', 'done']) {
        source.addEventListener(type, (event) => {
          const events = JSON.parse(sessionStorage.getItem('browserHarnessSse') || '[]');
          events.push({ type, data: event.data });
          sessionStorage.setItem('browserHarnessSse', JSON.stringify(events));
        });
      }
      return source;
    };
    window.EventSource.prototype = NativeEventSource.prototype;
  });
});

test('keyboard settings selection, mobile dialog focus, and filters work together', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/settings');

  const filterToggle = page.getByRole('button', { name: /filters/i });
  await filterToggle.click();
  await expect(filterToggle).toHaveAttribute('aria-expanded', 'true');

  const search = page.getByRole('textbox', { name: 'Search settings' });
  await search.fill('MinRandom');
  const detailButton = page.getByRole('button', { name: /AiPlayerbot\.MinRandomBots/ });
  await expect(detailButton).toBeVisible();
  await detailButton.focus();
  await page.keyboard.press('Enter');

  const detail = page.locator('#key-detail');
  await expect(detail).toHaveAttribute('role', 'dialog');
  await expect(detail.getByRole('button', { name: /back/i })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(detail).not.toHaveClass(/mobile-visible/);
  await expect(detailButton).toBeFocused();
});

test('HTMX polling swaps every server status in the status matrix', async ({ page, request }) => {
  await page.goto('/');
  await page.waitForFunction(() => window.htmx !== undefined);

  for (const state of ['running', 'exited', 'created', 'paused', 'restarting', 'dead', 'removing', 'missing', 'unknown']) {
    await request.post(`/__fixture/status/${state}`);
    await page.locator('#status').evaluate((element) => window.htmx.trigger(element, 'refresh'));
    await expect(page.locator(`#status .status-${state}`)).toHaveText(state.toUpperCase());
  }
});

for (const action of ['apply', 'rollback']) {
  test(`${action} waits for real SSE progress before a successful redirect`, async ({ page }) => {
    await page.goto('/settings');

    if (action === 'apply') {
      const input = page.locator('.key-input[data-key="AiPlayerbot.MinRandomBots"]');
      await input.fill('75');
      await expect(page.getByRole('button', { name: /^Apply/ })).toBeEnabled();
      await page.getByRole('button', { name: /^Apply/ }).click();
      await page.getByRole('button', { name: /Apply & restart/ }).click();
    } else {
      page.once('dialog', (dialog) => dialog.accept());
      await page.getByRole('button', { name: /rollback last apply/i }).click();
    }

    await expect(page).toHaveURL(/\/$/);
    const events = await page.evaluate(() => JSON.parse(sessionStorage.getItem('browserHarnessSse') || '[]'));
    expect(events.map((event) => event.type)).toEqual(expect.arrayContaining(['progress', 'done']));
  });
}

test('fetch failures leave the page available and expose recovery guidance', async ({ page }) => {
  await page.goto('/settings');
  await page.route('**/api/settings/rollback', (route) => route.abort('failed'));
  page.once('dialog', (dialog) => dialog.accept());

  await page.getByRole('button', { name: /rollback last apply/i }).click();
  await expect(page.getByRole('status')).toContainText('Rollback: Server unreachable');
  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByRole('button', { name: /rollback last apply/i })).toBeEnabled();
});

test('axe and HTML checks preserve core document and control semantics', async ({ page }) => {
  await page.goto('/settings');
  await page.addScriptTag({ path: axeSource });

  const violations = await page.evaluate(async () => {
    const result = await window.axe.run(document, {
      runOnly: {
        type: 'rule',
        values: ['aria-allowed-attr', 'aria-valid-attr-value', 'button-name', 'document-title', 'html-has-lang', 'label', 'landmark-one-main'],
      },
    });
    return result.violations.map(({ id, nodes }) => ({ id, nodes: nodes.length }));
  });
  expect(violations).toEqual([]);
  await expect(page.locator('main')).toHaveCount(1);
  await expect(page.locator('.key-row-select')).toHaveCount(1);
});
