#!/usr/bin/env python3
"""Listen and fix: a local page to change what the voice reads, hear the new take at once, and keep the take you approve.

  python3 lib/review.py <video dir> <media dir> [--port 8765]

Binds 127.0.0.1 only. The ElevenLabs key comes from ELEVENLABS_API_KEY and never reaches the page.
A kept take becomes scene-NN.mp3/.json in <media dir>/audio with the signature voice.py computes, so the audio
you approved is the audio that ships: voice.py skips it as unchanged. review.json lists the scenes to re-record.
"""
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import voice as V  # noqa: E402

HERE = Path(__file__).resolve().parent / "review"
TOKEN = secrets.token_urlsafe(24)
LOCK = threading.Lock()
MAX_TEXT = 5000


class Bad(Exception):
    pass


class Video:
    """One video's files: the sources in the repo, the audio and takes in the media folder."""

    def __init__(self, vdir: Path, media: Path):
        self.vdir, self.media = vdir, media
        self.audio, self.takes = media / "audio", media / "takes"

    def cfg(self):
        return json.loads((self.vdir / "video.json").read_text())

    def lines(self):
        return (self.vdir / "narration.txt").read_text().splitlines()

    def line(self, n):
        lines = self.lines()
        if not 1 <= n <= len(lines) or not lines[n - 1].strip():
            raise Bad(f"no scene {n}")
        return lines, V.split_line(lines[n - 1], self.cfg()["voice"].get("tags", ""))

    def review(self):
        p = self.media / "review.json"
        return json.loads(p.read_text()) if p.exists() else {"changed": []}

    def take_list(self, n):
        d = self.takes / f"scene-{n:02d}"
        return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.is_dir() else []

    def state(self):
        cfg = self.cfg()
        name = cfg["output"]
        edit_p = self.media / "out" / f"{name}.edit.json"
        subs = json.loads(edit_p.read_text())["subtitles"] if edit_p.exists() else []
        scenes = []
        for n, line in enumerate(self.lines(), 1):
            if not line.strip():
                continue
            spoken, _ = V.split_line(line, cfg["voice"].get("tags", ""))
            text, _ = V.voice_text(cfg, n, spoken)
            js = self.audio / f"scene-{n:02d}.json"
            scenes.append({
                "n": n, "subtitle": spoken, "voice": text,
                "audio": f"/audio/{n}" if js.exists() else None,
                "stt": json.loads(js.read_text()).get("stt") if js.exists() else None,
                "takes": self.take_list(n),
                "rows": [dict(t0=s["t0"], t1=s["t1"], text=s["text"]) for s in subs if s["item"] == f"s{n:02d}"],
            })
        return {
            "name": name, "dir": "rtl" if V.FA_RE.search(" ".join(self.lines())) else "ltr",
            "video": (self.media / "out" / f"{name}.mp4").exists(),
            "changed": self.review()["changed"], "scenes": scenes,
        }

    def voice(self, n, text):
        text = clean(text, "voice")
        cfg = self.cfg()
        _, (spoken, tags) = self.line(n)
        before, _ = V.voice_text(cfg, n, spoken)
        audio, al = V.tts(cfg, (tags + " " + text).strip())
        sent, ts, te = V.strip_tags(al["characters"], al["character_start_times_seconds"], al["character_end_times_seconds"])
        d = self.takes / f"scene-{n:02d}"
        d.mkdir(parents=True, exist_ok=True)
        with LOCK:
            k = 1 + max([int(p.stem) for p in d.glob("*.json")], default=0)
            (d / f"{k:03d}.mp3").write_bytes(audio)
            take = {"take": k, "voice": text, "sent": sent, "starts": ts, "ends": te, "settings": V.take_sig(cfg, ""),
                    "start_at": play_from(before, sent, ts), "chars": len(text), "at": int(time.time())}
            (d / f"{k:03d}.json").write_text(json.dumps(take, ensure_ascii=False))
        return take

    def keep(self, n, k, subtitle):
        subtitle = clean(subtitle, "subtitle")
        take_p = self.takes / f"scene-{n:02d}" / f"{int(k):03d}.json"
        if not take_p.exists():
            raise Bad("no such take")
        take = json.loads(take_p.read_text())
        audio = take_p.with_suffix(".mp3").read_bytes()
        cfg = self.cfg()
        if take.get("settings") != V.take_sig(cfg, ""):
            raise Bad("this take used other voice settings: voice it again")
        transcript, model = V.stt(cfg, audio)  # slow: outside the lock, so another keep can run meanwhile
        score, ratio, missing = V.check_take(subtitle, transcript, cfg["voice"].get("must_hear", ()))
        with LOCK:
            cfg = self.cfg()  # read again: another scene may have been kept during the speech-to-text call
            lines, (_, tags) = self.line(n)
            overrides = cfg["voice"].setdefault("lines", {})
            overrides.pop(str(n), None)
            # voice.lines holds this scene only when respell alone would not produce what was voiced
            if V.voice_text(cfg, n, subtitle)[0] != take["sent"]:
                overrides[str(n)] = take["sent"]
            if not overrides:
                del cfg["voice"]["lines"]
            # a line that had no tags of its own keeps following voice.tags
            explicit = lines[n - 1].lstrip().startswith("[")
            lines[n - 1] = f"{tags} {subtitle}" if explicit else subtitle
            _, spans = V.voice_text(cfg, n, subtitle)
            text, ts, te = V.unrespell(subtitle, take["sent"], spans, take["starts"], take["ends"])
            # audio first: a failure here leaves the sources untouched, so nothing goes stale and re-rolls
            V.write_take(cfg, self.audio, n, audio, V.take_sig(cfg, (tags + " " + take["sent"]).strip()), text, tags,
                         subtitle, ts, te, transcript, model, ratio, missing)
            atomic(self.vdir / "narration.txt", "\n".join(lines) + "\n")
            atomic(self.vdir / "video.json", json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
            rv = self.review()
            rv["changed"] = sorted(set(rv["changed"]) | {n})
            atomic(self.media / "review.json", json.dumps(rv))
        return {"ratio": round(ratio, 3), "missing": missing, "transcript": transcript, "changed": rv["changed"]}


def clean(text: str, what: str) -> str:
    """One line per scene: a pressed Enter must not shift every later scene."""
    text = " ".join(text.split())
    if not text or len(text) > MAX_TEXT:
        raise Bad(f"the {what} text is empty or too long")
    if text.startswith("["):
        raise Bad(f"the {what} text cannot start with [ : that marks delivery tags")
    return text


def atomic(p: Path, text: str):
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, p)


