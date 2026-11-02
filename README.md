# demo-video-maker

A Claude Code skill that makes product demo videos from the real UI: a scripted Playwright recording timed to an ElevenLabs voice-over, a Python compositor with auto-zoom and burned-in subtitles, and an automated quality gate. It works in right-to-left languages, and it has a local page to hear and fix pronunciation before you ship.

## Install

```bash
git clone git@github.com:hadifarnoud/demo-video-maker.git ~/.claude/skills/demo-video-maker
```

Then ask Claude Code for a demo video. `git pull` in that folder updates it.

## You need

- `ELEVENLABS_API_KEY` in your shell. It is read from the environment only.
- An SSH host with Docker for the heavy work, named in each project's `project.json`.
- `python3`, `rsync` and `ffprobe` on your own machine, for the review page and media sync.

## Layout

- `SKILL.md` is the workflow the agent follows.
- `references/` holds the rules (`method.md`), the commands and lessons (`engine.md`) and the Persian and RTL notes (`rtl.md`).
- `engine/` is the tooling, with images and dependencies pinned. `engine/lib/box.sh` is the entry point.

A project keeps only `demo-videos/project.json` and one folder per video. Finished media goes to `~/DemoVideos/<project>/<video>`, outside every repo.

```bash
~/.claude/skills/demo-video-maker/engine/lib/box.sh init my-project
~/.claude/skills/demo-video-maker/engine/lib/box.sh new 01-tour
```
