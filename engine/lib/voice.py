#!/usr/bin/env python3
"""Narration -> audio + char timings + STT check; optional music bed.

  uv run lib/voice.py <video> [scene] [--force] [--music] [--recheck]

Reads <video>/narration.txt (line N = scene N) and <video>/video.json.
Writes <video>/audio/scene-NN.mp3, scene-NN.json (per-char timings), music.mp3.
--recheck re-scores the saved takes from their saved STT JSON (STT only if absent).
The API key comes from ELEVENLABS_API_KEY only. It is never logged or written.
"""
import base64
import difflib
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API = "https://api.elevenlabs.io"
UA = "OpenAI File Downloader, XaiImageApiFetch/1.0"
ROOT = Path(__file__).resolve().parent.parent
LABEL_RE = re.compile(r"«([^»]+)»")
TAG_RE = re.compile(r"\[[^\]]*\]")


def key() -> str:
    k = os.environ.get("ELEVENLABS_API_KEY", "")
    if not k:
        sys.exit("ELEVENLABS_API_KEY is not set")
    return k


def call(path, body=None, form=None, query="", timeout=180):
    """One API call. Returns (content_type, bytes). Never echoes headers."""
    headers = {"xi-api-key": key(), "User-Agent": UA}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["content-type"] = "application/json"
    elif form is not None:
        boundary = uuid.uuid4().hex
        parts = []
        for name, val in form.items():
            if isinstance(val, tuple):  # (filename, bytes, mime)
                fn, blob, mime = val
                parts.append(
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                    f'filename="{fn}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + blob + b"\r\n"
                )
            else:
                parts.append(
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode()
                )
        data = b"".join(parts) + f"--{boundary}--\r\n".encode()
        headers["content-type"] = f"multipart/form-data; boundary={boundary}"
    req = urllib.request.Request(API + path + query, data=data, headers=headers, method="POST" if data else "GET")
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.headers.get("content-type", ""), r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < 5:  # busy or rate limited: wait and retry
                time.sleep(10 * (attempt + 1))
                continue
            raise RuntimeError(f"{path} -> HTTP {e.code}: {e.read()[:300].decode(errors='replace')}") from None
        except (http.client.HTTPException, urllib.error.URLError, OSError) as e:  # a dropped connection, not a refusal
            if attempt == 5:
                raise RuntimeError(f"{path} -> connection failed: {type(e).__name__}") from None
            time.sleep(3 * (attempt + 1))


# Persian: STT and the script may spell the same word with Arabic or Persian letters (ي/ی, ك/ک), with or without
# a zero-width non-joiner (می‌شود / میشود) or diacritics, and write numbers with Persian or Latin digits.
FA_FOLD = str.maketrans(
    {"ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه", "أ": "ا", "إ": "ا", "آ": "ا", "ؤ": "و", "\u200c": "", "\u0640": ""}
    | {chr(c): "" for c in [*range(0x064B, 0x0656), 0x0670]}
    | {chr(0x06F0 + i): str(i) for i in range(10)}
    | {chr(0x0660 + i): str(i) for i in range(10)}
)
FA_RE = re.compile(r"[\u0600-\u06FF]")


def norm(s: str) -> str:
    """Letters and digits only, lowercase: robust to punctuation and spacing."""
    return re.sub(r"[\W_]+", "", s.translate(FA_FOLD).lower())


NONWORD_RE = re.compile(r"[^\w\s]+")
NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}
_FA_ONES = "صفر یک دو سه چهار پنج شش هفت هشت نه ده یازده دوازده سیزده چهارده پانزده شانزده هفده هجده نوزده".split()
_FA_TENS = {2: "بیست", 3: "سی", 4: "چهل", 5: "پنجاه", 6: "شصت", 7: "هفتاد", 8: "هشتاد", 9: "نود"}


