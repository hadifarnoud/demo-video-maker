// Scene helper for scripted product-demo recordings (see ../../README.md).
//
//   import { scene } from '../lib/record/demo';
//   scene(1, 'Hero', async (s) => {
//     await s.go('/');
//     await s.look('h1', { on: 'Let your experts talk' });
//     await s.hover(s.page.getByRole('link', { name: 'Request a pilot slot' }).first(), { on: '«Request a pilot slot»' });
//     await s.finish();
//   });
//
// Every action is anchored to a phrase of the scene's narration line (cue). Cue time =
// voiceAt + that phrase's char start time from audio/scene-NN.json. Clip time 0 = first
// screencast frame; cuts (cutStart/cutEnd) are removed from clip time.
import { test, type Locator, type Page, type CDPSession } from '@playwright/test';
import { execFileSync, execSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = path.resolve(__dirname, '../..');
const VIDEO = process.env.VIDEO ?? '';
const VDIR = path.join(ROOT, VIDEO);
const VIEW = { w: 1280, h: 720 };
const FRAME = { w: 2560, h: 1440 };
const LATE_LIMIT = 0.35; // s; a later landing than this fails the quality gate
const HOVER_DWELL = 0.06; // pointer settles on the target this long before the cue
const SECRET_CFG: SecretCfg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'secret-patterns.json'), 'utf8'));
const DRY = process.env.DEMO_DRY === '1'; // drift check: run every step and label against the live app, film nothing

export type Cue = { on?: string; nth?: number; at?: 'start' | 'end' | number };
export type Box = { x: number; y: number; width: number; height: number };
type Target = string | Locator | Box;
type Ev = { t: number; kind: string; box?: Box; label?: string; hint?: string; text?: string; wide?: boolean; hold?: number; cue?: string; narrated?: boolean; spoken_t?: number };

type VoiceLog = { sig?: string; text: string; starts: number[]; ends: number[]; duration: number; line: string };

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const readJson = (p: string) => JSON.parse(fs.readFileSync(p, 'utf8'));
const norm = (s: string) => s.replace(/[\W_]+/gu, '').toLowerCase();

function videoCfg(): any {
  return readJson(path.join(VDIR, 'video.json'));
}

/** JPEG width/height from the SOF marker. */
function jpegSize(buf: Buffer): [number, number] {
  let i = 2;
  while (i < buf.length) {
    if (buf[i] !== 0xff) { i++; continue; }
    const m = buf[i + 1];
    if (m >= 0xc0 && m <= 0xcf && m !== 0xc4 && m !== 0xc8 && m !== 0xcc) return [buf.readUInt16BE(i + 7), buf.readUInt16BE(i + 5)];
    i += 2 + buf.readUInt16BE(i + 2);
  }
  throw new Error('no JPEG SOF');
}

/** Phrase lookup in the spoken text; « » are ignored on both sides. -> [firstCharIdx, lastCharIdx] in the original text. */
function findPhrase(text: string, phrase: string, nth = 0): [number, number] | null {
  const map: number[] = [];
  let plain = '';
  [...text].forEach((c, i) => { if (c !== '«' && c !== '»') { plain += c; map.push(i); } });
  const want = phrase.replace(/[«»]/g, '').toLowerCase();
  const hay = plain.toLowerCase();
  let from = 0, idx = -1;
  for (let k = 0; k <= nth; k++) {
    idx = hay.indexOf(want, from);
    if (idx < 0) return null;
    from = idx + 1;
  }
  return [map[idx], map[idx + want.length - 1]];
}

type Rect = { x: number; y: number; width: number; height: number };

/** Runs in the page: the box (viewport CSS px) of the tightest visible element (the scope root included) whose whole text
 *  matches, or null. A badge `Only holder<bdi>Name</bdi>` matches on its own text nodes and gets the box of those nodes. Self-contained. */
function labelFind(arg: { root: Element | null; src: string }): Rect | null {
  const re = new RegExp(arg.src);
  const base: Element = arg.root ?? document.body;
  const vw = window.innerWidth, vh = window.innerHeight;
  const norm = (x: string) => x.replace(/\s+/g, ' ').trim();
  let best: Rect | null = null;
  for (const e of [base, ...Array.from(base.querySelectorAll('*'))]) {
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) continue;
    const cs = getComputedStyle(e);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    const nodes = Array.from(e.childNodes).filter((n) => n.nodeType === 3);
    let box: Rect | null = null;
    if (re.test(norm(e.textContent ?? ''))) box = { x: r.x, y: r.y, width: r.width, height: r.height };
    else if (re.test(norm(nodes.map((n) => n.textContent ?? '').join(' ')))) {
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const n of nodes) {
        if (!(n.textContent ?? '').trim()) continue;
        const rg = document.createRange();
        rg.selectNodeContents(n);
        for (const q of Array.from(rg.getClientRects())) { x0 = Math.min(x0, q.left); y0 = Math.min(y0, q.top); x1 = Math.max(x1, q.right); y1 = Math.max(y1, q.bottom); }
      }
      if (x1 > x0 && y1 > y0) box = { x: x0, y: y0, width: x1 - x0, height: y1 - y0 };
    }
    if (!box) continue;
    const cx0 = Math.max(0, box.x), cy0 = Math.max(0, box.y), cx1 = Math.min(vw, box.x + box.width), cy1 = Math.min(vh, box.y + box.height);
    if (cx1 <= cx0 || cy1 <= cy0) continue;
    const c = { x: cx0, y: cy0, width: cx1 - cx0, height: cy1 - cy0 };
    if (!best || c.width * c.height < best.width * best.height) best = c;
  }
  return best;
}


type SecretCfg = { patterns: { kind: string; re: string }[]; entropy: { run: string; min_len: number; min_bits: number; min_classes: number; hex_min_len: number; hex_min_bits: number } };

