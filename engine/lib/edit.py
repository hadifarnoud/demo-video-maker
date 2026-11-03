#!/usr/bin/env python3
"""Compositor: clips + voice -> finished demo video.

  uv run lib/edit.py <video> [--preview] [--plan]

--plan writes out/<name>.plan.json (subtitle positions, overlaps, zoom) and stops before any frame is rendered.

Reads <video>/video.json, narration.txt, audio/, clips/. Composites every frame with
OpenCV (1920x1080, 30 fps; --preview: 960x540, 15 fps), pipes it to ffmpeg, mixes the audio
and writes out/<name>.mp4 (with subtitles), out/<name>-nosubs.mp4 and out/<name>.edit.json.
"""
import json
import math
import re
import subprocess
import sys
import wave
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
FW, FH = 2560, 1440  # recorded frame
VW, VH = 1280, 720  # CSS viewport
SR = 44100
CLAUSE = ",;:.!?—…،؛؟"
TARGET_KINDS = ("click", "hover", "look", "type")
LABEL_RE = re.compile(r"«([^»]+)»")


# ---- small helpers ----------------------------------------------------------
def ease(u):
    u = min(1.0, max(0.0, u))
    return (1 - math.cos(math.pi * u)) / 2


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def blend(img, spr, x, y, alpha=1.0):
    """Alpha-blend a BGRA sprite onto img at (x, y); clips to the image."""
    h, w = spr.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(img.shape[1], x + w), min(img.shape[0], y + h)
    if x1 <= x0 or y1 <= y0 or alpha <= 0:
        return
    s = spr[y0 - y : y1 - y, x0 - x : x1 - x].astype(np.float32)
    a = (s[..., 3:4] / 255.0) * alpha
    roi = img[y0:y1, x0:x1].astype(np.float32)
    img[y0:y1, x0:x1] = (roi * (1 - a) + s[..., :3] * a).astype(np.uint8)