def _int_words(n: int, fa: bool = False) -> str:
    ones, tens = (_FA_ONES, _FA_TENS) if fa else (_ONES, _TENS)
    if n < 20:
        return ones[n]
    if n < 100:
        t, r = divmod(n, 10)
        return tens[t] + ((" و " if fa else " ") + ones[r] if r else "")
    if n == 100:
        return "صد" if fa else "one hundred"
    return str(n)  # out of range: keep the digits


def _num_words(m, fa: bool = False) -> str:
    t = m.group(0)
    if "." in t:
        i, frac = t.split(".")
        return " ".join([_int_words(int(i), fa), "ممیز" if fa else "point"] + [_int_words(int(d), fa) for d in frac])
    return _int_words(int(t), fa)


def _norm(s: str, words: bool) -> str:
    """NFKC + lowercase, punctuation and dashes -> spaces, spaces collapsed, Persian letters and digits folded;
    with `words`, digits are mapped to words (STT writes them out), in Persian when the text is Persian."""
    fa = bool(FA_RE.search(s))
    s = unicodedata.normalize("NFKC", s).translate(FA_FOLD).lower()
    if words:
        s = NUM_RE.sub(lambda m: f" {_num_words(m, fa)} ", s)  # padded: glued digits detach
    return " ".join(NONWORD_RE.sub(" ", s).split())


def norm_words(s: str) -> str:
    return _norm(s, True)


def norm_plain(s: str) -> str:
    return _norm(s, False)


def label_heard(label: str, transcript: str) -> bool:
    """True when the label was spoken: both sides normalised (digits -> English
    words, punctuation gone); the digit form of the transcript is also accepted."""
    if norm_words(label) in norm_words(transcript) or norm_plain(label) in norm_plain(transcript):
        return True
    if FA_RE.search(label):
        # Persian: STT splits or joins words (می شود / میشود) and spells the ezafe three ways (نسخهٔ / نسخه‌ی / نسخه).
        # STT also spells loanwords and colloquial forms its own way (دموراژ / دماراژ, چه کار / چکار), so a near match
        # counts; but a word that may carry a negation prefix (ن, نا, نمی, غیر) must be heard exactly, or a take that
        # drops it (ناموفق heard as موفق) would pass.
        def words(x):
            return re.sub(r"ه(?:ی|ای)(?= |$)", "ه", norm_words(x)).split()

        lw, tw = words(label), words(transcript)
        if any(w.startswith(("ن", "غیر")) and w not in tw for w in lw):
            return False
        want, hay = "".join(lw), "".join(tw)
        if want in hay:
            return True
        k = len(want)
        return k >= 6 and any(
            difflib.SequenceMatcher(None, want, hay[i : i + w], autojunk=False).ratio() >= 0.85
            for w in (k - 1, k, k + 1)
            for i in range(0, max(1, len(hay) - w + 1))
        )
    # long labels: tolerate a one-letter STT spelling difference of a name (Rajaee / Rajaie)
    want, hay = norm_plain(label), norm_plain(transcript)
    if len(want) < 30:
        return False
    for k in (len(want) - 1, len(want), len(want) + 1):
        for i in range(0, max(1, len(hay) - k + 1)):
            if difflib.SequenceMatcher(None, want, hay[i : i + k]).ratio() >= 0.97:
                return True
    return False


def probe_seconds(mp3: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp3)],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(out.strip())


def split_line(line: str, default_tags: str):
    """-> (spoken text without tags, tags string to send before it)."""
    m = re.match(r"\s*((?:\[[^\]]*\]\s*)+)(.*)$", line, re.S)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    return line.strip(), default_tags


def strip_tags(chars, starts, ends):
    """Drop [tag] characters (and the space after a tag) from the alignment."""
    out_c, out_s, out_e = [], [], []
    depth = 0
    prev_closed = False
    for c, s, e in zip(chars, starts, ends):
        if c == "[":
            depth += 1
            continue
        if depth:
            if c == "]":
                depth -= 1
                prev_closed = depth == 0
            continue
        if prev_closed and c == " ":
            prev_closed = False
            continue
        prev_closed = False
        out_c.append(c)
        out_s.append(s)
        out_e.append(e)
    return "".join(out_c), out_s, out_e


