#!/usr/bin/env python3
"""Static scan of a video's spec files: a DOM edit must go through s.rewrite() or s.mask(), which log it.

  python3 lib/specscan.py <video dir>        exit 1 and one line per raw edit

Why: label() asserts against the page as edited, so a spec that rewrites text with page.evaluate can show a word the product
never rendered, and nobody reviewing the video can tell. The declared helpers log selector, before, after and the reason.
Read-only evaluate (scrollIntoView, reading innerText) is fine; the rules below flag writes and injected code.
"""
import re
import sys
from pathlib import Path

SOURCE = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
OUTPUT = {"work", "out", "clips", "audio", "state", "drift-shots"}  # run output, never spec code
RULES = [
    ("injected code", r"\b(addInitScript|addScriptTag|addStyleTag)\b|\bnew Function\b|\beval\("),
    ("network rewrite", r"\.route\(|\brouteFromHAR\b|\.fulfill\(|\.setContent\("),
    ("object write", r"\bObject\.(assign|defineProperty)\(|\bhistory\.(pushState|replaceState)\("),
    ("text or markup write", r"(\.|\[['\"])(textContent|innerText|innerHTML|outerHTML|nodeValue|data|title)(['\"]\])?\s*[-+*|]?=(?!=)"),
    ("value or property write", r"\.(value|hidden|disabled|checked|src|href|className)\s*[-+*|]?=(?!=)|\bset\.call\(|\.style\b|\.classList\.(add|remove|toggle|replace)\("),
    ("attribute write", r"\.(setAttribute|removeAttribute|toggleAttribute)\("),
    ("node insert or remove", r"\.(remove|removeChild|replaceChild|replaceWith|replaceChildren|before|after|append|prepend|appendChild|insertBefore|insertAdjacent\w*|attachShadow)\(|\bcreateElement\(|\bdocument\.write\("),
    ("timer that keeps editing", r"\bsetInterval\(|\bdispatchEvent\("),
]


def strip_comments(src):
    """Remove // and /* */ comments, leaving strings and template literals alone."""
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c in "'\"`":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            out.append(src[i : j + 1])
            i = j + 1
        elif c == "\\":  # an escape outside a string: the \/\/ in a regex literal is not a comment
            out.append(src[i : i + 2])
            i += 2
        elif src.startswith("//", i):
            while i < n and src[i] != "\n":
                i += 1
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("\n" * src.count("\n", i, j))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def scan(vdir):
    """-> [(file, line, rule, snippet)] for every raw DOM edit in the video folder's scripts (subfolders too, not run output; a shared helper outside the folder is not read)."""
    hits = []
    root = Path(vdir)
    for f in sorted(p for p in root.rglob("*") if p.suffix in SOURCE and p.is_file() and not OUTPUT & set(p.relative_to(root).parts)):
        for no, line in enumerate(strip_comments(f.read_text()).splitlines(), 1):
            for rule, rx in RULES:
                m = re.search(rx, line)
                if m:
                    hits.append((str(f.relative_to(vdir)), no, rule, line.strip()[:90]))
    return hits


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    hits = scan(sys.argv[1])
    for f, no, rule, snip in hits:
        print(f"[raw-dom-edit] {f}:{no} {rule}: {snip}")
    if hits:
        print("Declare the edit with s.rewrite(selector, {prop, from, to, why}) or s.mask(selector, {after, why}).", file=sys.stderr)
    sys.exit(1 if hits else 0)


if __name__ == "__main__":
    main()
