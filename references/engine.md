# Product demo videos

Scripted screen recording of the real UI, an ElevenLabs voice-over, and a composited edit.
Free tools only: Playwright, ffmpeg, Python (numpy, opencv-python-headless), Chromium for the
overlays. The only paid API is ElevenLabs (voice, speech-to-text check, music).
The rules and quality gate are in `method.md`; this file is the engine reference.

```
<project>/demo-videos/          in the project's repo, small
  project.json                  box, app container, git remote
  NN-name/                      narration.txt, video.json, scenes.spec.ts, storyboard.md
~/DemoVideos/<project>/NN-name/ media, outside every repo: out/, audio/, clips/, takes/, review.json
<skill>/engine/                 the one copy of the tooling: lib/, Dockerfile, pinned dependencies
test box: work/dvm/engine, work/dvm/<project>/videos/NN-name
```

## Run

From the project's `demo-videos` folder, with `BOX=~/.claude/skills/demo-video-maker/engine/lib/box.sh`:

```
$BOX init <project>            # starter project.json, then edit it
$BOX new 01-tour               # video folder from the template
$BOX sync                      # engine + video sources to the box, build the image, update the app checkout
$BOX app                       # build + start the app container (no-op when project.json has no app)
$BOX reseed                    # re-run the demo seed in the running app container
$BOX run 01-tour voice --music # voice new/changed lines; the bed is at least the video estimate + 3 s
$BOX run 01-tour record        # SCENES=2,5 limits it
$BOX run 01-tour edit --preview  # 960x540 / 15 fps; --plan writes subtitle positions without rendering
$BOX run 01-tour edit
$BOX run 01-tour gate
$BOX fetch 01-tour             # out/, clip logs and takes to the media folder
$BOX review 01-tour            # local page: change the voice, hear it, keep a take
$BOX apply 01-tour             # kept takes -> re-record changed scenes, edit, gate, fetch
$BOX rerecord 01-tour          # re-record late or low-fps scenes
```

`project.json`: `project`, `box`, and either `base_url` (a public or staging site, no container) or `app` plus `git` (see the
Praxabase project for a full example). `video.json` adds `record_env` (e.g. `{"DEMO_START_LANG": "fa"}`), `record_groups`
(scenes that need their own fresh sandbox, e.g. `[[5, 14]]` so they never see scene 4's writes) and `voice.lines` (written by
the review page).

## Scene API (`lib/record/demo.ts`)

```ts
import { scene } from '../lib/record/demo';
scene(1, 'Title', async (s) => {
  await s.go('/');
  await s.click('text=Create new', { on: '«Create new»' });
});
```
A cue is `{ on: 'exact phrase of the line', nth?: 0, at?: 'start' | 'end' | 0..1 }` (default `start`). Cue time =
`voiceAt` (default 0.5 s) + the phrase's char start time. `«` `»` are ignored when matching. Targets are a CSS selector,
a Playwright `Locator` or a `{x, y, width, height}` box (CSS px, 1280x720 viewport).

| call | does |
|---|---|
| `go(url, {ready})` | load, wait for the optional `ready` selector (late-rendered UI), fonts and two painted frames, pre-flight, then start the clip. Voice starts `voiceAt` later |
| `click(target, cue + {cut})` | eased pointer move that lands just before the cue, press at the cue. Logs the box and the press. `cut: true` starts a cut at the press: pair it with `cutEnd()` once the next state is ready (page load, save, spinner) |
| `hover(target, cue)` | same without the press |
| `look(target, cue + {wide})` | draws the eye to a region (zoom target) without moving the pointer; `wide` asks for a wider view |
| `callout(label, hint, cue + {hold, scope})` | lower third with the exact label and a short hint (also asserts the label) |
| `type(target, text, cue + {delay})` | click the field at the cue, then type char by char. Password-like targets are not logged |
| `scrollTo(target, cue)` | smooth scroll, ends at the cue |
| `label(text, scope?)` | assert an exact label is visible; writes `clips/labels.json`. `«Label»` cues call it for you |
| `cutStart()` / `cutEnd()` | cut slow saves and page loads out of the clip (clip time skips the cut) |
| `finish()` | hold until the line ends + 0.6 s, final pre-flight, encode `clips/scene-NN.mp4`. Called for you if omitted |

Every action lands the press/landing at the cue. A landing more than 0.35 s after its cue logs a warning and is
recorded in `clips/scene-NN.json` (`late`); the gate fails on it. Fix with an earlier cue, fewer moves, or a reworded line.
Output per scene: `clips/scene-NN.mp4` (2560x1440, 30 fps) and `clips/scene-NN.json` (events with boxes, pointer path,
presses, late cues, pre-flight, capture rate).

