#!/usr/bin/env python3
"""Static checks for the terminal-native web mockups (stdlib only).

Run from anywhere:

    python3 docs/design/terminal-native/web/check_mockups.py

1. Every #hex colour literal in web/*.html is one of the hex values in
   ../system/tokens.json (case-insensitive).
2. No external reference: no http(s):// or protocol-relative URL in src/href/
   url()/@import, no <link rel=stylesheet>, no <script src>.
3. Each page carries a dark and a light theme block, a prefers-reduced-motion
   rule and the ?reduced= predicate.
4. Each page carries at least one zh-CN text run of 30+ characters (the
   390px wrap test needs one).
5. Text on an accent fill (.btn-primary and anything inside it) is
   ink.onAccent, and the ink.onAccent / surface.accent pair is >= 4.5:1 in both
   themes.
Exit status 0 when every check passes.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parent
TOKENS = WEB.parent / "system" / "tokens.json"
HEX = re.compile(r"#[0-9A-Fa-f]{3,8}\b")
# an id selector or fragment such as href="#x" is not a colour: only count hex
# runs of length 3/4/6/8 that sit in a CSS value or an inline style
COLOUR_CTX = re.compile(r"(?::|,|\(|\s)\s*(#[0-9A-Fa-f]{3,8})\b")
CJK = re.compile(r"[\u4e00-\u9fff]")
EXTERNAL = re.compile(
    r"""(?:src|href|action|poster)\s*=\s*["']?\s*(?:https?:)?//|url\(\s*["']?\s*(?:https?:)?//|@import|<link[^>]+rel=["']?stylesheet|<script[^>]+src=""",
    re.I,
)


def token_hexes() -> set[str]:
    data = json.loads(TOKENS.read_text(encoding="utf-8"))
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            for m in HEX.findall(node):
                if len(m) in (4, 5, 7, 9):
                    found.add(m.upper())

    walk(data)
    return found


def themes() -> dict:
    return json.loads(TOKENS.read_text(encoding="utf-8"))["themes"]


def luminance(hex_: str) -> float:
    h = hex_.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    out = []
    for i in (0, 2, 4):
        c = int(h[i : i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = out
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def main() -> int:
    allowed = token_hexes()
    failures: list[str] = []
    pages = sorted(WEB.glob("*.html"))
    total_hex = 0
    for page in pages:
        text = page.read_text(encoding="utf-8")
        # strip HTML attributes that hold fragment links (href="#x") before the scan
        scan = re.sub(r'href="#[^"]*"', "", text)
        hexes = [m for m in COLOUR_CTX.findall(scan) if len(m) in (4, 5, 7, 9)]
        total_hex += len(hexes)
        bad = sorted({h for h in hexes if h.upper() not in allowed})
        if bad:
            failures.append(f"{page.name}: hex not in tokens.json: {', '.join(bad)}")
        for m in EXTERNAL.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            failures.append(f"{page.name}:{line}: external reference: {m.group(0)!r}")
        body = re.sub(r"<script>[\s\S]*?</script>", "", text[text.find("<body"):])
        longest = max((len(CJK.findall(seg)) for seg in re.split(r"<[^>]+>", body)), default=0)
        if longest < 30:
            failures.append(f"{page.name}: no zh-CN string of 30+ characters in the data (longest {longest})")
        if '[data-theme="light"]' not in text:
            failures.append(f"{page.name}: no light theme block")
        if "prefers-reduced-motion" not in text or 'get("reduced")' not in text:
            failures.append(f"{page.name}: reduced motion not honoured (media query + ?reduced=)")
        if re.search(r"\.btn-primary[^{,]*\{[^}]*(?<![-\w])color:var\(--ink-(?!on-accent)", text):
            failures.append(f"{page.name}: .btn-primary text is not ink.onAccent")
        if not re.search(r":focus-visible\{\s*outline:var\(--border-focus\) solid var\(--ink-focus\)", text):
            failures.append(f"{page.name}: no 2px ink.focus ring on :focus-visible")
    t = themes()
    for name in ("dark", "light"):
        fg = t[name]["ink"]["onAccent"]
        bg = t[name]["surfaces"]["accent"]
        ratio = contrast(fg, bg)
        print(f"contrast {name}: ink.onAccent {fg} on surface.accent {bg} = {ratio:.2f}:1")
        if ratio < 4.5:
            failures.append(f"{name}: ink.onAccent on surface.accent is {ratio:.2f}:1 < 4.5:1")
        er = contrast(t[name]["ink"]["error"], t[name]["surfaces"]["canvas"])
        print(f"contrast {name}: ink.error on surface.canvas (reject button) = {er:.2f}:1")
        if er < 4.5:
            failures.append(f"{name}: ink.error on surface.canvas is {er:.2f}:1 < 4.5:1")
    print(f"pages: {len(pages)}  colour literals scanned: {total_hex}  allowed hex in tokens.json: {len(allowed)}")
    for f in failures:
        print("FAIL", f)
    print("OK" if not failures else f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