def apply_respell(spoken: str, respell: dict):
    """Voice-only respelling (video.json voice.respell, e.g. {"Praxabase": "Prax-uh-base"}), case-insensitive.
    -> (text to send, [(orig_start, orig_end, sent_start, sent_end)]) so the alignment can be mapped back."""
    if not respell:
        return spoken, []
    pat = re.compile("|".join(re.escape(k) for k in sorted(respell, key=len, reverse=True)), re.I)
    out, spans, last, sent_len = [], [], 0, 0
    lower = {k.lower(): v for k, v in respell.items()}
    for m in pat.finditer(spoken):
        out.append(spoken[last : m.start()])
        sent_len += m.start() - last
        r = lower[m.group(0).lower()]
        spans.append((m.start(), m.end(), sent_len, sent_len + len(r)))
        out.append(r)
        sent_len += len(r)
        last = m.end()
    out.append(spoken[last:])
    return "".join(out), spans


def voice_text(cfg, n: int, spoken: str):
    """What the voice reads for scene n -> (text to send, spans onto `spoken`). video.json voice.lines[n] is a whole-line
    override written by the review sheet (vowel marks, a rewording of the voice only); otherwise voice.respell applies."""
    line = cfg["voice"].get("lines", {}).get(str(n))
    if line:
        ops = difflib.SequenceMatcher(None, spoken, line, autojunk=False).get_opcodes()
        return line, [(i1, i2, j1, j2) for tag, i1, i2, j1, j2 in ops if tag != "equal"]
    return apply_respell(spoken, cfg["voice"].get("respell", {}))


def take_sig(cfg, send: str) -> str:
    """A saved take is current when this matches: changing the words or the voice settings makes it stale."""
    v = cfg["voice"]
    return hashlib.sha256(
        json.dumps([send, v["model_id"], v["voice_id"], v["language_code"], v.get("stability", 0.3), v.get("similarity_boost", 0.8)] + ([v["speed"]] if v.get("speed") else [])).encode()
    ).hexdigest()[:16]


def write_take(cfg, a: Path, n: int, audio: bytes, sig, text, tags, spoken, starts, ends, transcript, model, ratio, missing):
    """Make this take scene n's current audio, in the layout the recorder and the editor read."""
    a.mkdir(parents=True, exist_ok=True)
    mp3_p = a / f"scene-{n:02d}.mp3"
    mp3_p.write_bytes(audio)
    (a / f"scene-{n:02d}.json").write_text(json.dumps({
        "sig": sig, "scene": n, "text": text, "tags": tags, "line": spoken,
        "starts": starts, "ends": ends,
        "duration": probe_seconds(mp3_p),
        "stt": {"model": model, "transcript": transcript, "ratio": round(ratio, 4), "missing_labels": missing},
        "voice": {k: cfg["voice"][k] for k in ("model_id", "voice_id", "language_code")},
    }, ensure_ascii=False))


def unrespell(spoken, sent, spans, starts, ends):
    """Map the alignment of the respelled text back onto the real text: the characters of a respelled word share
    the time span of its respelling, so subtitles keep the real spelling."""
    if not spans:
        return spoken, starts, ends
    if len(starts) != len(sent):
        raise RuntimeError(f"alignment has {len(starts)} chars, sent text has {len(sent)}")
    ns, ne, o, k = [], [], 0, 0
    for a, b, c, d in spans:
        # plain characters before the word map 1:1
        for i in range(a - o):
            ns.append(starts[k + i]); ne.append(ends[k + i])
        k += a - o
        t0 = starts[min(c, len(starts) - 1)]  # c == d: words removed from the voice share one instant
        t1 = ends[d - 1] if d > c else t0
        n = b - a
        for i in range(n):
            ns.append(t0 + (t1 - t0) * i / n); ne.append(t0 + (t1 - t0) * (i + 1) / n)
        k = d
        o = b
    for i in range(len(spoken) - o):
        ns.append(starts[k + i]); ne.append(ends[k + i])
    if len(ns) != len(spoken):
        raise RuntimeError("respell mapping produced the wrong length")
    return spoken, ns, ne