Environment: `VIDEO` (video dir), `SCENES` (e.g. `2,5`), `BASE_URL`, `UI_LOCALE`, and for sign-in `DEMO_START_LANG`
(demo mode; preferred), `DEMO_ENTRY_URL` or `DEMO_LOGIN_URL` + `DEMO_EMAIL` + `DEMO_PASSWORD` (see `login.setup.ts`
and "Demo mode"; with none set, an empty state is saved).
`DEMO_SEED_CMD` re-runs the demo-world seed before each take (and before each scene with `DEMO_RESET_PER_SCENE=1`).

## Demo mode

The app container starts with `DEMO_ENABLED=1` and the demo feature flags on, and seeds the demo
world with `pnpm demo:seed --lang all` at container start (`box.sh reseed` re-runs it). Recording
signs in through the app's demo mode: set `DEMO_START_LANG=en` (or `fa`) and `login.setup.ts` POSTs
`/api/demo/start` with that lang and the app origin as `Origin`, follows the returned redirect with
`?tour=0`, waits for network idle, and saves the storage state (the `pb_demo` + `pb_lang` cookies ride
along). `DEMO_ENTRY_URL` and `DEMO_LOGIN_URL` still work and come after it in that order.

## video.json

`output`; `voice` (`model_id`, `voice_id`, `language_code`, `stability`, `similarity_boost`, `tags`, `takes`, `stt_model`);
`fonts` (families that must be loaded on the page, e.g. `["Bricolage Grotesque"]`; matched against `document.fonts`);
`intro_card` / `outro_card` (seconds, 0 = none); `cards` (`brand`, `intro`/`outro` with `title`, `subtitle`, `url`);
`card_scenes` (`{"4": {"title", "subtitle", "seconds"}}`: a narrated scene shown as a card); `zoom` (`max`, `min`,
per scene `{"3": {"off": true}}`); `music` (`prompt`, `seconds`, `gain_db`, `duck_db`); `pacing` (`voice_at`, `tail`, `gap`,
`crossfade`); `subtitles` (`{"default": "bottom", "3": "top"}`; otherwise bottom, or top when the action is down there); `subtitle_top_margin` (`{"2": 120}`: px from the top of the 1080 frame for a top subtitle in that scene, to sit below a site nav bar);
`redscan` (`{"allow": {"3": {"frac": 0.03, "why": "..."}}}`: documented per-scene allowance, see "Changes to the method" 17); `theme` (card/overlay CSS variables); `calm_css` (CSS injected in every page to show steps still being set up in a neutral
colour); `max_seconds`; `gate` (`stt_min`, `lufs_tol`, `max_pan`, `max_zoom_rate`, `min_capture_fps`).

## Gate (`lib/gate.py`)

Automated: output shape (1920x1080, 30 fps, audio), duration limit, every `«label»` of the narration was found on screen,
no late cue, pre-flight clean, steady capture rate, red-state scan, camera speed, subtitles never cover an action,
music covers the video, loudness -16 LUFS +-1, STT of the final mix vs `narration.txt`. Writes `out/<name>.gate.json` and
`out/<name>.contact.jpg` (1 frame / 1.5 s). Still manual: an independent reviewer looks at the contact sheet and
transcript, including the zoom moves and the subtitle placement.

## Changes to the method

Settled with the orchestrator:
1. **Recorder pre-flight.** Before a clip starts (and again at its end) the recorder asserts every font in `video.json`
   `fonts` is loaded and that no `[role=alert]` text, toast, spinner or Next error overlay is on screen. A bad take fails
   at once instead of after the edit.
2. **`edit.py --preview`** renders 960x540 at 15 fps for fast iteration; the final render is full size.
3. **Deterministic data.** `DEMO_SEED_CMD` re-runs the seed on every take, so single scenes can be re-recorded and still match.
4. **Late cues fail the gate.** Cue time = voice char timings shifted by `voice_at`; `gate.py` runs every automatable gate item.

Found while building, kept because the first version failed without them:
5. **Screencast needs a Chromium flag.** `deviceScaleFactor: 2` alone still gives 1280x720 frames in headless Chromium;
   `--force-device-scale-factor=2` in the launch args gives real 2560x1440 frames (checked per clip: the take fails otherwise).
6. **Moves are time-driven.** Each CDP mouse move takes ~40 ms under software rendering, so a step-counted move ran 3x
   too long and every cue landed late. Moves now follow the clock; a late start shortens the move instead of delaying it.
