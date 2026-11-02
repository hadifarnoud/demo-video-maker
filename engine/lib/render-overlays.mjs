// Render cards (frame by frame), subtitles, callouts and the pointer sprite to PNG via Chromium.
//   node lib/render-overlays.mjs <plan.json>
// plan.json is written by edit.py:
//   { outDir, fps, theme: {--var: value}, cards: [{id, seconds, brand, title, subtitle, url}],
//     subtitles: [{id, rtl, parts: [{t: "text"|"label", s}]}], callouts: [{id, label, hint}] }
// Writes <outDir>/cards/<id>/NNNN.png, subs/<id>.png, callouts/<id>.png, pointer.png, index.json.
import { chromium } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const plan = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const out = plan.outDir;

const FONTS = [
  ['Bricolage Grotesque', 500, 'bricolage-grotesque', 'latin'],
  ['Bricolage Grotesque', 700, 'bricolage-grotesque', 'latin'],
  ['Bricolage Grotesque', 800, 'bricolage-grotesque', 'latin'],
  ['Atkinson Hyperlegible Next', 400, 'atkinson-hyperlegible-next', 'latin'],
  ['Atkinson Hyperlegible Next', 700, 'atkinson-hyperlegible-next', 'latin'],
  ['JetBrains Mono', 500, 'jetbrains-mono', 'latin'],
  ['Vazirmatn', 400, 'vazirmatn', 'arabic'],
  ['Vazirmatn', 700, 'vazirmatn', 'arabic'],
];
const fontCss = FONTS.map(([fam, w, pkg, sub]) => {
  const f = path.join(root, 'node_modules/@fontsource', pkg, 'files', `${pkg}-${sub}-${w}-normal.woff2`);
  const b64 = fs.readFileSync(f).toString('base64');
  return `@font-face{font-family:"${fam}";font-weight:${w};font-style:normal;font-display:block;src:url(data:font/woff2;base64,${b64}) format("woff2")}`;
}).join('\n');
const themeCss = ':root{' + Object.entries(plan.theme ?? {}).map(([k, v]) => `${k}:${v}`).join(';') + '}';

const tmp = path.join(out, '.html');
fs.mkdirSync(tmp, { recursive: true });
const loadHtml = (file) => {
  let h = fs.readFileSync(path.join(here, file), 'utf8').replaceAll('/*FONTS*/', () => fontCss);
  h = h.replace('</style>', themeCss + '</style>');
  const p = path.join(tmp, path.basename(file));
  return [h, p];
};