def tts(cfg, text_with_tags):
    v = cfg["voice"]
    body = {
        "text": text_with_tags,
        "model_id": v["model_id"],
        "language_code": v["language_code"],
        "voice_settings": {
            "stability": v.get("stability", 0.3),
            "similarity_boost": v.get("similarity_boost", 0.8),
        },
    }
    if v.get("speed"):  # 0.7..1.2; Persian runs ~20 % longer than the English script
        body["voice_settings"]["speed"] = v["speed"]
    _, raw = call(
        f"/v1/text-to-speech/{v['voice_id']}/with-timestamps", body=body,
        query="?output_format=mp3_44100_128",
    )
    d = json.loads(raw)
    return base64.b64decode(d["audio_base64"]), d["alignment"]


def stt(cfg, mp3: bytes):
    """Transcript text. Tries the configured model, then older scribe models."""
    lang = cfg["voice"]["language_code"]
    models = [cfg["voice"].get("stt_model", "scribe_v2"), "scribe_v1"]
    last = None
    for m in dict.fromkeys(models):
        try:
            _, raw = call(
                "/v1/speech-to-text",
                form={"model_id": m, "language_code": lang, "file": ("take.mp3", mp3, "audio/mpeg")},
            )
            return json.loads(raw)["text"], m
        except RuntimeError as e:
            last = e
    raise last


def check_take(spoken, transcript, must_hear=()):
    """-> (score 0..1, ratio, missing). Score = text similarity minus a penalty per miss. `must_hear` (video.json
    voice.must_hear, e.g. ["Praxabase"]): brand words that appear in the line and must be heard as spelled
    (a space inside the word is accepted: "Praxa base")."""
    # digits -> words on both sides: STT writes numbers out, and a digit mismatch must not cost extra takes
    heard = norm(norm_words(transcript))
    missing = [l for l in LABEL_RE.findall(spoken) if not label_heard(l, transcript)]
    missing += [w for w in must_hear if w.lower() in spoken.lower() and norm(w) not in heard]
    want = norm(norm_words(LABEL_RE.sub(lambda m: m.group(1), spoken)))
    ratio = difflib.SequenceMatcher(None, want, heard, autojunk=False).ratio()
    return ratio - 0.2 * len(missing), ratio, missing


def voice_scene(cfg, vdir: Path, n: int, line: str, force: bool):
    a = vdir / "audio"
    a.mkdir(exist_ok=True)
    mp3_p, js_p = a / f"scene-{n:02d}.mp3", a / f"scene-{n:02d}.json"
    v = cfg["voice"]
    spoken, tags = split_line(line, v.get("tags", ""))
    tts_text, spans = voice_text(cfg, n, spoken)
    send = (tags + " " + tts_text).strip()
    sig = take_sig(cfg, send)
    if not force and mp3_p.exists() and js_p.exists() and json.loads(js_p.read_text()).get("sig") == sig:
        print(f"scene {n}: unchanged, skip")
        return
    best = None
    for take in range(1, v.get("takes", 3) + 1):
        audio, al = tts(cfg, send)
        text, ts, te = strip_tags(al["characters"], al["character_start_times_seconds"], al["character_end_times_seconds"])
        text, ts, te = unrespell(spoken, tts_text, spans, ts, te)
        tmp = a / f".take-{n:02d}.mp3"
        tmp.write_bytes(audio)
        try:
            transcript, model = stt(cfg, audio)
            score, ratio, missing = check_take(spoken, transcript, v.get("must_hear", ()))
        except RuntimeError as e:
            print(f"scene {n} take {take}: STT failed ({e}); keeping take")
            score, ratio, missing, transcript, model = 0.5, 0.5, [], "", None
        print(f"scene {n} take {take}: score {score:.3f} text {ratio:.3f} missing labels {missing}")
        if best is None or score > best["score"]:
            best = dict(score=score, ratio=ratio, missing=missing, transcript=transcript, stt_model=model,
                        audio=audio, text=text, starts=ts, ends=te, tmp=tmp)
        if not missing and ratio >= 0.93:
            break
    for p in a.glob(".take-*.mp3"):
        p.unlink()
    write_take(cfg, a, n, best["audio"], sig, best["text"], tags, spoken, best["starts"], best["ends"],
               best["transcript"], best["stt_model"], best["ratio"], best["missing"])


