#!/usr/bin/env python3
"""Text size pre-check: renders a sample subtitle and callout with the video's theme and prints their size at 720p.

  python lib/textsize.py <video>

Same measurement and same limit as the gate's "readable at 720p" item (gate.min_text_px_720, default 18), but needs no clips, so a
theme change is caught before an edit. Exit 1 when any overlay text is under the limit.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from gate import text_size_check  # noqa: E402


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    vdir = (Path(sys.argv[1]) if Path(sys.argv[1]).is_dir() else ROOT / sys.argv[1]).resolve()
    cfg = json.loads((vdir / "video.json").read_text())
    with tempfile.TemporaryDirectory() as tmp:
        plan = dict(outDir=tmp, fps=30, theme=cfg.get("theme", {}), cards=[], subtitles=[dict(id="s0", rtl=False, parts=[dict(t="text", s="Sample subtitle")])],
                    callouts=[dict(id="c0", label="Label", hint="Hint")])
        (Path(tmp) / "plan.json").write_text(json.dumps(plan))
        subprocess.run(["node", str(ROOT / "lib/render-overlays.mjs"), str(Path(tmp) / "plan.json")], check=True, capture_output=True)
        text_px = json.loads((Path(tmp) / "index.json").read_text())["text_px"]
    ok, detail = text_size_check(text_px, cfg.get("gate", {}).get("min_text_px_720", 18))
    print(("PASS " if ok else "FAIL ") + "overlay text at 720p: " + json.dumps(detail))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
