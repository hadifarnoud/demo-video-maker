#!/usr/bin/env python3
"""GIF from the finished mp4: palette built from the clip itself, no new dependency (the image's ffmpeg).

  python lib/gif.py <video> [--start 0] [--dur 8] [--width 720] [--fps 12] [--subs]

Cuts from out/<name>-nosubs.mp4 (--subs: the burned-in out/<name>.mp4), writes out/<name>.gif, prints its size. A GIF has no audio and
no soft captions, so use --subs when the words matter. Keep it short: size grows with width, fps and length.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    flags = sys.argv[1:]

    def opt(k, d):
        return float(flags[flags.index(k) + 1]) if k in flags else d

    vdir = (Path(args[0]) if Path(args[0]).is_dir() else ROOT / args[0]).resolve()
    name = json.loads((vdir / "video.json").read_text()).get("output", vdir.name)
    src = vdir / "out" / (f"{name}.mp4" if "--subs" in flags else f"{name}-nosubs.mp4")
    if not src.exists():
        sys.exit(f"{src} missing: edit first")
    dst = vdir / "out" / f"{name}.gif"
    vf = f"fps={opt('--fps', 12):g},scale={int(opt('--width', 720))}:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{opt('--start', 0):g}", "-t", f"{opt('--dur', 8):g}", "-i", str(src), "-an", "-vf", vf, "-loop", "0", str(dst)], check=True)
    print(f"{dst.relative_to(vdir)} {dst.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
