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

test('incident links activate and focus App Events and refresh preserves the tab', async ({ page }) => {
  await page.goto('/?source=stats&app_event=EVT-BROWSER#logs');

  const logs = page.locator('#logs');
  const appEventsTab = logs.getByRole('tab', { name: 'App Events' });
  const incident = logs.locator('[data-incident-id="EVT-BROWSER"]');
  await expect(appEventsTab).toHaveClass(/active/);
  await expect(appEventsTab).toHaveAttribute('aria-selected', 'true');
  await expect(incident).toBeVisible();
  await expect(incident).toBeFocused();
  await expect(page).toHaveURL(/\?source=stats#logs$/);

  await logs.getByRole('button', { name: 'Refresh' }).click();
  await expect(appEventsTab).toHaveClass(/active/);
  await expect(logs.locator('#app-events-log')).toBeVisible();
});

test('refresh keeps a user-selected log tab after a successful incident deep-link', async ({ page }) => {
  await page.goto('/?app_event=EVT-BROWSER#logs');

  const logs = page.locator('#logs');
  await expect(logs.locator('[data-incident-id="EVT-BROWSER"]')).toBeFocused();
  const playerbotsTab = logs.getByRole('tab', { name: 'Playerbots.log' });
  await playerbotsTab.click();
  await logs.getByRole('button', { name: 'Refresh' }).click();

  await expect(playerbotsTab).toHaveClass(/active/);
  await expect(playerbotsTab).toHaveAttribute('aria-selected', 'true');
  await expect(logs.locator('#playerbots-log')).toBeVisible();
  await expect(logs.locator('#app-events-log')).toBeHidden();
});

test('Logs tabs use scoped roving focus and arrow, Home, and End navigation', async ({ page }) => {
  await page.goto('/#logs');

  const logs = page.locator('#logs');
  const tabs = logs.getByRole('tab');
  const serverTab = logs.getByRole('tab', { name: 'Server.log' });
  const playerbotsTab = logs.getByRole('tab', { name: 'Playerbots.log' });
  const appEventsTab = logs.getByRole('tab', { name: 'App Events' });
  await expect(tabs).toHaveCount(4);
  await expect(serverTab).toHaveAttribute('tabindex', '0');
  await expect(playerbotsTab).toHaveAttribute('tabindex', '-1');

  await serverTab.focus();
  await page.keyboard.press('ArrowRight');
  await expect(playerbotsTab).toBeFocused();
  await expect(playerbotsTab).toHaveAttribute('aria-selected', 'true');
  await expect(serverTab).toHaveAttribute('tabindex', '-1');
  await expect(playerbotsTab).toHaveAttribute('tabindex', '0');

  await page.keyboard.press('End');
  await expect(appEventsTab).toBeFocused();
  await expect(appEventsTab).toHaveAttribute('aria-selected', 'true');
  await page.keyboard.press('Home');
  await expect(serverTab).toBeFocused();
  await page.keyboard.press('ArrowLeft');
  await expect(appEventsTab).toBeFocused();
});

test('App Events severity filters hide warnings and retain errors', async ({ page }) => {
  await page.goto('/#logs');
  await page.getByRole('tab', { name: 'App Events' }).click();
  await page.getByRole('button', { name: 'Errors', exact: true }).click();

  await expect(page.locator('[data-event-severity="warning"]')).toBeHidden();
  await expect(page.locator('[data-event-severity="error"]')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Errors', exact: true })).toHaveAttribute('aria-pressed', 'true');
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

test('settings-key loading failure replaces the loading state with recovery guidance', async ({ page }) => {
  await page.route('**/api/keys', (route) => route.abort('failed'));

  await page.goto('/settings');

  await expect(page.locator('#key-list').getByRole('status')).toContainText('Could not load settings');
  await expect(page.locator('#key-list')).not.toContainText('Loading settings');
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