/** Runs in the page: what looks like a secret on screen? Visible text, input and textarea values (read-only too; a password field
 *  shows dots and is skipped), placeholder/title/aria-label/alt/value attributes, href and src (patterns only), open shadow roots,
 *  same-origin iframes, the URL and the title. Returns kind and field only: the matched text never leaves the page. Self-contained. */
function secretFind(arg: { cfg: SecretCfg; allow: string[]; masked: string[]; extra: { field: string; value: string }[] }): { kind: string; field: string }[] {
  const pats = arg.cfg.patterns.map((p) => ({ kind: p.kind, re: new RegExp(p.re, 'g') }));
  const allow = arg.allow.map((a) => new RegExp(a));
  const en = arg.cfg.entropy;
  const bits = (x: string) => {
    const m = new Map<string, number>();
    for (const c of x) m.set(c, (m.get(c) ?? 0) + 1);
    let h = 0;
    for (const n of m.values()) h -= (n / x.length) * Math.log2(n / x.length);
    return h;
  };
  const out: { kind: string; field: string }[] = [];
  const seen = new Set<string>();
  const hit = (kind: string, field: string, frag: string) => {
    if (allow.some((a) => a.test(frag))) return;
    if (!seen.has(kind + '|' + field)) { seen.add(kind + '|' + field); out.push({ kind, field }); }
  };
  const test = (value: string, field: string, entropy = true) => {
    if (!value || value.length < 8) return;
    for (const p of pats) for (const m of value.matchAll(p.re)) hit(p.kind, field, m[0]);
    if (!entropy) return;
    for (const m of value.matchAll(new RegExp(en.run, 'g'))) {
      const r = m[0];
      if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(r)) continue;
      const classes = [/[a-z]/, /[A-Z]/, /[0-9]/, /[_+=]/].filter((c) => c.test(r)).length;
      if (r.length >= en.min_len && classes >= en.min_classes && bits(r) >= en.min_bits) hit('high-entropy', field, r);
      else if (/^[0-9a-f]+$/i.test(r) && r.length >= en.hex_min_len && bits(r) >= en.hex_min_bits) hit('hex-run', field, r);
    }
  };
  const desc = (e: Element) => {
    const nm = e.getAttribute('name') ?? e.getAttribute('data-demo');
    return e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + (nm ? `[${nm}]` : '');
  };
  const masked = (e: Element) => arg.masked.some((q) => { try { return e.closest(q) !== null; } catch { return false; } });
  const skipTag = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']);
  const walk = (root: Document | ShadowRoot, pre: string) => {
    for (const e of Array.from(root.querySelectorAll('*'))) {
      if (e.shadowRoot) walk(e.shadowRoot, pre + desc(e) + '>>');
      if (e.tagName === 'IFRAME') {
        try { const d = (e as HTMLIFrameElement).contentDocument; if (d) walk(d, pre + desc(e) + '>>'); } catch { /* cross-origin */ }
      }
      if (skipTag.has(e.tagName)) continue;
      const r = e.getBoundingClientRect();
      const win = e.ownerDocument.defaultView ?? window;
      const cs = win.getComputedStyle(e);
      if (r.width <= 0 || r.height <= 0 || cs.visibility === 'hidden' || cs.display === 'none' || masked(e)) continue;
      const f = pre + desc(e);
      for (const n of Array.from(e.childNodes)) if (n.nodeType === 3) test(n.textContent ?? '', f);
      if ((e.tagName === 'INPUT' && (e as HTMLInputElement).type !== 'password') || e.tagName === 'TEXTAREA') test((e as HTMLInputElement).value, f + '@value');
      for (const a of ['placeholder', 'title', 'aria-label', 'alt', 'value']) test(e.getAttribute(a) ?? '', f + '@' + a);
      for (const a of ['href', 'src']) test(e.getAttribute(a) ?? '', f + '@' + a, false);
    }
  };
  walk(document, '');
  test(location.href, 'location.href', false);
  test(document.title, 'document.title');
  for (const x of arg.extra) test(x.value, x.field);
  return out;
}

/** Runs in the page: rewrite a property (`value`, `text` or an attribute) of every element matching `selector`, and keep doing it
 *  (a later React render must not bring the old text back). `from` is replaced by `to` inside the current text; without `from` the
 *  whole value becomes `to`. `$ORIGIN` in `from` is the page's origin. Self-contained. */
function domRewrite(arg: { selector: string; prop: string; from: string | null; to: string }): { matched: number; changed: number; before: string } {
  const from = arg.from === null ? null : arg.from.replace('$ORIGIN', location.origin);
  const apply = () => {
    let changed = 0, before = '';
    const els = Array.from(document.querySelectorAll(arg.selector));
    for (const el of els) {
      const cur = arg.prop === 'value' ? (el as HTMLInputElement).value : arg.prop === 'text' ? el.textContent ?? '' : el.getAttribute(arg.prop) ?? '';
      const nxt = from === null ? arg.to : cur.split(from).join(arg.to);
      if (nxt === cur) continue;
      changed++;
      before = before || (from ?? cur);
      if (arg.prop === 'value') Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value')!.set!.call(el, nxt); // React's own setter is bypassed on purpose: the page must not see an input event
      else if (arg.prop === 'text') el.textContent = nxt;
      else el.setAttribute(arg.prop, nxt);
    }
    return { matched: els.length, changed, before };
  };
  const first = apply();
  setInterval(apply, 20);
  return first;
}