def play_from(before: str, sent: str, starts) -> float:
    """Start of the sentence holding the first changed character, so a retry plays the words you just changed."""
    i = next((k for k, (a, b) in enumerate(zip(before, sent)) if a != b), min(len(before), len(sent)))
    i = min(i, len(sent) - 1)
    while i > 0 and sent[i - 1] not in ".!?؟":
        i -= 1
    while i < len(sent) - 1 and sent[i] == " ":
        i += 1
    return round(starts[i], 2) if starts else 0.0


def handler(video: Video, port: int):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send_json(self, code, obj):
            self.send(code, "application/json", json.dumps(obj, ensure_ascii=False).encode())

        def send(self, code, ctype, data, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")  # another site must not frame the page to borrow a click on Voice
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def send_file(self, p: Path, ctype):
            size = p.stat().st_size
            m = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
            if not m:
                self.send_response(200)
                a, b = 0, size - 1
            else:
                a, b = int(m[1]), min(int(m[2] or size - 1), size - 1)
                if a >= size:
                    return self.send(416, "text/plain", b"", {"Content-Range": f"bytes */{size}"})
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(b - a + 1))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(p, "rb") as f:
                f.seek(a)
                left = b - a + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def do_GET(self):
            # a page on another site must not reach this server, directly or through a rebound DNS name
            if self.headers.get("Host") not in hosts:
                return self.send(403, "text/plain", b"wrong host")
            path = self.path.split("?")[0]
            try:
                if path == "/":
                    html = (HERE / "sheet.html").read_text().replace("__TOKEN__", TOKEN)
                    return self.send(200, "text/html; charset=utf-8", html.encode())
                if path == "/font":
                    return self.send(200, "font/woff2", (HERE / "vazirmatn.woff2").read_bytes())
                if path == "/api/state":
                    return self.send_json(200, video.state())
                if path == "/video":
                    return self.send_file(video.media / "out" / f"{video.cfg()['output']}.mp4", "video/mp4")
                m = re.fullmatch(r"/audio/(\d+)", path)
                if m:
                    return self.send_file(video.audio / f"scene-{int(m[1]):02d}.mp3", "audio/mpeg")
                m = re.fullmatch(r"/take/(\d+)/(\d+)", path)
                if m:
                    return self.send_file(video.takes / f"scene-{int(m[1]):02d}" / f"{int(m[2]):03d}.mp3", "audio/mpeg")
            except FileNotFoundError:
                pass
            self.send(404, "text/plain", b"not found")

        def do_POST(self):
            if self.headers.get("Host") not in hosts or self.headers.get("X-Token") != TOKEN:
                return self.send(403, "text/plain", b"forbidden")
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > 4 * MAX_TEXT * 4:
                    raise Bad("request too large")
                b = json.loads(self.rfile.read(length))
                if self.path == "/api/voice":
                    return self.send_json(200, video.voice(int(b["scene"]), str(b["voice"])))
                if self.path == "/api/keep":
                    return self.send_json(200, video.keep(int(b["scene"]), int(b["take"]), str(b["subtitle"])))
                self.send(404, "text/plain", b"not found")
            except (Bad, KeyError, ValueError) as e:
                self.send_json(400, {"error": str(e)})
            except RuntimeError as e:  # an API refusal or a dropped connection, never holds the key
                self.send_json(502, {"error": str(e)})
            except Exception as e:  # the page must always get an answer; the type alone, a message could quote a file
                self.send_json(500, {"error": f"Something broke on the server: {type(e).__name__}"})

    return H


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ConnectionError):  # a browser leaving mid-stream is normal when seeking
            super().handle_error(request, client_address)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 2:
        sys.exit(__doc__)
    if not os.environ.get("ELEVENLABS_API_KEY"):
        sys.exit("ELEVENLABS_API_KEY is not set")
    if not shutil.which("ffprobe"):
        sys.exit("ffprobe is not installed: a kept take cannot be measured")
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8765
    if "--port" in sys.argv:
        args = [a for a in args if a != str(port)]
    video = Video(Path(args[0]).resolve(), Path(args[1]).expanduser().resolve())
    video.media.mkdir(parents=True, exist_ok=True)
    print(f"http://127.0.0.1:{port}/", flush=True)
    Server(("127.0.0.1", port), handler(video, port)).serve_forever()


if __name__ == "__main__":
    main()