def intersects(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


class Clip:
    """Sequential frame reader; holds the last frame past the end."""

    def __init__(self, path):
        self.path = str(path)
        self.cap = cv2.VideoCapture(self.path)
        self.n = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.i = -1
        self.f = None

    def get(self, i):
        i = max(0, min(i, self.n - 1))
        if i == self.i:
            return self.f
        if i < self.i:  # rewind: reopen, seeking is not frame-exact on h264
            self.cap = cv2.VideoCapture(self.path)
            self.i = -1
        while self.i < i - 1:
            self.cap.grab()
            self.i += 1
        ok, f = self.cap.read()
        if ok:
            self.f = f
        self.i = i
        return self.f


# ---- camera (auto-zoom) -------------------------------------------------------
def zoom_for(b, pad=70):
    return min(VW / (b[2] - b[0] + 2 * pad), VH / (b[3] - b[1] + 2 * pad))


def build_camera(events, zcfg, zmax_default, zmin):
    """-> (groups, keyframes, returns). Keyframes: (t, cx, cy, z) in CSS px, eased with a cosine.
    returns: the times the camera is back at the full view inside the clip; each is one zoom cycle (in, hold, out).
    The camera stays zoomed and pans when the next target comes within zoom.stay seconds (default 4.5) of the last one."""
    if zcfg.get("off"):
        return [], [(0.0, VW / 2, VH / 2, 1.0)], []
    stay = zcfg.get("stay", 4.5)
    zmax = zcfg.get("max", zmax_default)
    LEAD, JOIN, HOLD_AFTER = 1.0, 2.2, 0.4
    groups = []
    for e in sorted((e for e in events if e.get("box") and e["kind"] in TARGET_KINDS), key=lambda e: e["t"]):
        b = e["box"]
        r = (b["x"], b["y"], b["x"] + b["width"], b["y"] + b["height"])
        g = groups[-1] if groups else None
        if g:
            u = (min(g["box"][0], r[0]), min(g["box"][1], r[1]), max(g["box"][2], r[2]), max(g["box"][3], r[3]))
            if e["t"] - g["t1"] <= JOIN and zoom_for(u) >= zmin:
                g["box"], g["t1"], g["wide"] = u, e["t"], g["wide"] or e.get("wide", False)
                continue
        groups.append(dict(t0=e["t"], t1=e["t"], box=r, wide=bool(e.get("wide"))))
    use = []
    for g in groups:
        zf = zoom_for(g["box"], 110 if g["wide"] else 70)
        if zf < zmin and not g["wide"]:
            g["z"] = 1.0  # target too large to zoom on
            continue
        g["z"] = max(1.1, min(zf, 1.3)) if g["wide"] else min(zf, zmax)
        g["c"] = ((g["box"][0] + g["box"][2]) / 2, (g["box"][1] + g["box"][3]) / 2)
        use.append(g)
    base = (VW / 2, VH / 2, 1.0)
    K = [(0.0, *base)]
    returns = []
    cur = base

    def move_time(a, b):
        return max(0.9, math.hypot(a[0] - b[0], a[1] - b[1]) / 550, abs(math.log(b[2] / a[2])) / 0.55)

    for i, g in enumerate(use):
        st = (*g["c"], g["z"])
        t_in = max(K[-1][0], g["t0"] - LEAD)
        if t_in > K[-1][0]:
            K.append((t_in, *cur))
        K.append((K[-1][0] + move_time(cur, st), *st))
        hold_end = max(K[-1][0], g["t1"] + HOLD_AFTER)
        K.append((hold_end, *st))
        g["t_in"], g["t_out"] = t_in, hold_end
        cur = st
        out_d = move_time(cur, base)
        nxt = use[i + 1] if i + 1 < len(use) else None
        if nxt is None or nxt["t0"] - g["t1"] > stay:
            K.append((hold_end + out_d, *base))
            returns.append(hold_end + out_d)
            cur = base
    return use, K, returns


def cut_clip_times(cuts):
    """Clip time at which each cut starts. A clip log keeps its cuts in wall seconds, and clip time skips the cuts before it."""
    out, gone = [], 0.0
    for a, b in sorted(cuts):
        out.append(a - gone)
        gone += b - a
    return out


def cam_at(K, t):
    if t <= K[0][0]:
        return K[0][1:]
    for a, b in zip(K, K[1:]):
        if t < b[0]:
            e = ease((t - a[0]) / (b[0] - a[0]))
            return (a[1] + (b[1] - a[1]) * e, a[2] + (b[2] - a[2]) * e, math.exp(math.log(a[3]) + (math.log(b[3]) - math.log(a[3])) * e))
    return K[-1][1:]


def cam_window(cx, cy, z, out_w):
    """-> (x0, y0, s): source-pixel window origin and scale source -> output."""
    w, h = FW / z, FH / z
    x0 = min(max(cx * 2 - w / 2, 0), FW - w)
    y0 = min(max(cy * 2 - h / 2, 0), FH - h)
    return x0, y0, out_w / w


def css_to_out(x, y, win):
    x0, y0, s = win
    return (x * 2 - x0) * s, (y * 2 - y0) * s


def warp(src, win, out_w, out_h):
    """Sub-pixel crop+scale of the 2x frame. Down-scaling goes through INTER_AREA first."""
    x0, y0, s = win
    if s < 1.0:
        pre = cv2.resize(src, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        M = np.float32([[1, 0, -x0 * s], [0, 1, -y0 * s]])
        return cv2.warpAffine(pre, M, (out_w, out_h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    M = np.float32([[s, 0, -x0 * s], [0, s, -y0 * s]])
    return cv2.warpAffine(src, M, (out_w, out_h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


# ---- subtitles --------------------------------------------------------------
def chunk_line(text, starts, ends, maxlen=66):
    """Split a spoken line into subtitle chunks at clause marks outside quoted labels.
    -> [{t0, t1, parts:[{t, s}], rtl}] with times relative to the voice start."""
    inside = [False] * len(text)
    for m in LABEL_RE.finditer(text):
        for i in range(m.start(), m.end()):
            inside[i] = True
    cuts = [0]
    for i, c in enumerate(text):
        if c in CLAUSE and not inside[i] and i + 1 < len(text) and text[i + 1] == " ":
            cuts.append(i + 1)
        # a label that ends its own sentence ("know.»") is a break point too
        if c == "»" and i > 0 and text[i - 1] in CLAUSE and i + 1 < len(text) and text[i + 1] == " ":
            cuts.append(i + 1)
    cuts.append(len(text))
    cuts = sorted(set(cuts))
    hard = set(cuts)  # breaks at a clause mark; split_long adds others, which the burned-in text may use but the caption cues may not
    spans = [(a, b) for a, b in zip(cuts, cuts[1:]) if text[a:b].strip()]

    def disp_len(a, b):
        return len(text[a:b].replace("«", "").replace("»", "").strip())

    def split_long(a, b):
        if disp_len(a, b) <= maxlen:
            return [(a, b)]
        mid, best = (a + b) / 2, None
        for i in range(a + 1, b):
            if text[i] == " " and not inside[i] and (best is None or abs(i - mid) < abs(best - mid)):
                best = i
        return [(a, b)] if best is None else split_long(a, best) + split_long(best, b)

    spans = [s for a, b in spans for s in split_long(a, b)]
    merged = []
    for a, b in spans:  # fold tiny chunks into the following one
        if merged and disp_len(*merged[-1]) < 14:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    if len(merged) > 1 and disp_len(*merged[-1]) < 14:
        merged[-2:] = [(merged[-2][0], merged[-1][1])]
    out = []
    for a, b in merged:
        seg = text[a:b]
        idx = [i for i in range(a, b) if text[i] not in " «»"]
        if not idx:
            continue
        parts, last = [], 0
        for m in LABEL_RE.finditer(seg):
            if m.start() > last:
                parts.append({"t": "text", "s": seg[last : m.start()]})
            parts.append({"t": "label", "s": m.group(1)})
            last = m.end()
        if last < len(seg):
            parts.append({"t": "text", "s": seg[last:]})
        parts[0]["s"] = parts[0]["s"].lstrip() if parts[0]["t"] == "text" else parts[0]["s"]
        if parts[-1]["t"] == "text":
            parts[-1]["s"] = parts[-1]["s"].rstrip()
        out.append({"t0": starts[idx[0]], "t1": ends[idx[-1]], "parts": parts, "rtl": bool(re.search(r"[؀-ۿ]", seg)), "hard": a in hard})
    for i, c in enumerate(out):
        nxt = out[i + 1]["t0"] if i + 1 < len(out) else None
        c["t0"] = max(0.0, c["t0"] - 0.08)
        # the fade-out (0.1 s) must be over before the next chunk starts (its t0 - 0.08): never two lines at once
        c["t1"] = min(c["t1"] + 0.3, nxt - 0.08) if nxt is not None else c["t1"] + 0.45
    return out


# ---- sidecars: SRT/VTT, a soft subtitle track, chapters ----------------------------
LANG3 = {"en": "eng", "fa": "fas", "ar": "ara", "de": "deu", "fr": "fra", "es": "spa", "nl": "nld", "tr": "tur", "it": "ita", "pt": "por", "ru": "rus"}


def stamp(t, sep):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}{sep}{ms % 1000:03d}"


QUOTES = {"en": ("\u201c", "\u201d")}  # a label «like this» in the narration; other languages keep the narration's own « »
GUILLEMETS = ("\u00ab", "\u00bb")


def cue_text(parts, quotes=GUILLEMETS):
    """Plain caption text: a label keeps its quote marks (the burned-in text highlights it instead), and any bidi isolates are dropped (a player lays RTL out itself)."""
    q0, q1 = quotes
    return re.sub("[\u2066-\u2069]", "", "".join(f"{q0}{p['s']}{q1}" if p["t"] == "label" else p["s"] for p in parts)).strip()


def build_cues(subs, quotes):
    """One caption cue per clause, not per burned-in chunk: a chunk that split a long clause at a space joins the cue before it
    (on a new line), so a cue never breaks mid-clause (method.md: break only at clause marks outside quoted labels).
    -> [{item, t0, t1, text}]"""
    cues = []
    for s in subs:
        text = cue_text(s["parts"], quotes)
        if cues and not s.get("hard", True) and cues[-1]["item"] == s["item"]:
            cues[-1]["t1"] = s["t1"]
            cues[-1]["text"] += "\n" + text
        else:
            cues.append(dict(item=s["item"], t0=s["t0"], t1=s["t1"], text=text))
    return cues


def write_sidecars(cues, path_stem):
    """-> (srt path, vtt path)."""
    srt, vtt = [], ["WEBVTT", ""]
    for i, c in enumerate(cues, 1):
        srt += [str(i), f"{stamp(c['t0'], ',')} --> {stamp(c['t1'], ',')}", c["text"], ""]
        vtt += [f"{stamp(c['t0'], '.')} --> {stamp(c['t1'], '.')}", c["text"].replace("&", "&amp;").replace("<", "&lt;"), ""]  # a raw & or < is markup in WebVTT
    a, b = Path(str(path_stem) + ".srt"), Path(str(path_stem) + ".vtt")
    a.write_text("\n".join(srt))
    b.write_text("\n".join(vtt))
    return a, b


def chapter_meta(chapters, total):
    """ffmetadata text: one chapter per item, from its start to the next item's start."""
    out = [";FFMETADATA1"]
    for i, (t0, title) in enumerate(chapters):
        t1 = chapters[i + 1][0] if i + 1 < len(chapters) else total
        safe = re.sub(r"([=;#\\\n])", r"\\\1", title)
        out += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(round(t0 * 1000))}", f"END={max(int(round(t1 * 1000)), int(round(t0 * 1000)) + 1)}", f"title={safe}"]
    return "\n".join(out) + "\n"


# ---- main ---------------------------------------------------------------------
def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    preview = "--preview" in sys.argv
    plan_only = "--plan" in sys.argv
    if not args:
        sys.exit(__doc__)
    vdir = (Path(args[0]) if Path(args[0]).is_dir() else ROOT / args[0]).resolve()
    cfg = json.loads((vdir / "video.json").read_text())
    name = cfg.get("output", vdir.name)
    OUT_W, OUT_H, FPS = (960, 540, 15) if preview else (1920, 1080, 30)
    K_SCALE = OUT_W / 1920
    pacing = cfg.get("pacing", {})
    TAIL, GAP, XF = pacing.get("tail", 0.35), pacing.get("gap", 0.85), pacing.get("crossfade", 0.5)
    zcfg = cfg.get("zoom", {})
    ZMIN = zcfg.get("min", 1.35)
    work = vdir / "work"
    (vdir / "out").mkdir(exist_ok=True)
    work.mkdir(exist_ok=True)

    # -- items: intro card, scenes (or card scenes), outro card
    n_lines = len((vdir / "narration.txt").read_text().splitlines())
    items = []
    cards_cfg = cfg.get("cards", {})
    intro_s = float(cfg.get("intro_card", 0))
    outro_s = float(cfg.get("outro_card", intro_s))
    card_scenes = cfg.get("card_scenes", {})
    if intro_s:
        items.append(dict(kind="card", id="intro", seconds=intro_s, voice=None))
    for n in range(1, n_lines + 1):
        vp = vdir / "audio" / f"scene-{n:02d}.json"
        voice = json.loads(vp.read_text()) if vp.exists() else None
        if str(n) in card_scenes:
            cs = card_scenes[str(n)]
            items.append(dict(kind="card", id=f"cs{n:02d}", n=n, seconds=float(cs.get("seconds", 3)), voice=voice, voice_at=pacing.get("voice_at", 0.5), cfg=cs))
            continue
        cp = vdir / "clips" / f"scene-{n:02d}.json"
        if not cp.exists():
            continue
        log = json.loads(cp.read_text())
        items.append(dict(kind="scene", id=f"s{n:02d}", n=n, voice=voice, log=log, voice_at=log["voiceAt"], clip=Clip(vdir / "clips" / f"scene-{n:02d}.mp4")))
    if outro_s:
        items.append(dict(kind="card", id="outro", seconds=outro_s, voice=None))
    if not any(i["kind"] == "scene" for i in items):
        sys.exit("no clips found; record first")

    # -- pacing: keep length per item, then start times with crossfades
    for j, it in enumerate(items):
        v_end = it["voice_at"] + it["voice"]["duration"] if it.get("voice") else 0.0
        it["voice_end"] = v_end
        if it["kind"] == "scene":
            acts = [e["t"] for e in it["log"]["events"] if e["kind"] in ("click", "hover", "type", "look")]
            it["last_action"] = max(acts, default=0.0)
            keep = max(v_end + TAIL, it["last_action"] + 0.5)
        else:
            keep = max(it["seconds"], v_end + TAIL)
        nxt = items[j + 1] if j + 1 < len(items) else None
        if nxt is not None and v_end and nxt["kind"] == "scene":  # lines ~GAP apart
            keep = max(keep, v_end + GAP - nxt["voice_at"] + XF)
        it["keep"] = keep
    t = 0.0
    for it in items:
        it["start"] = t
        t += it["keep"] - XF
    total = items[-1]["start"] + items[-1]["keep"]
    n_frames = int(math.ceil(total * FPS))

    # -- per-scene camera
    for it in items:
        if it["kind"] != "scene":
            continue
        n = it["n"]
        zc = {**{k: v for k, v in zcfg.items() if not k.isdigit()}, **zcfg.get(str(n), {})}
        it["groups"], it["K"], it["returns"] = build_camera(it["log"]["events"], zc, zcfg.get("max", 1.75), ZMIN)
        P = np.array(it["log"]["path"], dtype=np.float64) if it["log"]["path"] else np.zeros((0, 3))
        it["P"] = P
        it["cut_ct"] = cut_clip_times(it["log"].get("cuts", []))

    # -- zoom stability: one cycle = the camera zooms in and is back at the full view. A clip that ends zoomed cycles at its end,
    # where the crossfade returns to the full view of the next scene.
    def zoom_stats(it):
        travel = sum(abs(math.log(b[3] / a[3])) for a, b in zip(it["K"], it["K"][1:]))
        times = [round(it["start"] + min(r, it["keep"]), 2) for r in it["returns"]]
        return dict(cycles=len(times), travel=round(travel, 2), cycle_times=times)

    zoom_cycle_times = sorted(t for it in items if it["kind"] == "scene" for t in zoom_stats(it)["cycle_times"])

    # -- subtitle chunks (timeline time)
    subs = []
    for it in items:
        v = it.get("voice")
        if not v:
            continue
        for c in chunk_line(v["text"], v["starts"], v["ends"]):
            subs.append(dict(item=it["id"], n=it.get("n"), t0=it["start"] + it["voice_at"] + c["t0"], t1=it["start"] + it["voice_at"] + c["t1"], parts=c["parts"], rtl=c["rtl"]))
    for i, s in enumerate(subs):
        s["id"] = f"sub{i:03d}"

    # -- overlays via Chromium
    cards = []
    theme = cfg.get("theme", {})
    for it in items:
        if it["kind"] != "card":
            continue
        base = cards_cfg.get(it["id"], it.get("cfg", {}))
        cards.append(dict(id=it["id"], seconds=it["keep"], brand=cards_cfg.get("brand", ""), title=base.get("title", ""), subtitle=base.get("subtitle", ""), url=base.get("url", ""), mark=base.get("mark", True)))
    callouts = []
    for it in items:
        if it["kind"] == "scene":
            for k, e in enumerate(e for e in it["log"]["events"] if e["kind"] == "callout"):
                e["cid"] = f"c{it['n']:02d}{k}"
                callouts.append(dict(id=e["cid"], label=e["label"], hint=e["hint"]))
    ov = work / f"overlays-{FPS}"
    ov.mkdir(exist_ok=True)
    plan = dict(renderer=3, outDir=str(ov), fps=FPS, theme=theme, cards=cards, subtitles=[dict(id=s["id"], rtl=s["rtl"], parts=s["parts"]) for s in subs], callouts=callouts)
    plan_s = json.dumps(plan, sort_keys=True)
    ph = ov / "plan.json"
    if not (ph.exists() and ph.read_text() == plan_s and (ov / "index.json").exists()):
        ph.write_text(plan_s)
        run(["node", str(ROOT / "lib/render-overlays.mjs"), str(ph)])
    index = json.loads((ov / "index.json").read_text())

    def sprite(path, k=K_SCALE):
        im = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if k != 1:
            im = cv2.resize(im, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
        return im

    for s in subs:
        s["img"] = sprite(ov / "subs" / f"{s['id']}.png")
    pointer = sprite(ov / "pointer.png")
    hot = (round(6 * K_SCALE), round(4 * K_SCALE))
    card_frames = {}
    for c in cards:
        card_frames[c["id"]] = sorted((ov / "cards" / c["id"]).glob("*.png"))

    # -- subtitle position per scene (one per scene) + overlap checks
    SUB_MARGIN = round(56 * K_SCALE)
    sub_cfg = cfg.get("subtitles", {})
    sub_pos = {}
    ev_rects = {}  # item id -> [(t, kind, out rect)] used for overlap and callout placement

    # A label the line names is a target too (not a camera target). A label asserted before the voice says it is judged at the
    # moment of speaking, unless the page scrolled or changed in between.
    for it in items:
        if it["kind"] != "scene":
            continue
        moved = [e["t"] for e in it["log"]["events"] if e["kind"] in ("scroll", "click")]
        for e in it["log"]["events"]:
            if e["kind"] == "label":
                sp = e.get("spoken_t")
                e["te"] = sp if e.get("narrated") and sp and sp > e["t"] and not any(e["t"] < m <= sp for m in moved) else e["t"]

    def named_label(e):
        return e["kind"] != "label" or e.get("narrated")

    def event_rect(it, e, t=None):
        win = cam_window(*cam_at(it["K"], e["t"] if t is None else t), OUT_W)
        b = e["box"]
        x0, y0 = css_to_out(b["x"], b["y"], win)
        x1, y1 = css_to_out(b["x"] + b["width"], b["y"] + b["height"], win)
        return (x0, y0, x1, y1)

    for it in items:
        if it["kind"] == "scene":
            ev_rects[it["id"]] = [(e.get("te", e["t"]), e["kind"], event_rect(it, e, e.get("te", e["t"]))) for e in it["log"]["events"] if e.get("box") and named_label(e)]

    top_margin = cfg.get("subtitle_top_margin", {})  # per scene: keeps a top subtitle below a site nav bar

    def sub_rect(s, pos):
        h, w = s["img"].shape[:2]
        x = (OUT_W - w) // 2
        y = round(top_margin.get(str(s.get("n")), 56) * K_SCALE) if pos == "top" else OUT_H - SUB_MARGIN - h
        return (x, y, x + w, y + h)

    def overlaps_for(it, pos):
        out = []
        for s in (s for s in subs if s["item"] == it["id"]):
            r = sub_rect(s, pos)
            ct0, ct1 = s["t0"] - it["start"], s["t1"] - it["start"]
            for te, kind, er in ev_rects.get(it["id"], []):
                if ct0 - 1.0 <= te <= ct1 + 0.6 and intersects(r, er):
                    out.append(dict(sub=s["id"], event_t=te, kind=kind))
        return out

    for it in items:
        forced = sub_cfg.get(str(it.get("n")), sub_cfg.get("default"))
        order = [forced] if forced else ["bottom", "top"]
        best = None
        for pos in order:
            o = overlaps_for(it, pos) if it["kind"] == "scene" else []
            if best is None or len(o) < len(best[1]):
                best = (pos, o)
            if not o:
                break
        sub_pos[it["id"]] = best
    for s in subs:
        s["pos"] = sub_pos[s["item"]][0]
        s["rect"] = sub_rect(s, s["pos"])

    # -- named labels: where each one sits in the final picture (1920x1080 px), for the gate's pixel check
    label_rects = []
    for j, it in enumerate(items):
        if it["kind"] != "scene":
            continue
        lo = it["start"] + (XF if j else 0.0)  # inside a crossfade the picture is a blend: no stable box
        hi = items[j + 1]["start"] if j + 1 < len(items) else it["start"] + it["keep"]
        for e in it["log"]["events"]:
            if e["kind"] != "label" or not e.get("narrated"):
                continue
            samples = []
            for d in (0.0, 0.3):
                ct = e.get("te", e["t"]) + d
                if not lo <= it["start"] + ct < hi:
                    continue
                x0, y0, x1, y1 = (v / K_SCALE for v in event_rect(it, e, ct))
                samples.append(dict(t=round(it["start"] + ct, 3), rect=[round(x0), round(y0), round(x1), round(y1)]))
            if samples:
                label_rects.append(dict(scene=it["n"], label=e["label"], samples=samples))

    # -- callout placement: emptiest candidate corner, away from the action and the subtitles
    def place_callouts(it):
        placed = []
        cs = [e for e in it["log"]["events"] if e["kind"] == "callout"]
        for e in cs:
            w, h = (round(v * K_SCALE) for v in index["callouts"][e["cid"]])
            pos = sub_pos[it["id"]][0]
            band = round(250 * K_SCALE)
            m = round(72 * K_SCALE)
            ys = {"bottom": OUT_H - band - h, "top": SUB_MARGIN + round(190 * K_SCALE)}
            lower_y = ys["bottom"] if pos == "bottom" else OUT_H - round(150 * K_SCALE) - h
            upper_y = round(90 * K_SCALE) if pos == "bottom" else ys["top"]
            cands = {"bl": (m, lower_y), "br": (OUT_W - m - w, lower_y), "tl": (m, upper_y), "tr": (OUT_W - m - w, upper_y)}
            fr = it["clip"].get(round((e["t"] + 0.6) * 30))
            win = cam_window(*cam_at(it["K"], e["t"] + 0.6), OUT_W)
            view = warp(fr, win, OUT_W, OUT_H)
            edges = cv2.Canny(cv2.cvtColor(view, cv2.COLOR_BGR2GRAY), 60, 140)
            near = [er for te, _, er in ev_rects[it["id"]] if abs(te - e["t"]) < 4.0]
            best = None
            for k, (x, y) in cands.items():
                rect = (x, y, x + w, y + h)
                dens = float(edges[max(0, y) : y + h, max(0, x) : x + w].mean()) / 255
                pen = 10.0 * sum(intersects((rect[0] - 30, rect[1] - 30, rect[2] + 30, rect[3] + 30), er) for er in near)
                sc = dens + pen
                if best is None or sc < best[0]:
                    best = (sc, k, rect)
            e["rect"], e["pos"] = best[2], best[1]
            placed.append(dict(label=e["label"], pos=best[1], rect=list(best[2]), t=e["t"]))
        return placed

    for it in items:
        if it["kind"] == "scene":
            it["callouts_placed"] = place_callouts(it)
    callout_sprites = {cid: sprite(ov / "callouts" / f"{cid}.png") for cid in index["callouts"]}

    if plan_only:  # decisions only (subtitle positions, overlaps, zoom): no frames, no audio
        plan = dict(total=round(total, 3), subtitle_pos={k: v[0] for k, v in sub_pos.items()}, subtitle_overlaps={k: v[1] for k, v in sub_pos.items() if v[1]}, label_rects=label_rects,
                    zoom={i["id"]: [(round(g["t0"], 2), round(g["z"], 2)) for g in i["groups"]] for i in items if i["kind"] == "scene"},
                    zoom_cycle_times=zoom_cycle_times)
        (vdir / "out" / f"{name}.plan.json").write_text(json.dumps(plan, indent=1))
        print(json.dumps({k: plan[k] for k in ("total", "subtitle_pos", "subtitle_overlaps")}))
        return

    # -- frame rendering
    def draw_pointer(img, it, ct, win):
        P, presses = it["P"], it["log"]["presses"]
        if len(P):
            times = P[:, 0]
            idx = int(np.searchsorted(times, ct, side="right")) - 1
            if idx >= 0:
                idle = ct - times[idx]
                a = 1.0 if idle <= 1.0 else max(0.0, 1 - (idle - 1.0) / 0.3)
                j = idx
                while j > 0 and times[j] - times[j - 1] <= 0.15:
                    j -= 1
                if j == 0 or times[j] - times[j - 1] >= 1.3:  # first move after a long idle: fade in
                    a *= min(1.0, (ct - times[j]) / 0.15)
                if a > 0:
                    if idx + 1 < len(times) and times[idx + 1] - times[idx] <= 0.15:
                        f = (ct - times[idx]) / (times[idx + 1] - times[idx])
                        px = P[idx, 1] + (P[idx + 1, 1] - P[idx, 1]) * f
                        py = P[idx, 2] + (P[idx + 1, 2] - P[idx, 2]) * f
                    else:
                        px, py = P[idx, 1], P[idx, 2]
                    ox, oy = css_to_out(px, py, win)
                    blend(img, pointer, int(round(ox)) - hot[0], int(round(oy)) - hot[1], a)
        for tp, x, y in presses:
            u = (ct - tp) / 0.5
            end = min([tp + 0.5] + [c for c in it["cut_ct"] if c >= tp - 0.02])  # a cut follows the click: the ripple must not land on the next page
            if 0 <= u <= 1 and ct < end:
                ox, oy = css_to_out(x, y, win)
                r = int((8 + 34 * (1 - (1 - u) ** 3)) * K_SCALE)
                ov_ = img.copy()
                cv2.circle(ov_, (int(ox), int(oy)), r, (36, 20, 13), max(1, int(7 * K_SCALE)), cv2.LINE_AA)
                cv2.circle(ov_, (int(ox), int(oy)), r, (58, 255, 212), max(1, int(4 * K_SCALE)), cv2.LINE_AA)
                cv2.addWeighted(ov_, 0.6 * (1 - u), img, 1 - 0.6 * (1 - u), 0, img)

    def scene_img(it, ct, k):
        src = it["clip"].get(int(round(ct * 30)))
        cx, cy, z = cam_at(it["K"], ct)
        win = cam_window(cx, cy, z * k, OUT_W)
        img = warp(src, win, OUT_W, OUT_H)
        draw_pointer(img, it, ct, win)
        for e in it["log"]["events"]:
            if e["kind"] == "callout" and "rect" in e:
                u = ct - e["t"]
                if -0.01 <= u <= e["hold"] + 0.3:
                    a = min(1.0, max(0.0, u / 0.25)) * min(1.0, (e["hold"] + 0.3 - u) / 0.3)
                    slide = int((1 - min(1.0, max(0.0, u / 0.25))) * 14 * K_SCALE)
                    blend(img, callout_sprites[e["cid"]], e["rect"][0], e["rect"][1] + slide, a)
        return img

    def card_img(it, ct):
        fr = card_frames[it["id"]]
        im = cv2.imread(str(fr[min(len(fr) - 1, max(0, int(round(ct * FPS))))]))
        return im if im.shape[1] == OUT_W else cv2.resize(im, (OUT_W, OUT_H), interpolation=cv2.INTER_AREA)

    def item_img(it, t, k):
        ct = t - it["start"]
        return scene_img(it, ct, k) if it["kind"] == "scene" else card_img(it, ct)

    def frame_at(t):
        act = [j for j, it in enumerate(items) if it["start"] <= t < it["start"] + it["keep"]]
        if not act:
            act = [len(items) - 1]
        if len(act) == 1:
            return item_img(items[act[0]], t, 1.0)
        a, b = items[act[0]], items[act[1]]
        u = ease((t - b["start"]) / XF)
        A = item_img(a, t, 1.0 + 0.04 * u)
        B = item_img(b, t, 1.04 - 0.04 * u)
        return cv2.addWeighted(A, 1 - u, B, u, 0)

    def subs_overlay(img, t):
        for s in subs:
            if s["t0"] <= t < s["t1"]:
                a = min(1.0, (t - s["t0"]) / 0.1, (s["t1"] - t) / 0.1)
                blend(img, s["img"], s["rect"][0], s["rect"][1], max(0.0, a))

    # -- encode the picture twice (with and without subtitles)
    tag = ".preview" if preview else ""
    v_subs, v_nosubs = work / f"video-subs{tag}.mp4", work / f"video-nosubs{tag}.mp4"

    def encoder(path):
        return subprocess.Popen(
            ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{OUT_W}x{OUT_H}", "-r", str(FPS), "-i", "-",
             "-c:v", "libx264", "-preset", "veryfast" if preview else "medium", "-crf", "30" if preview else "18", "-pix_fmt", "yuv420p", "-r", str(FPS), str(path)],
            stdin=subprocess.PIPE,
        )

    pn = None if preview else encoder(v_nosubs)
    ps = encoder(v_subs)
    for f in range(n_frames):
        t = f / FPS
        img = frame_at(t)
        if pn:
            pn.stdin.write(img.tobytes())
        subs_overlay(img, t)
        ps.stdin.write(img.tobytes())
        if f % 150 == 0:
            print(f"frame {f}/{n_frames}", flush=True)
    for p in (pn, ps):
        if p:
            p.stdin.close()
            if p.wait():
                sys.exit("ffmpeg video encode failed")

    # -- audio: voice lines at their clip's voice start, music ducked under the voice
    n_samp = int(math.ceil((total + 0.5) * SR))
    voice = np.zeros(n_samp, np.float32)
    vad = []
    for it in items:
        if it.get("voice"):
            mp3 = vdir / "audio" / f"scene-{it['n']:02d}.mp3"
            pcm = np.frombuffer(run(["ffmpeg", "-v", "error", "-i", str(mp3), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"], capture_output=True).stdout, np.float32)
            o = int(round((it["start"] + it["voice_at"]) * SR))
            voice[o : o + len(pcm)] += pcm[: max(0, n_samp - o)]
            vad.append((it["start"] + it["voice_at"], it["start"] + it["voice_at"] + it["voice"]["duration"]))
    mix = voice.copy()
    music_info = None
    mus = vdir / "audio" / "music.mp3"
    mcfg = cfg.get("music") or {}
    if mus.exists() and mcfg:
        m = np.frombuffer(run(["ffmpeg", "-v", "error", "-i", str(mus), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"], capture_output=True).stdout, np.float32).copy()
        m = m[:n_samp]
        env_hz = 200
        n_env = int(total * env_hz) + env_hz
        duck = np.zeros(n_env, np.float32)
        for a, b in vad:
            duck[int(a * env_hz) : int(b * env_hz) + 1] = 1
        k = np.ones(int(0.35 * env_hz)) / int(0.35 * env_hz)  # attack/release smoothing
        duck = np.convolve(duck, k, mode="same")
        duck = np.minimum(1.0, duck * 1.6)
        g = 10 ** (mcfg.get("gain_db", -16) / 20)
        duck_g = 10 ** (mcfg.get("duck_db", -12) / 20)
        env = np.interp(np.arange(len(m)) / SR, np.arange(n_env) / env_hz, 1 - duck * (1 - duck_g)).astype(np.float32)
        fade_in, fade_out = int(0.8 * SR), int(min(2.5, len(m) / SR) * SR)
        env[:fade_in] *= np.linspace(0, 1, min(fade_in, len(env)))
        # fade out at the end of the picture, or at the end of the bed if it is shorter
        end = min(len(m), int(total * SR))
        env[max(0, end - fade_out) : end] *= np.linspace(1, 0, min(fade_out, end))
        env[end:] = 0
        mix[: len(m)] += m * env * g
        music_info = dict(seconds=round(len(m) / SR, 2), shorter_than_video=bool(len(m) / SR < total), gain_db=mcfg.get("gain_db", -16), duck_db=mcfg.get("duck_db", -12))
    peak = float(np.abs(mix).max()) or 1.0
    if peak > 0.9:
        mix *= 0.9 / peak
    wav = work / "mix.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(mix, -1, 1) * 32767).astype(np.int16).tobytes())
    ln = "loudnorm=I=-16:TP=-1.5:LRA=11"
    p1 = subprocess.run(["ffmpeg", "-v", "info", "-i", str(wav), "-af", ln + ":print_format=json", "-f", "null", "-"], capture_output=True, text=True, check=True)
    meas = json.loads(p1.stderr[p1.stderr.rindex("{") : p1.stderr.rindex("}") + 1])
    ln2 = f"{ln}:measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}:offset={meas['target_offset']}:linear=true"

    # chapters (one per item) in both files; the text as SRT/VTT beside them and as a soft track (off by default) in the clean file
    def chapter_title(it):
        if it["kind"] == "scene":
            return it["log"].get("title") or f"Scene {it['n']}"
        if it["id"] in ("intro", "outro"):
            return cards_cfg.get(it["id"], {}).get("title") or it["id"].capitalize()
        return it["cfg"].get("title") or f"Scene {it['n']}"

    chapters = [(it["start"], chapter_title(it)) for it in items]
    meta = work / "chapters.ffmeta"
    meta.write_text(chapter_meta(chapters, total))
    lang3 = LANG3.get(str(cfg.get("voice", {}).get("language_code", "")).split("-")[0], "und")
    srt = vtt = None
    quotes = QUOTES.get(str(cfg.get("voice", {}).get("language_code", "")).split("-")[0], GUILLEMETS)
    cues = build_cues(subs, quotes)
    if not preview:
        srt, vtt = write_sidecars(cues, vdir / "out" / name)

    outs = {}
    for label, vid, fname in ([("nosubs", v_nosubs, f"{name}-nosubs.mp4")] if not preview else []) + [("subs", v_subs, f"{name}{tag}.mp4")]:
        dst = vdir / "out" / fname
        soft = label == "nosubs" and srt is not None
        run(["ffmpeg", "-y", "-v", "error", "-i", str(vid), "-i", str(wav), "-i", str(meta)] + (["-i", str(srt)] if soft else []) + ["-map", "0:v", "-map", "1:a"] + (["-map", "3:s"] if soft else [])
            + ["-map_chapters", "2", "-c:v", "copy", "-af", ln2, "-ar", "48000", "-ac", "2", "-c:a", "aac", "-b:a", "192k"]
            + (["-c:s", "mov_text", "-metadata:s:s:0", f"language={lang3}", "-disposition:s:0", "0"] if soft else []) + ["-movflags", "+faststart", str(dst)])
        outs[label] = str(dst.relative_to(vdir))

    # -- decisions
    def cam_speed(it):
        ts = np.arange(0, it["keep"], 1 / 30)
        pts = np.array([cam_at(it["K"], x) for x in ts])
        pan = np.hypot(np.diff(pts[:, 0]), np.diff(pts[:, 1])).max() * 30 if len(pts) > 1 else 0
        zr = np.abs(np.diff(np.log(pts[:, 2]))).max() * 30 if len(pts) > 1 else 0
        return round(float(pan), 1), round(float(zr), 3)

    edit = dict(
        video=name, preview=preview, size=[OUT_W, OUT_H], fps=FPS, total=round(total, 3), crossfade=XF,
        items=[dict(id=i["id"], kind=i["kind"], scene=i.get("n"), start=round(i["start"], 3), keep=round(i["keep"], 3), voice_start=round(i["start"] + i["voice_at"], 3) if i.get("voice") else None,
                    voice_end=round(i["start"] + i["voice_end"], 3) if i.get("voice") else None,
                    clip_duration=i["log"]["duration"] if i["kind"] == "scene" else None,
                    held_last_frame=bool(i["kind"] == "scene" and i["keep"] > i["log"]["duration"] + 0.02),
                    last_action=round(i["last_action"], 3) if i["kind"] == "scene" else None) for i in items],
        zoom={i["id"]: dict(groups=[dict(t0=round(g["t0"], 2), t1=round(g["t1"], 2), z=round(g["z"], 2), center=[round(v) for v in g["c"]]) for g in i["groups"]],
                           max_pan_css_px_per_s=cam_speed(i)[0], max_zoom_log_per_s=cam_speed(i)[1], **zoom_stats(i)) for i in items if i["kind"] == "scene"},
        zoom_cycle_times=zoom_cycle_times,
        subtitles=[dict(id=s["id"], item=s["item"], t0=round(s["t0"], 2), t1=round(s["t1"], 2), pos=s["pos"], rect=[int(v) for v in s["rect"]], text="".join(p["s"] for p in s["parts"])) for s in subs],
        subtitle_overlaps={k: v[1] for k, v in sub_pos.items() if v[1]},
        subtitle_pos={k: v[0] for k, v in sub_pos.items()},
        label_rects=label_rects,
        text_px=index.get("text_px"),
        callouts={i["id"]: i["callouts_placed"] for i in items if i["kind"] == "scene" and i["callouts_placed"]},
        audio=dict(measured=meas, music=music_info), outputs=outs,
        sidecars=dict(srt=str(srt.relative_to(vdir)), vtt=str(vtt.relative_to(vdir)), cues=len(cues), cue_items=[c["item"] for c in cues], quotes=list(quotes), soft_track_in="nosubs" if srt else None, language=lang3, chapters=[dict(start=round(t0, 3), title=ti) for t0, ti in chapters]) if srt else dict(chapters=[dict(start=round(t0, 3), title=ti) for t0, ti in chapters]),
    )
    (vdir / "out" / f"{name}{tag}.edit.json").write_text(json.dumps(edit, indent=1))
    print(json.dumps(outs), f"total {total:.1f}s")


if __name__ == "__main__":
    main()