/** Runs in the page: a blur plate over the first element matching `selector` (only the part after the text `after` when given). */
function domMask(arg: { selector: string; after: string | null; id: string }): number {
  const plate = document.createElement('div');
  plate.id = arg.id;
  document.body.appendChild(plate);
  const place = () => {
    const el = document.querySelector(arg.selector);
    if (!el) return;
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    let x0 = r.left + 1;
    if (arg.after !== null) {
      const ctx = document.createElement('canvas').getContext('2d')!;
      ctx.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
      x0 = r.left + parseFloat(cs.paddingLeft) + ctx.measureText(arg.after).width + 2;
    }
    Object.assign(plate.style, { position: 'fixed', left: x0 + 'px', top: r.top + 1 + 'px', width: Math.max(0, r.right - x0 - 1) + 'px', height: r.height - 2 + 'px', zIndex: '2147483000', pointerEvents: 'none', backdropFilter: 'blur(9px)', background: 'rgba(240,243,247,0.72)', borderRadius: '4px' });
  };
  place();
  setInterval(place, 20);
  return document.querySelectorAll(arg.selector).length;
}

/** Runs in the page: the visible texts a label could have meant (an element's own text, or the whole text of a small element). */
function visibleTexts(): string[] {
  const vw = window.innerWidth, vh = window.innerHeight;
  const out = new Set<string>();
  const clean = (x: string) => x.replace(/\s+/g, ' ').trim();
  for (const e of Array.from(document.body.querySelectorAll('*'))) {
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) continue;
    const cs = getComputedStyle(e);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    const own = clean(Array.from(e.childNodes).filter((n) => n.nodeType === 3).map((n) => n.textContent ?? '').join(' '));
    if (own.length >= 2 && own.length <= 120) out.add(own);
    const whole = clean(e.textContent ?? '');
    if (e.children.length <= 3 && whole.length >= 2 && whole.length <= 120) out.add(whole);
    if (out.size > 800) break;
  }
  return [...out];
}

/** Bigram overlap (Dice) of two strings, 0..1; a text that contains the other counts as at least 0.6. */
function similar(a: string, b: string): number {
  const x = a.toLowerCase(), y = b.toLowerCase();
  if (x.length > 3 && (y.includes(x) || x.includes(y))) return Math.max(0.6, 2 / (1 + Math.abs(x.length - y.length)));
  const grams = (z: string) => { const m = new Map<string, number>(); for (let i = 0; i + 1 < z.length; i++) m.set(z.slice(i, i + 2), (m.get(z.slice(i, i + 2)) ?? 0) + 1); return m; };
  const ga = grams(x), gb = grams(y);
  let hit = 0;
  for (const [g, n] of ga) hit += Math.min(n, gb.get(g) ?? 0);
  return x.length + y.length < 2 ? 0 : (2 * hit) / (Math.max(1, x.length - 1) + Math.max(1, y.length - 1));
}

export class Scene {
  readonly page: Page;
  readonly n: number;
  private cdp!: CDPSession;
  private voice: VoiceLog | null = null;
  private voiceAt: number;
  private fonts: string[];
  private frames: { ts: number; file: string }[] = [];
  private framesDir: string;
  private t0 = 0; // performance.now() at the first frame
  private recording = false;
  private cutAccum = 0;
  private cutOpen: number | null = null; // wall seconds since t0
  private cuts: [number, number][] = [];
  private events: Ev[] = [];
  private pathLog: [number, number, number][] = [];
  private presses: [number, number, number][] = [];
  private late: { kind: string; cue: string; by: number }[] = [];
  private checked = new Set<string>();
  private preflightLog: { when: string; ok: boolean; problems: string[] }[] = [];
  private domEdits: { t: number; kind: string; selector: string; prop?: string; before?: string; after?: string; why: string }[] = [];
  private secretAllow: { pattern: string; why: string }[] = [];
  private maskedSelectors: string[] = [];
  private scans = 0;
  private lastScan = 0;
  private reported = new Set<string>();
  private pointer = { x: 1040, y: 640 };
  private side = 1;
  private done = false;
  private firstFrame!: Promise<void>;
  private gotFirst!: () => void;

  readonly title: string;

  constructor(page: Page, n: number, title = '') {
    this.page = page;
    this.n = n;
    this.title = title;
    const cfg = videoCfg();
    this.voiceAt = cfg.pacing?.voice_at ?? 0.5;
    this.fonts = cfg.fonts ?? [];
    this.secretAllow = cfg.secrets?.allow ?? [];
    for (const a of this.secretAllow) {
      if (!a.pattern || !a.why) throw new Error('video.json secrets.allow: every entry needs a pattern and a why');
      new RegExp(a.pattern);
    }
    this.framesDir = path.join(VDIR, 'work', `frames-${String(n).padStart(2, '0')}`);
    const vp = path.join(VDIR, 'audio', `scene-${String(n).padStart(2, '0')}.json`);
    if (fs.existsSync(vp)) this.voice = readJson(vp);
  }

  async init() {
    fs.rmSync(this.framesDir, { recursive: true, force: true });
    fs.mkdirSync(this.framesDir, { recursive: true });
    fs.mkdirSync(path.join(VDIR, 'clips'), { recursive: true });
    const hf = path.join(VDIR, 'clips', 'secret-hits.json'); // a new take of this scene clears the last take's hit
    if (!DRY && fs.existsSync(hf)) { const h = readJson(hf); delete h[String(this.n)]; fs.writeFileSync(hf, JSON.stringify(h, null, 1)); }
    const calm = videoCfg().calm_css ? String(videoCfg().calm_css) : '';
    // invisible 2 px element that keeps repainting, so the screencast stays ~30 fps on a still page
    await this.page.addInitScript((css: string) => {
      const add = () => {
        if (document.getElementById('__repaint')) return;
        const st = document.createElement('style');
        st.textContent =
          '@keyframes __rp{from{background-color:#fefefe}to{background-color:#ffffff}}' +
          '#__repaint{position:fixed;right:0;bottom:0;width:2px;height:2px;pointer-events:none;z-index:2147483647;animation:__rp 33ms steps(1) infinite alternate}' + css;
        document.documentElement.appendChild(st);
        const d = document.createElement('div');
        d.id = '__repaint';
        document.documentElement.appendChild(d);
      };
      if (document.documentElement) add(); else document.addEventListener('DOMContentLoaded', add);
    }, calm);
    this.firstFrame = new Promise((r) => (this.gotFirst = r));
    await this.page.mouse.move(this.pointer.x, this.pointer.y);
  }