7. **Clip starts after the page settled** (load, fonts, two painted frames), so a clip never opens on a blank page.
8. **Audio is mixed in numpy, not an `amix` graph.** The voice track, music bed and ducking envelope are built as one
   waveform (no silent `anullsrc` input needed, no leading-delay trap), then ffmpeg `loudnorm` (two pass) and a single AAC encode.
9. **Subtitle placement is checked, not guessed.** One position per scene, chosen by testing the rendered subtitle boxes
   against the logged action boxes; the overlaps are written to `edit.json` and fail the gate.
10. **Subtitle breaks** also fall after a label that ends its own sentence, never inside a label.
11. **Fonts for cards and overlays** come from pinned `@fontsource` packages, inlined as data URIs, and the renderer fails if a family is missing.
12. **`box.sh`** wraps the box flow (sync, build, app container, run, fetch) with ssh retries.

Found while recording 01-tour (the first run on the demo-mode app):
13. **Record over the app container's network.** The app sets `Secure` cookies, and Chromium stores those only for https or
    `http://localhost`, never for `http://pxb-demo-app:3000` (the demo sign-in silently did nothing). `box.sh run <v> record`
    now starts the recorder with `--network container:pxb-demo-app` and `BASE_URL=http://localhost:3000`.
14. **Cue lands on time.** `look()` resolved its target box after waiting for the cue, so a slow locator made it late (up to
    1.7 s on the box). Geometry is now resolved first, then the clock is awaited. The label check is one DOM walk
    (`labelFind`) instead of Playwright's `getByText` over the whole page (0.3 s each). It matches an element's whole text or
    its own text nodes (a badge `Only holder<bdi>Name</bdi>`), tolerates curly or straight quotes around a quoted line and
    curly apostrophes, and requires the element to be visible in the viewport.
15. **Pre-flight fonts only where used.** Chromium loads a web font lazily, so a page with no monospace text never fetches
    JetBrains Mono. A family in `fonts` is now required on a page only when a rendered element with text uses it (the landing page
    does, the app pages do not). A determinate `role=progressbar` (has `aria-valuenow`, e.g. onboarding progress) is not a spinner.
16. **`click(..., {cut: true})`** drops the frames after a press until `cutEnd()`: the click lands, then the result is on screen,
    with no page load, "Starting..." or "Looking through..." on camera.
17. **Red-scan allowances.** The app's red "only holder" badges (Dashboard, Analytics) and the bus-factor bar are the product's
    risk signal, not an error. `video.json` `redscan.allow` maps a scene to `{frac, why}`; `gate.py` turns it into time
    ranges of the final video and passes them to `redscan.sh` (`RED_ALLOW`), where frames up to `frac` red are counted as
    "allowed" and listed. Used by 01-tour scene 14 only (Analytics: bus-factor bar and "Only holder" badges, 1.4 % of the frame at most; the limit is 3 % there). Anything larger (a red toast, an alert banner, a validation block) still fails, and every other
    scene keeps the 0.4 % limit.
18. **Subtitles.** 46 px on a calm translucent plate (74 % ink, slight blur), up to 66 characters per chunk, bottom unless the
    action is there. A label longer than the plate wraps instead of running off the frame (RTL labels still never wrap).
19. **Voice recheck** accepts a one-letter STT spelling difference in a long spoken label (`Rajaee` heard as `Rajaie`).
20. **Box load.** The test box is shared (Go builds of other projects run on it at load 10-15), which dropped the screencast to
    7-17 fps. The recorder container runs with `--cpu-shares 262144`, the JPEG screencast quality is 65, and `SCENE_TIMEOUT_MS`
    overrides the per-scene Playwright timeout (default 180 s) for debugging.
21. **Detached steps.** The box drops ssh sessions under load, which killed a 10-minute edit. `box.sh run` starts the container
    with `docker run -d` and follows `docker logs -f`, re-attaching after a drop; the exit code is the container's.

22. **Dev origin never on screen.** The sandbox is served from `http://localhost:3000` on the box, and the app builds the employee
    link in the browser from `window.location.origin` (no server env changes it). A viewer reads that as a dev build. The
    pre-flight now fails a take with `localhost`, `127.0.0.1` or `pxb-demo-app` in the page text or any input value, and the
    01-tour spec rewrites the displayed link origin to `https://praxabase.com` while the scene is cut.
23. **Voice-only respelling.** `video.json` `voice.respell` (`{"Praxabase": "Praxa-bays"}`) changes only the text sent to the
    TTS; the alignment is mapped back onto the real spelling, so subtitles and labels keep "Praxabase". `voice.must_hear`
    makes a take that STT does not hear as written count as a miss, so up to `takes` takes are tried. Why: STT heard the plain
    spelling as "Praxibase" in 0 of 3 isolated clips; "Praxa-bays" was heard as written 4 of 4.
