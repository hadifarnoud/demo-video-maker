#!/usr/bin/env bash
# Flag sampled frames that contain error-red UI (HSV threshold). Exit 1 when any are flagged.
#   lib/record/redscan.sh <video.mp4> [samples-per-second=2]
# RED_FRAC (default 0.004) = share of pixels that must be error-red to flag a frame.
# RED_ALLOW (JSON, optional) = [[t0, t1, frac, "why"], ...]: in that time range the limit is `frac` instead
#   (documented allowance for intentional red product UI such as the "only holder" badges; a real error toast
#   or alert banner is much larger and still trips it). Frames above the default but within an allowance are
#   counted as "allowed" and listed.
set -euo pipefail
IN="$1"; SPS="${2:-2}"
PY=python3; command -v uv >/dev/null 2>&1 && PY="uv run --frozen --no-sync python"
RED_FRAC="${RED_FRAC:-0.004}" $PY - "$IN" "$SPS" <<'PYEOF'
import json, os, sys, cv2, numpy as np
path, sps = sys.argv[1], float(sys.argv[2])
frac = float(os.environ["RED_FRAC"])
cap = cv2.VideoCapture(path)
fps = cap.get(cv2.CAP_PROP_FPS) or 30
step = max(1, round(fps / sps))
allow = json.loads(os.environ.get("RED_ALLOW") or "[]")
flagged, allowed, i, n = [], [], 0, 0
while True:
    ok = cap.grab()
    if not ok:
        break
    if i % step == 0:
        ok, f = cap.retrieve()
        f = cv2.resize(f, (480, int(480 * f.shape[0] / f.shape[1])), interpolation=cv2.INTER_AREA)
        h = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
        red = (((h[..., 0] <= 8) | (h[..., 0] >= 172)) & (h[..., 1] >= 140) & (h[..., 2] >= 130)).mean()
        n += 1
        t = i / fps
        lim = max([frac] + [a[2] for a in allow if a[0] <= t < a[1]])
        if red > lim:
            flagged.append((round(t, 2), round(float(red), 4)))
        elif red > frac:
            allowed.append((round(t, 2), round(float(red), 4)))
    i += 1
print(f"redscan {path}: {n} frames sampled, {len(flagged)} flagged" + (f", {len(allowed)} red-but-allowed (max {max(r for _, r in allowed)})" if allowed else ""))
for t, r in flagged:
    print(f"  t={t}s red={r}")
sys.exit(1 if flagged else 0)
PYEOF
