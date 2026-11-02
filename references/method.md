# Product demo videos: method (user's spec, verbatim + orchestrator decisions at the end)

Make demo videos from the real product UI: a scripted screen recording, an AI voice-over, and a composited edit. Build the tooling in the project if it does not exist yet; this file is the full spec.

Free and open-source tools only: Playwright, ffmpeg, Python with numpy and opencv-python-headless (pinned), and Chromium for the overlays. The only paid API is ElevenLabs, for the voice and the music. Do not use Remotion, paid editors or SaaS video tools.

## Inputs to settle first
- The account to record with, the UI language, and the narration language.
- The voice: the newest ElevenLabs model (older ones sound flat), a voice id, a language code, and delivery tags. The API key comes from the environment. Never write it into a file.

## Rules
1. Use real data, never lorem ipsum. Seed a believable demo world: a named company, real-looking people and emails, cities, dates and history over the last few months, and lists, templates and records with real names. Write the seed as a script that can run again safely.
2. Quote UI labels verbatim. The narration names every UI item by its exact on-screen label, in quotes («Create new», «Publish»), never translated. The recorder checks that each named label is on screen.
3. Record the UI in the language production users see. Do not switch the UI language for the video. The data can be in the audience's language.
4. Load the production fonts. Check what production really loads. If a font is missing, the take fails.
5. Voice first. Write the narration (one line per scene), voice it, then record. Anchor each action to the words that name it, not to timing numbers.
6. No error states on camera. Show no red validation, error toasts or spinners unless the scene is about them. If showing the real error once teaches something, show it briefly, then return to a calm state.
7. Work on a local or staging stack, never production data.

## Layout
See `engine.md`. The engine lives in this skill; a project holds only `project.json` and its video folders.

## 1. Voice (`voice.py <video> [scene] [--force] [--music]`)
- Voice a line only when it is new or has changed, unless `--force` is given.
- Send each line to ElevenLabs `POST /v1/text-to-speech/{voice_id}/with-timestamps` with `model_id`, `language_code`, `voice_settings` (stability about 0.3, similarity_boost 0.8) and output mp3. Save `audio/scene-NN.mp3` and `audio/scene-NN.json` (start/end time of each character).
- Put one audio tag before each line to set the delivery, e.g. `[warm, enthusiastic, smiling]`. Not spoken, not subtitled. A line can carry its own tags.
- Check each take with ElevenLabs speech-to-text (scribe): every quoted label must be heard; transcribed words must match the line. Up to 3 takes, keep the best.
- To fix one pronunciation, change only that line's text (diacritics/spelling) and re-voice that scene. If timing barely changes, re-edit, don't re-record.
- `--music` makes a music bed with the ElevenLabs music API: prompt from video.json, length = video + 3 s or more. Never loop the bed.

## 2. Record (Playwright; `VIDEO=<dir> npx playwright test`)
Capture:
- 1280x720 CSS viewport.
- CDP `Page.startScreencast` at device scale factor 2 → 2560x1440 frames so zoom has real pixels. Force the scale in config; headless Chromium otherwise sends CSS-size frames.
- Inject an invisible 2 px element that keeps animating (steady ~30 fps).
- Sign in once in a setup project, off camera, reuse storage state.
Scene API in `demo.ts` (each scene reads its `audio/scene-NN.json`):
- `s.go(url)` starts the clip; voice starts `voiceAt` (~0.35-0.5 s) later.
- `s.click(target, { on: '«Label»' })`: pointer starts early enough that the press lands when the voice says the cue. Cue = any exact phrase of the line, optional `nth` and `at: 'start'|'end'|0..1`.
- `s.hover`, `s.look(box)`, `s.callout(label, hint)` take cues the same way.
- `s.type(target, text, { on })` types one char at a time.
- Pointer moves on eased curves at human speed.
- Action landing >0.35 s after its cue → warn and log. Fix with earlier cue, fewer moves, or reworded line.
- `s.label(text, scope)` asserts exact label visible, writes to `clips/labels.json`.
- `s.cutStart()` / `s.cutEnd()` cut slow saves and page loads.
- `s.finish()` holds until the line ends + ~0.6 s.
Logs/output:
- Log every click, typed field and look (time, bounding box), the pointer path, and voice start to `clips/scene-NN.json`.
- Encode frames to `clips/scene-NN.mp4` (VFR to 30 fps), delete raw frames.
- Optional "calm" mode: steps still being set up show neutral/amber instead of red.
Review: contact sheet per clip (`grid.sh`); `redscan.sh` HSV threshold for error-red on sampled frames.