def recheck_scene(cfg, vdir: Path, n: int, line: str):
    """Re-score a saved take from its saved STT JSON; calls STT only if it was not saved."""
    a = vdir / "audio"
    js_p, mp3_p = a / f"scene-{n:02d}.json", a / f"scene-{n:02d}.mp3"
    if not js_p.exists() or not mp3_p.exists():
        print(f"scene {n}: no saved take, skip")
        return
    d = json.loads(js_p.read_text())
    spoken, _ = split_line(line, "")
    s = d.get("stt") or {}
    if not s.get("transcript"):
        s["transcript"], s["model"] = stt(cfg, mp3_p.read_bytes())
    score, ratio, missing = check_take(spoken, s["transcript"], cfg["voice"].get("must_hear", ()))
    s.update(ratio=round(ratio, 4), missing_labels=missing)
    d["stt"] = s
    js_p.write_text(json.dumps(d, ensure_ascii=False))
    print(f"scene {n}: score {score:.3f} text {ratio:.3f} missing labels {missing}")


def music(cfg, vdir: Path, force: bool, total_seconds: float):
    m = cfg.get("music") or {}
    if not m.get("prompt"):
        sys.exit("video.json has no music.prompt")
    out = vdir / "audio" / "music.mp3"
    out.parent.mkdir(exist_ok=True)
    # never shorter than the video estimate: a longer script (01-tour-fa) outgrew music.seconds; the bed is never looped
    secs = max(10.0, float(m.get("seconds") or 0), total_seconds + 3)
    if out.exists() and not force and probe_seconds(out) >= secs - 3:  # the API may return a little less than asked
        print("music: exists, skip")
        return
    print(f"music: {secs:.0f}s")
    ctype, raw = call(
        "/v1/music",
        body={"prompt": m["prompt"], "music_length_ms": int(secs * 1000), "force_instrumental": True},
        query="?output_format=mp3_44100_128", timeout=600,
    )
    if "json" in ctype:
        sys.exit(f"music API returned JSON: {raw[:300]!r}")
    out.write_bytes(raw)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        sys.exit(__doc__)
    vdir = Path(args[0])
    vdir = (vdir if vdir.is_dir() else ROOT / args[0]).resolve()
    only = int(args[1]) if len(args) > 1 else None
    cfg = json.loads((vdir / "video.json").read_text())
    lines = (vdir / "narration.txt").read_text().splitlines()
    force = "--force" in flags
    recheck = "--recheck" in flags
    for n, line in enumerate(lines, 1):
        if line.strip() and (only is None or only == n):
            if recheck:
                recheck_scene(cfg, vdir, n, line)
            else:
                voice_scene(cfg, vdir, n, line, force)
    if "--music" in flags and not recheck:
        total = sum(
            json.loads(p.read_text())["duration"] + 1.0
            for p in sorted((vdir / "audio").glob("scene-*.json"))
        ) + float(cfg.get("intro_card", 3)) + float(cfg.get("outro_card", cfg.get("intro_card", 3)))
        music(cfg, vdir, force, total)


if __name__ == "__main__":
    main()
