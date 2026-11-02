// VIDEO=<dir> picks the video (e.g. VIDEO=00-smoke); SCENES=2,5 records those scenes only.
import { defineConfig } from '@playwright/test';
import path from 'node:path';

const VIDEO = process.env.VIDEO;
if (!VIDEO) throw new Error('set VIDEO=<video dir>, e.g. VIDEO=00-smoke');
const vdir = path.join(__dirname, VIDEO);

export default defineConfig({
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: Number(process.env.SCENE_TIMEOUT_MS ?? 180_000),
  reporter: [['list']],
  outputDir: path.join(vdir, 'work', 'pw'),
  use: {
    baseURL: process.env.BASE_URL ?? 'http://localhost:3000',
    viewport: { width: 1280, height: 720 },
    // headless Chromium sends CSS-size (1280x720) screencast frames from the context scale alone;
    // the --force-device-scale-factor flag below is what makes them 2560x1440
    deviceScaleFactor: 2,
    colorScheme: 'light',
    locale: process.env.UI_LOCALE ?? 'en-US',
    launchOptions: { args: ['--force-device-scale-factor=2', '--hide-scrollbars', '--font-render-hinting=none', '--disable-lcd-text'] },
  },
  projects: [
    { name: 'setup', testDir: path.join(__dirname, 'lib/record'), testMatch: 'login.setup.ts' },
    {
      name: 'scenes',
      testDir: vdir,
      testMatch: 'scenes.spec.ts',
      dependencies: ['setup'],
      use: { storageState: path.join(vdir, 'state/auth.json') },
    },
  ],
});
