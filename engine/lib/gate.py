#!/usr/bin/env python3
"""Automated quality gate: runs every gate item that a script can check.

  uv run lib/gate.py <video> [--no-stt]

Writes out/<name>.gate.json and the contact sheet out/<name>.contact.jpg.
Exit code 0 only when every check passes. The independent human/agent review of frames and
transcript stays manual (see README).
"""
import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

LABEL_RE = re.compile(r"«([^»]+)»")


def norm(s):
    from voice import FA_FOLD  # same folder

    return re.sub(r"[\W_]+", "", s.translate(FA_FOLD).lower())


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,avg_frame_rate,duration", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    vdir = (Path(args[0]) if Path(args[0]).is_dir() else ROOT / args[0]).resolve()
    cfg = json.loads((vdir / "video.json").read_text())
    name = cfg.get("output", vdir.name)
    gcfg = cfg.get("gate", {})
    out = vdir / "out"
    checks = []

    def add(key, ok, detail):
        checks.append(dict(check=key, ok=bool(ok), detail=detail))
        print(("PASS " if ok else "FAIL ") + key + ": " + (detail if isinstance(detail, str) else json.dumps(detail)))

    subs_mp4, nosubs_mp4 = out / f"{name}.mp4", out / f"{name}-nosubs.mp4"
    edit_p = out / f"{name}.edit.json"
    edit = json.loads(edit_p.read_text()) if edit_p.exists() else {}

    # 1. outputs exist with the right shape
    shapes = {}
    for p in (subs_mp4, nosubs_mp4):
        if not p.exists():
            add(f"output {p.name}", False, "missing")
            continue
        info = probe(p)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        a = [s for s in info["streams"] if s["codec_type"] == "audio"]
        num, den = v["avg_frame_rate"].split("/")
        shapes[p.name] = dict(w=v["width"], h=v["height"], fps=round(int(num) / int(den), 2), audio=bool(a), dur=round(float(info["format"]["duration"]), 2))
        s = shapes[p.name]
        add(f"output {p.name}", s["w"] == 1920 and s["h"] == 1080 and abs(s["fps"] - 30) < 0.1 and s["audio"], s)
    if len(shapes) == 2:
        d = [s["dur"] for s in shapes.values()]
        add("subs and nosubs same length", abs(d[0] - d[1]) < 0.1, d)

    # 2. duration limit
    limit = float(cfg.get("max_seconds", gcfg.get("max_seconds", 180)))
    if subs_mp4.exists():
        add("duration under limit", shapes[subs_mp4.name]["dur"] < limit, f"{shapes[subs_mp4.name]['dur']}s < {limit}s")

    # 3. labels: every quoted label in the narration was found on screen
    lines = (vdir / "narration.txt").read_text().splitlines()
    # a clip's cues only fit the voice take it was recorded against; edit.py would hold its last frame over a longer take
    clips = {n: json.loads(p.read_text()) for n in range(1, len(lines) + 1) for p in [vdir / "clips" / f"scene-{n:02d}.json"] if p.exists()}
    spoken = [n for n, l in enumerate(lines, 1) if l.strip()]
    add("every scene has a clip", all(n in clips for n in spoken), dict(missing=[n for n in spoken if n not in clips]))
    stale = [n for n in clips if clips[n].get("voice_sig") and clips[n]["voice_sig"] != json.loads((vdir / "audio" / f"scene-{n:02d}.json").read_text()).get("sig")]
    add("every clip matches its voice take", not stale, dict(re_record=stale))
    labels_p = vdir / "clips" / "labels.json"
    seen = json.loads(labels_p.read_text()) if labels_p.exists() else {}
    found = {(v["scene"], norm(v["label"])) for v in seen.values() if v["found"]}
    bad = {f"scene {v['scene']}: {v['label']}" for v in seen.values() if not v["found"]}
    missing = [f"scene {n}: {l}" for n, line in enumerate(lines, 1) for l in LABEL_RE.findall(line) if (n, norm(l)) not in found]
    add("labels found on screen", not bad and not missing, dict(not_found=sorted(bad), never_checked=missing))

    # 4-6. per-scene recorder logs: late cues, preflight, capture rate
    late, pre, slow = [], [], []
    for lp in sorted((vdir / "clips").glob("scene-*.json")):
        if lp.name == "labels.json":
            continue
        log = json.loads(lp.read_text())
        late += [dict(scene=log["scene"], **l) for l in log["late"]]
        pre += [dict(scene=log["scene"], **p) for p in log["preflight"] if not p["ok"]]
        if log["capture"]["avg_fps"] < gcfg.get("min_capture_fps", 20):
            slow.append(dict(scene=log["scene"], avg_fps=log["capture"]["avg_fps"]))
    add("no cue landed late (> 0.35 s)", not late, late or "ok")
    add("preflight clean (fonts loaded, no error toast/alert/spinner)", not pre, pre or "ok")
    add("screencast kept a steady rate", not slow, slow or "ok")

    # 7. red-state scan of the final picture
    if nosubs_mp4.exists():
        # documented allowances (video.json redscan.allow: {"<scene>": {"frac", "why"}}): intentional red product UI
        allow = []
        for it in edit.get("items", []):
            a = cfg.get("redscan", {}).get("allow", {}).get(str(it.get("scene")))
            if a:
                allow.append([it["start"], it["start"] + it["keep"], a["frac"], a["why"]])
        env = dict(os.environ, RED_ALLOW=json.dumps(allow))
        r = subprocess.run([str(ROOT / "lib/record/redscan.sh"), str(nosubs_mp4)], capture_output=True, text=True, env=env)
        add("red-state scan clean", r.returncode == 0, dict(summary=r.stdout.strip().splitlines()[0] if r.stdout else r.stderr[-200:], allowances={str(k): v for k, v in cfg.get("redscan", {}).get("allow", {}).items()}))

    # 8. edit decisions: camera speed, subtitles never cover the action
    if edit:
        zs = edit["zoom"]
        worst_pan = max((z["max_pan_css_px_per_s"] for z in zs.values()), default=0)
        worst_z = max((z["max_zoom_log_per_s"] for z in zs.values()), default=0)
        add("camera moves are smooth", worst_pan <= gcfg.get("max_pan", 900) and worst_z <= gcfg.get("max_zoom_rate", 0.9), dict(pan_css_px_per_s=worst_pan, zoom_log_per_s=worst_z))
        add("subtitles never cover the action", not edit["subtitle_overlaps"], edit["subtitle_overlaps"] or "ok")
        m = edit["audio"].get("music")
        if m:
            add("music bed covers the video", not m["shorter_than_video"], m)

    # 9. loudness of the final mix
    if subs_mp4.exists():
        r = subprocess.run(["ffmpeg", "-nostats", "-i", str(subs_mp4), "-af", "ebur128=framelog=quiet", "-f", "null", "-"], capture_output=True, text=True)
        m = re.findall(r"I:\s+(-?\d+\.?\d*) LUFS", r.stderr)
        lufs = float(m[-1]) if m else None
        add("loudness near -16 LUFS", lufs is not None and abs(lufs + 16) <= gcfg.get("lufs_tol", 1.0), f"{lufs} LUFS")

    # 10. speech-to-text of the final mix against the narration
    if "--no-stt" not in sys.argv and subs_mp4.exists():
        try:
            from voice import call, norm_plain, norm_words, stt  # same folder
            mp3 = out / f".gate-{name}.mp3"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(subs_mp4), "-vn", "-ac", "1", "-b:a", "96k", str(mp3)], check=True)
            text, model = stt(cfg, mp3.read_bytes())
            mp3.unlink()
            (out / f"{name}.stt.txt").write_text(text.strip() + "\n")
            want = re.sub(r"\[[^\]]*\]", "", " ".join(lines))
            heard = re.sub(r"[\[(][^\])]*[\])]", "", text)  # drop [music] style tags
            ratio = max(  # STT writes numbers as words; the digit form is also accepted
                difflib.SequenceMatcher(None, norm_words(want), norm_words(heard), autojunk=False).ratio(),
                difflib.SequenceMatcher(None, norm_plain(want), norm_plain(heard), autojunk=False).ratio(),
            )
            add("final-mix STT matches narration", ratio >= gcfg.get("stt_min", 0.92), dict(ratio=round(ratio, 4), model=model, transcript=text))
        except (SystemExit, RuntimeError) as e:
            add("final-mix STT matches narration", False, f"STT not run: {e}")

    # contact sheet (1 frame / 1.5 s) for the manual frame review
    sheet = None
    if subs_mp4.exists():
        sheet = out / f"{name}.contact.jpg"
        subprocess.run([str(ROOT / "lib/record/grid.sh"), str(subs_mp4), str(sheet), "1.5"], check=True, capture_output=True)

    ok = all(c["ok"] for c in checks)
    (out / f"{name}.gate.json").write_text(json.dumps(dict(video=name, ok=ok, checks=checks, contact_sheet=str(sheet.relative_to(vdir)) if sheet else None), indent=1))
    print("GATE", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