  // ---- clock -------------------------------------------------------------
  /** Clip time in seconds (cuts removed). */
  now(): number {
    const wall = (performance.now() - this.t0) / 1000;
    return this.cutOpen !== null ? this.cutOpen - this.cutAccum : wall - this.cutAccum;
  }
  private async until(t: number) {
    if (DRY) return;
    for (;;) {
      if (this.cutOpen !== null) throw new Error('cannot wait on the clip clock inside cutStart/cutEnd');
      const d = t - this.now();
      if (d <= 0.002) return;
      if (d > 0.6 && performance.now() - this.lastScan > 1000) await this.scanSecrets('wait'); // a token that appears mid-scene
      await sleep(Math.min(d * 1000, 50));
    }
  }

  // ---- cues --------------------------------------------------------------
  /** Cue time in clip seconds, or null when no cue given. */
  private cueTime(cue?: Cue): number | null {
    if (!cue?.on) return null;
    if (DRY) { // before any voice is paid for: check the cue against the script, and the label against the screen
      const line = fs.readFileSync(path.join(VDIR, 'narration.txt'), 'utf8').split('\n')[this.n - 1] ?? '';
      if (!findPhrase(line, cue.on, cue.nth ?? 0)) console.log(`[drift] scene ${this.n}: cue "${cue.on}" is not in the script`);
      const m = cue.on.match(/«([^»]+)»/);
      if (m) this.pendingLabels.push(m[1]);
      return 0;
    }
    if (!this.voice) throw new Error(`scene ${this.n}: cue "${cue.on}" but no audio/scene-NN.json; voice the line first (voice.py)`);
    const hit = findPhrase(this.voice.text, cue.on, cue.nth ?? 0);
    if (!hit) throw new Error(`scene ${this.n}: cue phrase "${cue.on}" not found in "${this.voice.text}"`);
    const [a, b] = hit;
    const s0 = this.voice.starts[a], e1 = this.voice.ends[b];
    const at = cue.at ?? 'start';
    const f = at === 'start' ? 0 : at === 'end' ? 1 : at;
    const label = cue.on.match(/«([^»]+)»/);
    if (label) this.pendingLabels.push(label[1]);
    return this.voiceAt + s0 + f * (e1 - s0);
  }
  private pendingLabels: string[] = [];
  private async autoLabels() {
    const l = this.pendingLabels.splice(0);
    for (const t of l) await this.label(t);
  }
  private lateCheck(kind: string, cue: number | null, landed: number, desc: string) {
    if (cue === null) return;
    const by = landed - cue;
    if (by > LATE_LIMIT) {
      this.late.push({ kind, cue: desc, by: +by.toFixed(3) });
      console.warn(`[late] scene ${this.n} ${kind} "${desc}" landed ${by.toFixed(2)}s after its cue`);
    }
  }

  // ---- geometry ----------------------------------------------------------
  private async box(t: Target): Promise<Box> {
    if (typeof t === 'object' && 'width' in t && !('count' in t)) return t as Box;
    const loc = typeof t === 'string' ? this.page.locator(t).first() : (t as Locator).first();
    await loc.waitFor({ state: 'visible', timeout: 8000 });
    const b = await loc.boundingBox();
    if (!b) throw new Error(`scene ${this.n}: no box for target`);
    if (b.y + b.height < 0 || b.y > VIEW.h || b.x + b.width < 0 || b.x > VIEW.w)
      throw new Error(`scene ${this.n}: target is outside the viewport (${JSON.stringify(b)}); s.scrollTo() it first`);
    return b;
  }
  private center(b: Box) {
    return { x: Math.round(b.x + b.width / 2), y: Math.round(b.y + b.height / 2) };
  }

  // ---- pointer -----------------------------------------------------------
  /** Eased human move: minimum-jerk speed profile along a slightly curved path. Ends at `landBy`.
   *  Time-driven: slow CDP round trips make steps coarser, never make the move longer. */
  private async moveTo(x: number, y: number, landBy: number | null) {
    if (DRY) {
      await this.page.mouse.move(x, y);
      this.pointer = { x, y };
      return;
    }
    const p0 = { ...this.pointer };
    const dist = Math.hypot(x - p0.x, y - p0.y);
    let dur = dist < 4 ? 0.05 : Math.min(1.3, 0.38 + dist / 1500);
    if (landBy !== null) {
      await this.until(landBy - dur);
      dur = Math.max(dist < 4 ? 0.05 : 0.3, Math.min(dur, landBy - this.now())); // behind: move faster, not later
    }
    const t0 = this.now();
    this.side = -this.side;
    const nx = dist ? -(y - p0.y) / dist : 0, ny = dist ? (x - p0.x) / dist : 0;
    const off = dist * 0.07 * this.side;
    const cx = (p0.x + x) / 2 + nx * off, cy = (p0.y + y) / 2 + ny * off;
    this.pathLog.push([+t0.toFixed(3), p0.x, p0.y]);
    for (;;) {
      const u = Math.min(1, (this.now() - t0) / dur);
      const e = 10 * u ** 3 - 15 * u ** 4 + 6 * u ** 5;
      const px = (1 - e) ** 2 * p0.x + 2 * (1 - e) * e * cx + e ** 2 * x;
      const py = (1 - e) ** 2 * p0.y + 2 * (1 - e) * e * cy + e ** 2 * y;
      await this.page.mouse.move(u >= 1 ? x : px, u >= 1 ? y : py);
      this.pathLog.push([+this.now().toFixed(3), +(u >= 1 ? x : px).toFixed(1), +(u >= 1 ? y : py).toFixed(1)]);
      if (u >= 1) break;
      await sleep(8);
    }
    this.pointer = { x, y };
  }