async function assertFonts(page, families) {
  const missing = await page.evaluate(async (fams) => {
    await Promise.all([...document.fonts].map((f) => f.load().catch(() => null)));
    const loaded = new Set([...document.fonts].filter((f) => f.status === 'loaded').map((f) => f.family.replace(/"/g, '')));
    return fams.filter((f) => !loaded.has(f));
  }, families);
  if (missing.length) throw new Error('fonts not loaded in overlay renderer: ' + missing.join(', '));
}

const browser = await chromium.launch();
const index = { cards: {}, subs: {}, callouts: {}, pointer: null };
try {
  // ---- cards -------------------------------------------------------------
  const [cardHtml, cardPath] = loadHtml('cards/card.html');
  for (const c of plan.cards ?? []) {
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    const html = cardHtml.replace('<script>', `<script>window.CARD=${JSON.stringify(c)};`);
    fs.writeFileSync(cardPath, html);
    await page.goto('file://' + cardPath);
    const rtl = /[؀-ۿ]/.test((c.title ?? '') + (c.subtitle ?? ''));
    await assertFonts(page, ['Bricolage Grotesque', 'Atkinson Hyperlegible Next', 'JetBrains Mono', ...(rtl ? ['Vazirmatn'] : [])]);
    const dir = path.join(out, 'cards', c.id);
    fs.rmSync(dir, { recursive: true, force: true });
    fs.mkdirSync(dir, { recursive: true });
    const n = Math.round(c.seconds * plan.fps);
    for (let i = 0; i < n; i++) {
      await page.evaluate((ms) => document.getAnimations().forEach((a) => { a.pause(); a.currentTime = ms; }), (i / plan.fps) * 1000);
      await page.screenshot({ path: path.join(dir, String(i).padStart(4, '0') + '.png') });
    }
    index.cards[c.id] = { frames: n };
    await page.close();
  }

  // ---- subtitles, callouts, pointer --------------------------------------
  const [ovHtml, ovPath] = loadHtml('overlays/overlay.html');
  fs.writeFileSync(ovPath, ovHtml);
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
  await page.goto('file://' + ovPath);
  await assertFonts(page, ['Atkinson Hyperlegible Next', 'Bricolage Grotesque', 'Vazirmatn']);

  const shot = async (build, file) => {
    await page.evaluate(build.fn, build.arg);
    const el = page.locator('#stage > .item');
    const buf = await el.screenshot({ omitBackground: true });
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, buf);
    const b = await el.boundingBox();
    await page.evaluate(() => (document.getElementById('stage').innerHTML = ''));
    return [Math.ceil(b.width), Math.ceil(b.height)];
  };

  for (const s of plan.subtitles ?? []) {
    index.subs[s.id] = await shot(
      {
        arg: s,
        fn: (s) => {
          const d = document.createElement('div');
          d.className = 'item sub' + (s.rtl ? ' rtl' : '');
          if (s.rtl) d.dir = 'rtl';
          for (const p of s.parts) {
            if (p.t === 'label') {
              const m = document.createElement('mark');
              // RTL: each label is its own isolate (<mark> has unicode-bidi: isolate) with the direction of its first
              // strong letter. A Persian label stays right-to-left (an LRI would reverse its word order); an English
              // label inside Persian text is never reordered or split.
              m.textContent = p.s;
              if (s.rtl) m.dir = /^[^A-Za-z؀-ۿ]*[؀-ۿ]/.test(p.s) ? 'rtl' : 'ltr';
              d.appendChild(m);
            } else d.appendChild(document.createTextNode(p.s));
          }
          document.getElementById('stage').appendChild(d);
        },
      },
      path.join(out, 'subs', s.id + '.png'),
    );
  }
  for (const c of plan.callouts ?? []) {
    index.callouts[c.id] = await shot(
      {
        arg: c,
        fn: (c) => {
          const d = document.createElement('div');
          d.className = 'item callout';
          const b = document.createElement('div');
          b.className = 'body';
          const l = document.createElement('span');
          l.className = 'lab';
          l.textContent = c.label;
          const h = document.createElement('div');
          h.className = 'hint';
          h.textContent = c.hint;
          b.append(l, h);
          d.appendChild(b);
          document.getElementById('stage').appendChild(d);
        },
      },
      path.join(out, 'callouts', c.id + '.png'),
    );
  }
  index.pointer = await shot(
    {
      arg: null,
      fn: () => {
        const d = document.createElement('div');
        d.className = 'item pointer';
        d.innerHTML =
          '<svg width="44" height="52" viewBox="0 0 44 52"><defs><filter id="s" x="-30%" y="-30%" width="160%" height="170%"><feDropShadow dx="0" dy="2" stdDeviation="2" flood-color="#0D1424" flood-opacity=".4"/></filter></defs>' +
          '<path filter="url(#s)" d="M6 4 L6 34 L13.5 27.5 L19 40 L25 37.5 L19.5 25.5 L29.5 25.5 Z" fill="#fff" stroke="#0D1424" stroke-width="2.4" stroke-linejoin="round"/></svg>';
        document.getElementById('stage').appendChild(d);
      },
    },
    path.join(out, 'pointer.png'),
  );
  index.pointer_hotspot = [6, 4];
} finally {
  await browser.close();
}
fs.writeFileSync(path.join(out, 'index.json'), JSON.stringify(index));
console.log('overlays rendered:', Object.keys(index.cards).length, 'cards,', Object.keys(index.subs).length, 'subtitles,', Object.keys(index.callouts).length, 'callouts');