## 3. Edit (`uv run lib/edit.py <video>`)
Composite every frame in Python/OpenCV at 1920x1080 30 fps, pipe to ffmpeg (libx264).
- Pacing: cut each clip to end of its line + ~0.35 s, or last action + 0.5 s if later. Lines ~0.85 s apart.
- Auto-zoom: join logged targets close in time/place; per group one eased (cosine) move in to 1.35-1.75x starting ~1 s before the action, out ~1.3 s after. Limit camera speed. Sub-pixel affine warp of the 2x frame (no jitter). An action can ask for a wider view (e.g. dialog).
- Pointer: draw from logged path, ripple on each press, hide when idle.
- Cuts: 0.5 s crossfade with slight zoom-through.
- Cards: animated intro/outro from HTML with CSS animations, rendered frame by frame in Chromium with animations paused and stepped.
- Callouts: lower third with exact label + short hint, in the emptiest area, away from action and subtitles.
- Subtitles: render yourself as PNG overlays from HTML (no libass: it reverses RTL word order). Time from voice char timings. Break only at clause marks outside quoted labels. In RTL text, put each LTR label in an isolate (U+2066 … U+2069), never break it, highlight it. One position per scene: bottom, or top for the whole scene if the action is under the subtitles.
- Audio: one voice track with each line at its clip's voice start. Silent `anullsrc` bed as first `amix` input (or amix drops leading delay). Normalise once to -16 LUFS. Duck music under voice (~-12 dB). Encode AAC once.
- Output: `out/<name>.mp4` with subs, `out/<name>-nosubs.mp4`, `out/<name>.edit.json` (decisions).
`video.json` keys: `output`; `voice` (`model_id`, `voice_id`, `language_code`, `stability`, `similarity_boost`, `tags`, `takes`); `intro_card` (seconds); `cards` (intro/outro `title`, `subtitle`, `url`); `card_scenes`; `zoom` (`max`, per scene `{"3": {"off": true}}`); `music` (`prompt`, `seconds`, `gain_db`); `pacing` (`voice_at`, `tail`); `subtitles` (position per scene).

## Quality gate (before you show anyone)
- `labels.json` shows every quoted label found. No cue landed late.
- Red-state scan clean: no error toasts, no spinners.
- Contact sheet of final video (1 frame / ~1.5 s): frames around 2-3 zoom moves smooth; subtitles never cover the action.
- STT of final mix matches narration.txt.
- Loudness ~-16 LUFS (`ffmpeg -i out.mp4 -af ebur128 -f null -`).
- Independent reviewer (another agent/model) checks frames and transcript. Fix, re-review.

## Known traps
- ffmpeg `tpad` after `trim` on webm does not lengthen the clip; use `loop` to hold a frame.
- Clicks where audio segments join: build one mixed voice track.
- Screencast stops sending frames on a still page; keep something repainting.
- Missing font in headless Chromium falls back quietly; assert it loaded.
- `rm` inside a nested shell can be blocked; use a script with `mv` to a trash dir.

---
## Orchestrator decisions (settled inputs + improvements)
- Voice: model `eleven_v4` (newest TTS model on the account), voice `hpp4J3VqNfWAUOO0d1Us` (Bella - Professional, Bright, Warm; en-US), `language_code` `en`, default tags `[warm, confident, friendly]`. fa follow-up later: voice `1chyhyGkILtoINMXV7RK` (Niloofar, fa cloned).
- STT: ElevenLabs `POST /v1/speech-to-text` with `model_id` = newest scribe model listed by the API (`scribe_v2` or newer; fall back to `scribe_v1`).
- UI language en, narration en. Recording account: the seeded demo world (the app's demo mode); the seed lives in the app repo (scripts), `demo-videos/lib/seed/` wraps it.
- Production fonts (src/app/(site)/layout.tsx, next/font/google, self-hosted at build): Bricolage Grotesque (headings), Atkinson Hyperlegible Next (body), Vazirmatn (fa), JetBrains Mono (ids). Assert `document.fonts.check()` for each used family on every take; fail the take otherwise. Cards/overlays use the same families (bundle the font files from pinned @fontsource packages so rendering is offline and deterministic).
- Heavy work (recording, edit) runs in Docker on SSH host `test_vps` (x86, 4 cores, 15 GB). Add `demo-videos/Dockerfile` (base image pinned by `@sha256:` digest; Playwright pinned exactly; Python deps pinned; `npm ci`).
- Improvements over the method (keep a "Changes to the method" section in demo-videos/README.md explaining each):
  1. Recorder pre-flight: before a take, assert fonts loaded AND no `[role=alert]`/toast/spinner present, so a bad take fails fast instead of after the edit.
  2. `edit.py --preview` renders at 960x540 / 15 fps for fast iteration; the final render is full size.
  3. Deterministic data: scenes run against a freshly reset demo world each take (re-run the seed), so clips can be re-recorded one at a time and still match.
  4. Cue timing uses the voice char timings shifted by `voice_at`; late cues > 0.35 s fail the quality gate script (`lib/gate.py <video>`) which runs every gate item it can automate (labels, late cues, redscan, loudness, duration < limit, STT diff) and writes `out/<name>.gate.json`.