  // ---- actions -----------------------------------------------------------
  /** Load a page, check fonts and calm state, then start the clip. Voice starts voiceAt later. */
  async go(url: string, opts: { ready?: string } = {}) {
    await this.page.goto(url, { waitUntil: 'load' });
    // `ready`: an element that appears after hydration or a fetch (e.g. «See a demo» on the landing page),
    // awaited before the clip starts so nothing pops in or shifts the layout on camera
    if (opts.ready) await this.page.locator(opts.ready).first().waitFor({ state: 'visible', timeout: 15_000 });
    await this.settle();
    await this.preflight('start');
    if (!this.recording) await this.startCapture();
  }

  /** Wait for load, web fonts and two painted frames. */
  async settle(ms = 250) {
    await this.page.waitForLoadState('load');
    await this.page.evaluate(() => document.fonts.ready.then(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))));
    await sleep(ms);
  }

  async click(target: Target, cue: Cue & { cut?: boolean } = {}) {
    const cueT = this.cueTime(cue);
    const b = await this.box(target);
    const c = this.center(b);
    await this.moveTo(c.x, c.y, cueT === null ? null : cueT - HOVER_DWELL);
    if (cueT !== null) await this.until(cueT);
    const tp = this.now();
    this.lateCheck('click', cueT, tp, cue.on ?? '');
    await this.autoLabels();
    this.events.push({ t: +tp.toFixed(3), kind: 'click', box: b, cue: cue.on });
    this.presses.push([+tp.toFixed(3), c.x, c.y]);
    if (cue.cut) this.cutStart(); // frames after the press are dropped until cutEnd(): hides saves and page loads
    await this.page.mouse.down();
    await sleep(70);
    await this.page.mouse.up();
    if (!cue.cut) await this.scanSecrets('click');
  }

  async hover(target: Target, cue: Cue = {}) {
    const cueT = this.cueTime(cue);
    const b = await this.box(target);
    const c = this.center(b);
    await this.moveTo(c.x, c.y, cueT === null ? null : cueT - HOVER_DWELL);
    if (cueT !== null) await this.until(cueT);
    const tl = this.now();
    this.lateCheck('hover', cueT, tl, cue.on ?? '');
    await this.autoLabels();
    this.events.push({ t: +tl.toFixed(3), kind: 'hover', box: b, cue: cue.on });
    await this.scanSecrets('hover');
  }

  /** Direct the viewer's eye to a region without moving the pointer. `wide` asks for a wider view. */
  async look(target: Target, cue: Cue & { wide?: boolean } = {}) {
    const cueT = this.cueTime(cue);
    const b = await this.box(target); // resolve geometry before the cue so the landing is not late
    if (cueT !== null) await this.until(cueT);
    const tl = this.now();
    this.lateCheck('look', cueT, tl, cue.on ?? '');
    await this.autoLabels();
    this.events.push({ t: +tl.toFixed(3), kind: 'look', box: b, wide: cue.wide, cue: cue.on });
    await this.scanSecrets('look');
  }

  /** Lower-third in the edit: exact label + short hint, shown from the cue for `hold` seconds. */
  async callout(label: string, hint: string, cue: Cue & { hold?: number; scope?: Target } = {}) {
    const cueT = this.cueTime(cue);
    await this.label(label, cue.scope, cueT ?? undefined);
    if (cueT !== null) await this.until(cueT);
    await this.autoLabels();
    const tl = this.now();
    this.lateCheck('callout', cueT, tl, cue.on ?? label);
    this.events.push({ t: +tl.toFixed(3), kind: 'callout', label, hint, hold: cue.hold ?? 3.2, cue: cue.on });
    await this.scanSecrets('callout');
  }

  /** Type into a field one char at a time (the field is clicked at the cue first). */
  async type(target: Target, text: string, cue: Cue & { delay?: number } = {}) {
    const pw = /pass/i.test(String(target));
    if (!pw) await this.scanSecrets('type', [{ field: 'typed text', value: text }]); // before it appears on screen
    await this.click(target, cue);
    const b = await this.box(target);
    this.events.push({ t: +this.now().toFixed(3), kind: 'type', box: b, text: /pass/i.test(String(target)) ? '' : text });
    const d = cue.delay ?? 65;
    for (let i = 0; i < text.length; i++) {
      await this.page.keyboard.type(text[i]);
      await sleep(d + ((i * 37) % 31) - 15);
    }
    await this.scanSecrets('type');
  }

  /** Smooth scroll so `target` sits near the upper third (`align: 'bottom'`: its bottom edge on the viewport's bottom edge,
   *  which keeps whatever follows it in the page out of the picture); finishes at the cue. */
  async scrollTo(target: Target, cue: Cue & { align?: 'bottom' } = {}) {
    const cueT = this.cueTime(cue);
    const loc = typeof target === 'string' ? this.page.locator(target).first() : (target as Locator).first();
    await loc.waitFor({ state: 'attached' });
    const { y: wantY, cur } = await loc.evaluate((el, bottom) => {
      const r = el.getBoundingClientRect();
      const y = bottom ? r.bottom + window.scrollY - window.innerHeight + 2 : r.top + window.scrollY - window.innerHeight * 0.18;
      return { y: Math.max(0, y), cur: window.scrollY };
    }, cue.align === 'bottom');
    const dist = Math.abs(wantY - cur);
    const dur = Math.min(1.6, 0.5 + dist / 2400);
    if (cueT !== null) await this.until(cueT - dur);
    const t0 = this.now();
    for (;;) {
      const u = Math.min(1, (this.now() - t0) / dur);
      const e = 10 * u ** 3 - 15 * u ** 4 + 6 * u ** 5;
      await this.page.evaluate((y) => window.scrollTo({ top: y, behavior: 'instant' as ScrollBehavior }), cur + (wantY - cur) * e);
      if (u >= 1) break;
      await sleep(8);
    }
    this.events.push({ t: +this.now().toFixed(3), kind: 'scroll' });
    await this.autoLabels();
    await this.scanSecrets('scroll');
  }



  /** Evidence for a failed take: the screen as it was (clips/fail-NN.png; drift-shots/ in a drift run). */
  async failShot(): Promise<string> {
    const dir = path.join(VDIR, DRY ? 'drift-shots' : 'clips');
    fs.mkdirSync(dir, { recursive: true });
    const file = path.join(dir, `fail-${String(this.n).padStart(2, '0')}.png`);
    if (this.shot) return file;
    this.shot = true;
    await this.page.screenshot({ path: file }).catch(() => {});
    return file;
  }
  private shot = false;

  /** The visible texts closest to a label that was not found, best first. */
  private async nearest(text: string): Promise<string[]> {
    const seen = await this.page.evaluate(visibleTexts).catch(() => [] as string[]);
    return seen.map((t) => ({ t, s: similar(text, t) })).filter((x) => x.s >= 0.3).sort((a, b) => b.s - a.s).slice(0, 5).map((x) => x.t);
  }

  /** The one way a spec may change what the page shows. Rewrites `prop` (`value`, `text` or an attribute) of the elements matching
   *  `selector`: `from` -> `to` inside the current text, or the whole value -> `to` without `from`. The edit and its reason are
   *  logged to `dom_edits` and listed by the gate; a raw page.evaluate/route/addInitScript in a spec fails the gate. */
  async rewrite(selector: string, o: { prop?: string; from?: string; to: string; why: string }) {
    if (!o.why?.trim()) throw new Error(`scene ${this.n}: rewrite(${selector}) needs a why`);
    await this.page.locator(selector).first().waitFor({ state: 'attached', timeout: 8000 });
    const prop = o.prop ?? 'text';
    const r = await this.page.evaluate(domRewrite, { selector, prop, from: o.from ?? null, to: o.to });
    if (r.changed === 0) {
      const m = `scene ${this.n}: rewrite(${selector}) changed nothing (${r.matched} match${r.matched === 1 ? '' : 'es'}); the app no longer renders "${o.from ?? '(value)'}" there`;
      if (DRY) { console.log(`[drift] ${m}`); return; }
      throw new Error(m);
    }
    const tok = (x: string) => x.length > 60 ? x.slice(0, 40) + '…' : x.replace(/[A-Za-z0-9_+=-]{16,}/g, '…'); // a log must not carry the secret it hides
    this.domEdits.push({ t: +this.now().toFixed(3), kind: 'rewrite', selector, prop, before: tok(r.before), after: tok(o.to), why: o.why });
  }

  /** A blur plate over an element (only the part after the text `after`, when given). Logged like rewrite(); the secret scan
   *  skips what the plate covers. */
  async mask(selector: string, o: { after?: string; why: string }) {
    if (!o.why?.trim()) throw new Error(`scene ${this.n}: mask(${selector}) needs a why`);
    await this.page.locator(selector).first().waitFor({ state: 'attached', timeout: 8000 });
    await this.page.evaluate(domMask, { selector, after: o.after ?? null, id: `__dvm_mask_${this.maskedSelectors.length}` });
    this.maskedSelectors.push(selector);
    this.domEdits.push({ t: +this.now().toFixed(3), kind: 'mask', selector, before: o.after, why: o.why });
  }

  /** Assert an exact on-screen label is visible; recorded in clips/labels.json. A label the line names in «» is also logged
   *  as a `label` event with its box: the edit keeps subtitles off it and the gate compares the picture with and without subtitles. */
  async label(text: string, scope?: Target, at?: number) {
    // One DOM walk (Playwright's getByText over a big page takes 0.3 s and made the next cue late).
    // Whole-text match; curly, straight or Persian quotes around a quoted line (“…”, «…») and curly apostrophes are tolerated.
    const esc = text.trim().replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/['’]/g, "['’]").replace(/\s+/g, '\\s+');
    const src = `^\\s*["“”«]?${esc}["“”»]?\\s*$`;
    let box: Rect | null;
    if (scope === undefined) box = await this.page.evaluate(labelFind, { root: null, src });
    else {
      const sl = typeof scope === 'string' ? this.page.locator(scope).first() : (scope as Locator).first();
      const h = await sl.elementHandle();
      box = h ? await this.page.evaluate(labelFind, { root: h, src }) : null;
    }
    const found = box !== null;
    this.checked.add(norm(text));
    const suggest = found ? [] : await this.nearest(text);
    const shot = found ? '' : path.relative(VDIR, await this.failShot());
    if (DRY) {
      if (!found) console.log(`[drift] scene ${this.n}: label "${text}" is not on screen; nearest: ${suggest.map((x) => `"${x}"`).join(', ') || 'nothing alike'} (${shot})`);
      return; // list every stale label in one run, and leave the real clip logs alone
    }
    const f = path.join(VDIR, 'clips', 'labels.json');
    const all = fs.existsSync(f) ? readJson(f) : {};
    all[`scene-${this.n}: ${text}`] = found ? { scene: this.n, label: text, found } : { scene: this.n, label: text, found, suggest, shot };
    fs.writeFileSync(f, JSON.stringify(all, null, 1));
    if (!found) throw new Error(`scene ${this.n}: label "${text}" is not visible on screen; nearest on screen: ${suggest.map((x) => `"${x}"`).join(', ') || 'nothing alike'}; screenshot ${shot}`);
    if (this.recording && this.cutOpen === null && box) {
      const named = [...(this.voice?.line ?? '').matchAll(/«([^»]+)»/g)].some((m) => norm(m[1]) === norm(text));
      const hit = this.voice ? findPhrase(this.voice.text, text, 0) : null;
      this.events.push({ t: +(at ?? this.now()).toFixed(3), kind: 'label', label: text, box, narrated: named, spoken_t: hit ? +(this.voiceAt + this.voice!.starts[hit[0]]).toFixed(3) : undefined });
    }
  }

  cutStart() {
    this.cutOpen = (performance.now() - this.t0) / 1000;
  }
  cutEnd() {
    if (this.cutOpen === null) return;
    const end = (performance.now() - this.t0) / 1000;
    this.cuts.push([this.cutOpen, end]);
    this.cutAccum += end - this.cutOpen;
    this.cutOpen = null;
  }

  /** Hold until the line ends + 0.6 s, check the page is calm, encode the clip. */
  async finish() {
    if (this.done) return;
    if (this.voice) await this.until(this.voiceAt + this.voice.duration + 0.6);
    await this.preflight('end');
    const spoken = DRY ? fs.readFileSync(path.join(VDIR, 'narration.txt'), 'utf8').split('\n')[this.n - 1] ?? '' : this.voice?.line ?? '';
    const unchecked = [...spoken.matchAll(/«([^»]+)»/g)].map((m) => m[1]).filter((l) => !this.checked.has(norm(l)));
    for (const l of unchecked) console.warn(`[label] scene ${this.n}: "${l}" is named in the line but was never checked`);
    if (DRY) {
      for (const l of unchecked) console.log(`[drift] scene ${this.n}: "${l}" is in the script but no step checks it`);
      this.done = true;
      return;
    }
    const end = this.now();
    await this.cdp.send('Page.stopScreencast');
    this.recording = false;
    this.capturing = false;
    await this.encode(end, unchecked);
    this.done = true;
  }

  // ---- internals ---------------------------------------------------------
  /** Fail the take when a secret is on screen (or about to be typed). The log names the kind and the field, never the value. */
  private async scanSecrets(when: string, extra: { field: string; value: string }[] = []) {
    let hits: { kind: string; field: string }[];
    this.lastScan = performance.now();
    try {
      hits = await this.page.evaluate(secretFind, { cfg: SECRET_CFG, allow: this.secretAllow.map((a) => a.pattern), masked: this.maskedSelectors, extra });
    } catch (e) {
      if (/context was destroyed|navigat|closed/i.test(String(e))) return; // the page is changing under a click
      throw e;
    }
    this.scans++;
    if (!hits.length) return;
    const msg = hits.map((h) => `${h.kind} in ${h.field}`).join('; ');
    if (DRY) {
      if (!this.reported.has(msg)) console.log(`[drift] scene ${this.n}: possible secret (${when}): ${msg}`);
      this.reported.add(msg);
      return;
    }
    const f = path.join(VDIR, 'clips', 'secret-hits.json');
    const all = fs.existsSync(f) ? readJson(f) : {};
    all[String(this.n)] = { when, hits };
    fs.writeFileSync(f, JSON.stringify(all, null, 1));
    throw new Error(`scene ${this.n} secret scan (${when}) found: ${msg}`);
  }

  private async preflight(when: string) {
    await this.scanSecrets('preflight ' + when);
    const problems: string[] = await this.page.evaluate(async (fams: string[]) => {
      await document.fonts.ready;
      const loaded = [...document.fonts].filter((f) => f.status === 'loaded').map((f) => f.family.replace(/["'\s]+/g, '_').toLowerCase());
      const out: string[] = [];
      // a family is required only when a rendered element on this page uses it (Chromium loads a web font lazily,
      // so a page without monospace text never fetches JetBrains Mono; the landing page, which uses it, must)
      const used = (f: string) => {
        for (const e of document.querySelectorAll('body *')) {
          if (!Array.from(e.childNodes).some((n) => n.nodeType === 3 && (n.textContent ?? '').trim())) continue;
          const r = e.getBoundingClientRect();
          if (r.width > 0 && r.height > 0 && getComputedStyle(e).fontFamily.replace(/[\s_"']/g, '').toLowerCase().includes(f.replace(/\s+/g, '').toLowerCase())) return true; // next/font names are __JetBrains_Mono_hash
        }
        return false;
      };
      for (const f of fams) if (used(f) && !loaded.some((l) => l.includes(f.replace(/\s+/g, '_').toLowerCase()))) out.push(`font not loaded: ${f}`);
      const vis = (e: Element) => {
        const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
        return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
      };
      for (const e of document.querySelectorAll('[role=alert]')) if (vis(e) && (e.textContent ?? '').trim()) out.push(`alert: ${(e.textContent ?? '').trim().slice(0, 60)}`);
      const sel = '[data-sonner-toast],[role=progressbar],[aria-busy=true],.animate-spin,[class*=spinner],[class*=Toastify],nextjs-portal';
      for (const e of document.querySelectorAll(sel)) if ((vis(e) && !(e.getAttribute('role') === 'progressbar' && e.hasAttribute('aria-valuenow'))) || e.tagName === 'NEXTJS-PORTAL') out.push(`toast/spinner: ${e.tagName.toLowerCase()}.${String(e.className).slice(0, 40)}`);
      // a dev origin must never be on screen (text, input values): a viewer reads it as a dev build
      const shown = (document.body.innerText || '') + ' ' + Array.from(document.querySelectorAll('input,textarea')).map((i) => (i as HTMLInputElement).value).join(' ');
      if (/localhost|127\.0\.0\.1|pxb-demo-app/.test(shown)) out.push('dev origin on screen: ' + (shown.match(/(localhost|127\.0\.0\.1|pxb-demo-app)\S{0,30}/) ?? [''])[0]);
      return out;
    }, this.fonts);
    this.preflightLog.push({ when, ok: problems.length === 0, problems });
    if (problems.length) throw new Error(`scene ${this.n} preflight (${when}) failed: ${problems.join('; ')}; screenshot ${path.relative(VDIR, await this.failShot())}`);
  }

  private async startCapture() {
    if (DRY) {
      this.t0 = performance.now();
      this.recording = true;
      return;
    }
    this.cdp = await this.page.context().newCDPSession(this.page);
    this.cdp.on('Page.screencastFrame', (e: any) => {
      if (!this.capturing) return;
      const i = this.frames.length;
      const file = path.join(this.framesDir, `${String(i).padStart(6, '0')}.jpg`);
      const buf = Buffer.from(e.data, 'base64');
      if (i === 0) {
        const [w, h] = jpegSize(buf);
        if (w !== FRAME.w || h !== FRAME.h) this.badSize = `${w}x${h}`;
        this.t0 = performance.now();
        this.gotFirst();
      }
      fs.writeFileSync(file, buf);
      this.frames.push({ ts: e.metadata.timestamp, file });
      this.cdp.send('Page.screencastFrameAck', { sessionId: e.sessionId }).catch(() => {});
    });
    this.capturing = true;
    await this.cdp.send('Page.startScreencast', { format: 'jpeg', quality: 65, maxWidth: FRAME.w, maxHeight: FRAME.h, everyNthFrame: 1 });
    await Promise.race([this.firstFrame, sleep(5000).then(() => { throw new Error('no screencast frame within 5 s'); })]);
    if (this.badSize) throw new Error(`screencast frames are ${this.badSize}, need ${FRAME.w}x${FRAME.h}; check deviceScaleFactor 2`);
    this.recording = true;
  }
  private badSize = '';
  private capturing = false;

  private async encode(endClip: number, unchecked: string[]) {
    const nn = String(this.n).padStart(2, '0');
    const t_first = this.frames[0].ts;
    // frame clip times: wall offset minus the cuts before it; frames inside a cut are dropped
    const kept: { file: string; t: number }[] = [];
    for (const f of this.frames) {
      const w = f.ts - t_first;
      if (this.cuts.some(([a, b]) => w >= a && w < b)) continue;
      const before = this.cuts.filter(([, b]) => b <= w).reduce((s, [a, b]) => s + (b - a), 0);
      kept.push({ file: f.file, t: Math.max(0, w - before) });
    }
    if (kept.length < 2) throw new Error(`scene ${this.n}: only ${kept.length} frames captured`);
    const list: string[] = [];
    kept.forEach((k, i) => {
      const next = i + 1 < kept.length ? kept[i + 1].t : endClip;
      list.push(`file '${k.file}'`, `duration ${Math.max(0.001, next - k.t).toFixed(4)}`);
    });
    list.push(`file '${kept[kept.length - 1].file}'`);
    const lf = path.join(this.framesDir, 'list.txt');
    fs.writeFileSync(lf, list.join('\n'));
    const out = path.join(VDIR, 'clips', `scene-${nn}.mp4`);
    execFileSync('ffmpeg', ['-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', lf, '-vf', 'fps=30,format=yuv420p', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '12', '-r', '30', out]);
    const span = kept[kept.length - 1].t - kept[0].t;
    fs.writeFileSync(
      path.join(VDIR, 'clips', `scene-${nn}.json`),
      JSON.stringify({
        log_version: 4, scene: this.n, title: this.title, voiceAt: this.voiceAt, voiceDuration: this.voice?.duration ?? 0, duration: +endClip.toFixed(3),
        fps: 30, size: [FRAME.w, FRAME.h], viewport: [VIEW.w, VIEW.h],
        capture: { frames: kept.length, span: +span.toFixed(3), avg_fps: +(kept.length / Math.max(span, 0.1)).toFixed(1) },
        cuts: this.cuts, events: this.events, path: this.pathLog, presses: this.presses, late: this.late,
        voice_sig: this.voice?.sig, unchecked_labels: unchecked, preflight: this.preflightLog, fonts: this.fonts,
        secrets: { scans: this.scans, allow: this.secretAllow }, dom_edits: this.domEdits,
      }),
    );
    fs.rmSync(this.framesDir, { recursive: true, force: true });
  }

  async dispose() {
    this.capturing = false;
    if (this.recording && !DRY) await this.cdp.send('Page.stopScreencast').catch(() => {});
    if (!this.done) fs.rmSync(this.framesDir, { recursive: true, force: true });
  }
}

/** Register one Playwright test per scene. SCENES=2,5 runs only those. */
export function scene(n: number, title: string, fn: (s: Scene) => Promise<void>) {
  test(`scene ${n}: ${title}`, async ({ page }) => {
    const only = process.env.SCENES;
    test.skip(!!only && !only.split(',').map(Number).includes(n), 'not selected');
    if (process.env.DEMO_RESET_PER_SCENE === '1' && process.env.DEMO_SEED_CMD) execSync(process.env.DEMO_SEED_CMD, { stdio: 'inherit' });
    const s = new Scene(page, n, title);
    await s.init();
    try {
      await fn(s);
      await s.finish();
    } catch (e) {
      const png = await s.failShot();
      console.error(`[fail] scene ${n}: ${String(e instanceof Error ? e.message : e).split('\n')[0]}\n[fail] screenshot ${path.relative(VDIR, png)}`);
      throw e;
    } finally {
      await s.dispose();
    }
  });
}
