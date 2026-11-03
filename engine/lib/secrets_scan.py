"""Secret scan over recorded text: the same patterns and entropy rule as the recorder (secret-patterns.json).

A hit is (kind, where). The matched text is never returned or printed.
"""
import json
import math
import re
from pathlib import Path

CFG = json.loads((Path(__file__).resolve().parent / "secret-patterns.json").read_text())
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
SKIP_KEYS = {"voice_sig", "sig"}  # hashes of the voice take, not secrets


def bits(s):
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in (s.count(ch) for ch in set(s)))


def load_allow(cfg):
    """video.json secrets.allow: [{"pattern": regex tested against the matched text, "why": reason}]. A reason is required."""
    out = []
    for a in (cfg.get("secrets") or {}).get("allow", []):
        if not a.get("why") or not a.get("pattern"):
            raise SystemExit("video.json secrets.allow needs a pattern and a why for every entry")
        out.append(re.compile(a["pattern"]))
    return out


def kinds_in(text, allow=()):
    """-> sorted kinds of secret found in one string."""
    if not isinstance(text, str) or len(text) < 8:
        return []
    found = set()

    def hit(kind, frag):
        if not any(a.search(frag) for a in allow):
            found.add(kind)

    for p in CFG["patterns"]:
        for m in re.finditer(p["re"], text):
            hit(p["kind"], m.group(0))
    en = CFG["entropy"]
    for m in re.finditer(en["run"], text):
        r = m.group(0)
        if UUID.match(r):
            continue
        classes = sum(bool(re.search(c, r)) for c in ("[a-z]", "[A-Z]", "[0-9]", "[_+=]"))
        if len(r) >= en["min_len"] and classes >= en["min_classes"] and bits(r) >= en["min_bits"]:
            hit("high-entropy", r)
        elif re.fullmatch(r"[0-9a-fA-F]+", r) and len(r) >= en["hex_min_len"] and bits(r) >= en["hex_min_bits"]:
            hit("hex-run", r)
    return sorted(found)


def scan_json(obj, allow=(), where=""):
    """-> [(where, kind)] for every string inside a JSON value."""
    out = []
    if isinstance(obj, str):
        out += [(where, k) for k in kinds_in(obj, allow)]
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k not in SKIP_KEYS:
                out += scan_json(v, allow, f"{where}.{k}" if where else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += scan_json(v, allow, f"{where}[{i}]")
    return out
