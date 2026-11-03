#!/usr/bin/env python3
"""Review frames: about 20 stills of the finished video, taken at the moments a reviewer must see, not at even spacing.

  python lib/frames.py <video> [--n 20] [--src out/<name>.mp4]

Moments come from data the edit and the recorder already wrote: scene boundaries (mid-crossfade), zoom peaks (edit.json),
narrated clicks, typed fields and callouts (clip logs). Too many candidates are thinned to an equal share per kind, spread over
the timeline; too few are topped up at the widest gap. Writes out/<name>.review/frame-NN-<kind>-sSS-tT.jpg (1280x720) and
index.json (time, kind, scene, the subtitle on screen, the cue). gate.py runs it after the contact sheet.
"""
import json
import subprocess
import sys
from pathlib import Path

PRIORITY = {"peak": 3, "click": 3, "callout": 2, "boundary": 2, "start": 4, "end": 4, "fill": 0}
MIN_GAP = 0.9  # s between two frames
HOLD_AFTER = 0.4  # edit.py build_camera: the camera holds this long after the last target of a group


def candidates(edit, clips):
    """-> [dict(t, kind, scene, why)] on the final timeline (s). edit: out/<name>.edit.json, clips: {scene: clip log}."""
    xf = edit.get("crossfade", 0.5)
    total = edit["total"]
    items = edit["items"]
    out = []
    for j, it in enumerate(items):
        s, k = it["start"], it["keep"]
        if j:
            out.append(dict(t=s + xf / 2, kind="boundary", scene=it.get("scene"), why=f"crossfade into {it['id']}"))
        if j == 0:
            out.append(dict(t=s + 0.3, kind="start", scene=it.get("scene"), why="first frames"))
        if j == len(items) - 1:
            out.append(dict(t=s + k - 0.3, kind="end", scene=it.get("scene"), why="last frames"))
        if it["kind"] != "scene":
            continue
        z = edit["zoom"].get(it["id"], {})
        for g in z.get("groups", []):
            if g["z"] > 1.0:
                out.append(dict(t=s + (g["t0"] + g["t1"] + HOLD_AFTER) / 2, kind="peak", scene=it["scene"], why=f"zoom {g['z']}x"))
        log = clips.get(it["scene"], {})
        for e in log.get("events", []):
            if e["kind"] in ("click", "type") and e.get("cue"):
                out.append(dict(t=s + e["t"] + 0.25, kind="click", scene=it["scene"], why=f"{e['kind']} on \"{e['cue']}\""))
            elif e["kind"] == "callout":
                out.append(dict(t=s + e["t"] + 0.6, kind="callout", scene=it["scene"], why=f"callout {e.get('label', '')}"))
    out = [c for c in out if 0.05 <= c["t"] <= total - 0.05]
    return sorted(out, key=lambda c: c["t"])


def spread(cs, k):
    """k of cs, evenly across the list (already in time order)."""
    if k >= len(cs):
        return list(cs)
    return [cs[round(i * (len(cs) - 1) / (k - 1))] for i in range(k)] if k > 1 else [cs[len(cs) // 2]]


def pick(cands, total, n=20):
    """Keep n: first and last frame, then an equal share per kind (boundary, peak, click, callout), each spread over the timeline.
    A share a kind cannot fill goes to the others. Frames closer than MIN_GAP lose to the higher priority; gaps are topped up at the widest."""
    by = {}
    for c in sorted(cands, key=lambda c: c["t"]):
        by.setdefault(c["kind"], []).append(c)
    keep = [c for k in ("start", "end") for c in by.get(k, [])]
    kinds = [k for k in ("boundary", "peak", "click", "callout") if by.get(k)]
    left, quota = n - len(keep), {}
    while kinds and left > 0:  # deal one frame at a time to each kind that still has some
        for k in list(kinds):
            if left and quota.get(k, 0) < len(by[k]):
                quota[k] = quota.get(k, 0) + 1
                left -= 1
            if quota.get(k, 0) >= len(by[k]):
                kinds.remove(k)
    for k, q in quota.items():
        keep += spread(by[k], q)
    cs = []
    for c in sorted(keep, key=lambda c: c["t"]):  # MIN_GAP: the higher priority wins
        if cs and c["t"] - cs[-1]["t"] < MIN_GAP:
            if PRIORITY[c["kind"]] > PRIORITY[cs[-1]["kind"]]:
                cs[-1] = c
            continue
        cs.append(c)
    while len(cs) < n:  # top up in the widest gap
        pts = [0.0] + [c["t"] for c in cs] + [total]
        a, b = max(zip(pts, pts[1:]), key=lambda p: p[1] - p[0])
        if b - a < 2 * MIN_GAP:
            break
        cs.append(dict(t=(a + b) / 2, kind="fill", scene=None, why="even spacing"))
        cs.sort(key=lambda c: c["t"])
    return cs


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = sys.argv[1:]
    n = int(flags[flags.index("--n") + 1]) if "--n" in flags else 20
    src = flags[flags.index("--src") + 1] if "--src" in flags else None
    vdir = (Path(args[0]) if Path(args[0]).is_dir() else Path(__file__).resolve().parent.parent / args[0]).resolve()
    cfg = json.loads((vdir / "video.json").read_text())
    name = cfg.get("output", vdir.name)
    edit = json.loads((vdir / "out" / f"{name}.edit.json").read_text())
    clips = {p: json.loads((vdir / "clips" / f"scene-{p:02d}.json").read_text()) for p in {i["scene"] for i in edit["items"] if i["kind"] == "scene"} if (vdir / "clips" / f"scene-{p:02d}.json").exists()}
    mp4 = Path(src) if src else vdir / "out" / f"{name}.mp4"
    outdir = vdir / "out" / f"{name}.review"
    outdir.mkdir(exist_ok=True)
    for old in outdir.glob("frame-*.jpg"):
        old.unlink()
    picks = pick(candidates(edit, clips), edit["total"], n)
    index = []
    for i, c in enumerate(picks, 1):
        sc = f"s{c['scene']:02d}" if c["scene"] else "s--"
        f = outdir / f"frame-{i:02d}-{c['kind']}-{sc}-t{c['t']:06.2f}.jpg"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{c['t']:.3f}", "-i", str(mp4), "-frames:v", "1", "-vf", "scale=1280:720", "-q:v", "2", str(f)], check=True)
        sub = next((s["text"] for s in edit["subtitles"] if s["t0"] <= c["t"] <= s["t1"]), None)
        index.append(dict(file=f.name, t=round(c["t"], 2), kind=c["kind"], scene=c["scene"], why=c["why"], subtitle=sub))
    (outdir / "index.json").write_text(json.dumps(index, indent=1))
    kinds = {}
    for c in picks:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    print(f"review frames: {len(picks)} in {outdir.relative_to(vdir)} {kinds}")


if __name__ == "__main__":
    main()