24. **STT diff without autojunk.** `difflib.SequenceMatcher` junks frequent characters in strings over 200 characters, so one
    proper-noun difference ("Kavir" heard as "Cavir") made it drop a 250-character block (ratio 0.907 instead of 0.994) and the
    gate failed a correct mix. `gate.py` and `voice.py` pass `autojunk=False`.
25. **`scrollTo(..., {align: 'bottom'})`** puts the target's bottom edge on the viewport's bottom edge. It keeps a row that follows
    it (a seeded failed import under «Recent imports») out of the picture, and scrolls targets out of the bottom band so a
    bottom subtitle stays clear of them. `edit.py --plan` writes subtitle positions and overlaps without rendering.

Found while making 01-tour-fa (Persian UI, Persian narration):
26. **RTL label isolates.** A subtitle label was wrapped in U+2066 (left-to-right isolate), which laid a Persian label out
    left to right and reversed its word order. Each label `<mark>` now gets `dir` from its first strong letter: a Persian label
    stays right to left and may wrap, an English label inside Persian text stays left to right and never wraps.
27. **RTL cards.** Bricolage and Atkinson have no Arabic-script glyphs. A card whose title or subtitle is Persian switches to
    `dir=rtl` and Vazirmatn (Latin falls back to the brand fonts), and the renderer then requires Vazirmatn to be loaded.
28. **Persian STT folding.** The label and transcript checks fold Arabic and Persian letter forms (ي/ی, ك/ک, ة/ۀ/ه), drop
    ZWNJ, tatweel and diacritics, map Persian and Arabic-Indic digits to Latin, and write numbers out as Persian words when the
    text is Persian. A Persian label also counts as heard at 0.85 similarity without spaces and with the ezafe folded: STT
    spells the ezafe three ways (نسخهٔ / نسخه‌ی / نسخه), splits or joins words (می شود / میشود) and spells loanwords its own way
    (دموراژ / دماراژ). A label word that may carry a negation prefix (ن, نا, نمی, غیر) must still be heard exactly, so a take
    that drops it (ناموفق heard as موفق) fails.
29. **Quoted labels in «».** Persian pages render a quote as «…»; `labelFind` tolerates «» around a quoted line like “…”.
30. **The music bed is never shorter than the video.** `music.seconds` was a fixed 300 s; the Persian script runs ~25 % longer
    than the English one. The bed is now at least the video estimate + 3 s, and a bed that is too short is made again.
31. **`voice.speed`** (optional, 0.7-1.2) is sent to ElevenLabs. With eleven_v4 it changed the Persian length by under 4 %;
    tightening the lines did the rest.
32. **Brand and symbols in Persian TTS.** «پراکسابیس» is respelled «پْرَکسا بِیس» (STT heard "Praxabase" 4 of 4; the plain spelling
    gave "Paraxabis"), and a label with « / » is respelled with a pause, else the voice says "اسلش".

Found while listening to 01-tour-fa:
33. **Review page (`box.sh review`).** Speech-to-text cannot hear vowels, so a person must. The page voices the whole scene and
    plays from the changed sentence. "Keep" stores that exact take with the signature `voice.py` computes, so nothing re-rolls the
    pronunciation that was approved. A sentence-only preview would be cheaper but could not guarantee that.
34. **`voice.lines`.** A per-scene voice text, written by the review page, for a fix in one scene only; `voice.respell` is for a word
    that is wrong everywhere. Subtitles keep the real text; the alignment is mapped back with a diff.
35. **Clip and take must match.** Each clip log stores the signature of the voice take it was recorded against; the gate fails a mismatch and a scene with no clip.
36. **Media outside the repo.** Removing a worktree no longer deletes the only copy of a finished video, and the repo carries sources only.

## Known limits

- Capture is ~20-25 fps on test_vps (software rendering at 2x). The pointer and ripple are drawn from logs at 30 fps, so
  they are smooth; page animations can look slightly steppy. The gate fails below 20 fps.
- The pointer is not in the recording; it is drawn in the edit. Hover states in the page are real.
- Zoom targets come from `click`, `hover`, `look`, `type` boxes only. Callouts are placed in the emptiest corner away from them.
- Subtitle position is one per scene; a scene whose action fills both the top and the bottom needs a reworded line or a split scene.
- Cards use the Praxabase palette by default; override with `theme`.
- Right-to-left (fa) subtitles and cards were first used by 01-tour-fa. The landing page has no Persian version, so a Persian
  video starts from `/signup` (signed out, `pb_lang=fa`).
- Local runs on a Mac need Playwright's Chromium; the pinned Docker image is the reference environment.
