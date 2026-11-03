---
name: demo-video-maker
description: Make product demo videos from the real UI with a scripted Playwright recording, an ElevenLabs voice-over, burned-in subtitles and an automated quality gate, in any language including Persian/RTL. Use when asked to make, re-record, translate or fix a demo video, product tour, walkthrough or screencast, or to fix how a word is pronounced in one.
---

# Demo video maker

The engine is `engine/` in this skill. A project keeps only `demo-videos/project.json` and one folder per video. Media goes to `~/DemoVideos/<project>/<video>/`, never into git.

Read [references/method.md](references/method.md) (rules and gate) and [references/engine.md](references/engine.md) (commands, Scene API, lessons) before the first command. For a right-to-left video also read [references/rtl.md](references/rtl.md).

```bash
BOX=~/.claude/skills/demo-video-maker/engine/lib/box.sh   # run from the project's demo-videos folder
```

## Settle first

Project and URL, the account or demo sign-in, UI language, narration language, voice id, **target platform** (where it will play: site embed, YouTube, social feed, GIF in a README). Ask the platform before recording. The engine makes one shape only, 16:9 1920x1080 at 30 fps, recorded from a 1280x720 viewport: say so, and if the platform wants vertical or square (9:16, 1:1, 4:5) tell the user it is not supported rather than cropping the 16:9 cut. A platform's length limit goes in `max_seconds`. A voice the user has already named wins. Ask only for what the repo cannot answer.

## Steps

1. **Look at the real UI.** Write a throwaway spec that dumps each page's text and a screenshot, and quote labels from that, not from the source. Labels differ per language and pages drift.
2. **Write** `storyboard.md`, `narration.txt` (line N = scene N) and `scenes.spec.ts`. Translations are rewritten as natural speech, not translated. Put every on-screen label in `«»`, exactly as rendered: the recorder asserts each one.
3. **Voice.** `$BOX sync`, `$BOX app`, `$BOX run <v> voice`, `$BOX fetch <v>`. Check total voice length against the limit before recording (video is about voice + 18 s). Tighten lines rather than speeding up the voice.
4. **Listen.** Start `$BOX review <v>` in the user's terminal and give them `http://127.0.0.1:8765/`. They edit, hear and keep takes themselves. Speech-to-text cannot hear vowels, so never claim a pronunciation is right from a transcript. A word wrong everywhere goes in `voice.respell`, then voice those scenes again in the page: a take the page did not keep is voiced unheard.
5. **Record, edit, gate.** `$BOX sync` (carries kept takes), then `$BOX run <v> voice --music`, `record`, `edit`, `gate`, `fetch`. After this first full recording, `$BOX apply <v>` ships later kept takes; it cannot build a first cut. The gate names any scene whose clip no longer matches its take: re-record it with `SCENES=`.
6. **Independent review.** Four fresh agents in parallel on `fable` or `opus`, one lens each (design/layout, text/readability, pacing/narrative, brand vs real product; lens checklists in `method.md`). Each gets the review frames (`out/<name>.review/`), the contact sheet, `edit.json`, the gate result, the transcript and the step 1 UI dump, and returns findings only. Merge them into one fix plan (dedupe, severity, then re-edit / re-record / re-voice), fix, re-review the lenses that had findings. Never review your own cut or tooling change in the same agent.
7. **Listen to the final** with the user, then deliver. Send the contact sheet, transcript and loudness. A GIF on request: `$BOX run <v> gif` (no audio; `--subs` for burned-in words).

**Later:** the app keeps moving. Run `$BOX drift <v>` before relying on an old video: it runs every step and label against the current app without filming or spending, and lists what no longer matches.

Done means both mp4s under the limit, every gate item PASS, reviewer findings fixed, the user has listened, and the sources committed.

## Rules

- Heavy work (record, edit, gate) runs in Docker on the box in `project.json`, never on the Mac. The review page and the first look at media run on the Mac.
- `ELEVENLABS_API_KEY` from the environment only. Never in a file, argv or a log.
- Local or staging data only, never production data.
- Pin every image by `@sha256:` and every dependency exactly. Load `docker-optimization` before editing the `Dockerfile`, `supply-chain-security` before changing dependencies.
- Never push demo work to `origin` unless the user asked. The box remote is disposable and may be force-pushed; shared branches never.
- Send bulk writing (storyboard tables, label source lists) to opencode, and check it against measured numbers. Its estimates go stale.
- Before spending on voice, confirm the labels. After a change to a line, its scene must be re-recorded because the timing shifts.
- A lesson from a failed take goes in the engine (a check in `gate.py`, the pre-flight or `box.sh`) and a line in `references/engine.md`, not in this file.
