// Sign in once, off camera, and save the storage state to <video>/state/auth.json.
// All input comes from the environment; nothing is logged or written except the state file.
//   DEMO_START_LANG    start a demo sandbox: POST /api/demo/start {lang} (Origin = app origin),
//                     then follow the returned redirect with ?tour=0 (demo mode; preferred)
//   DEMO_ENTRY_URL     a "demo entry" URL that signs the browser in on GET
//   DEMO_LOGIN_URL     a login page; with DEMO_EMAIL + DEMO_PASSWORD it is filled and submitted
//   DEMO_EMAIL_SEL / DEMO_PASSWORD_SEL / DEMO_SUBMIT_SEL   selector overrides
//   DEMO_AFTER_URL     glob the browser must reach once signed in (default: leave the login page)
//   DEMO_SEED_CMD      re-run the demo-world seed before the take (safe to repeat)
// Precedence: DEMO_START_LANG, then DEMO_ENTRY_URL, then DEMO_LOGIN_URL.
// With none set (public pages) an empty state is written.
import { test as setup } from '@playwright/test';
import { execSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const vdir = path.join(__dirname, '../..', process.env.VIDEO ?? '');
const E = process.env;

setup('sign in once', async ({ page, context, baseURL }) => {
  if (E.DEMO_SEED_CMD) execSync(E.DEMO_SEED_CMD, { stdio: 'inherit' });
  fs.mkdirSync(path.join(vdir, 'state'), { recursive: true });
  const out = path.join(vdir, 'state/auth.json');
  const abs = (u: string) => new URL(u, baseURL).toString();
  if (E.DEMO_START_LANG) {
    const origin = new URL(baseURL ?? 'http://localhost:3000').origin;
    await page.goto(abs('/'));
    const res = await page.request.post('/api/demo/start', {
      data: { lang: E.DEMO_START_LANG },
      headers: { Origin: origin },
    });
    if (!res.ok()) throw new Error(`demo start failed: ${res.status()} ${await res.text()}`);
    const { redirect } = (await res.json()) as { redirect: string };
    const url = new URL(redirect, origin);
    url.searchParams.set('tour', '0'); // keep the existing query, drop the onboarding tour
    await page.goto(url.toString());
    await page.waitForLoadState('networkidle');
  } else if (E.DEMO_ENTRY_URL) {
    await page.goto(abs(E.DEMO_ENTRY_URL));
    if (E.DEMO_AFTER_URL) await page.waitForURL(E.DEMO_AFTER_URL);
    else await page.waitForLoadState('networkidle');
  } else if (E.DEMO_LOGIN_URL) {
    if (!E.DEMO_EMAIL || !E.DEMO_PASSWORD) throw new Error('DEMO_LOGIN_URL needs DEMO_EMAIL and DEMO_PASSWORD');
    const loginUrl = abs(E.DEMO_LOGIN_URL);
    await page.goto(loginUrl);
    await page.locator(E.DEMO_EMAIL_SEL ?? 'input[type=email], input[name=email]').first().fill(E.DEMO_EMAIL);
    await page.locator(E.DEMO_PASSWORD_SEL ?? 'input[type=password]').first().fill(E.DEMO_PASSWORD);
    await page.locator(E.DEMO_SUBMIT_SEL ?? 'button[type=submit]').first().click();
    if (E.DEMO_AFTER_URL) await page.waitForURL(E.DEMO_AFTER_URL);
    else await page.waitForURL((u) => u.toString() !== loginUrl);
  }
  await context.storageState({ path: out });
});
