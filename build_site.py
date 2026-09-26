#!/usr/bin/env python3
"""
Build the static site for GitHub Pages into _site/.

web/index.html is written without <!doctype>/<head>/<body> (the Claude
artifact viewer adds that skeleton when it publishes the page), so here it
gets a standalone document around it.  The other pages are copied as they are.

Usage: python build_site.py [--out _site]
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

WEB = Path(__file__).parent / "web"
PAGES = ["cabin3d.html", "annotate.html"]
ASSETS = ["model.js", "data.js", "mc-worker.js"]

HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<style>
  :root { color-scheme: light; padding: env(safe-area-inset-top, 0px) 0 env(safe-area-inset-bottom, 0px); }
  body { margin: 0; }
  img { max-width: 100%; }
  [hidden] { display: none !important; }
</style>
"""


def wrap(page: str) -> str:
    """Move the leading <title>/<link>/<style> into <head>; the rest is <body>."""
    split = page.index('<div class="wrap">')
    return HEAD + page[:split] + "</head>\n<body>\n" + page[split:] + "</body>\n</html>\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="_site")
    out = Path(ap.parse_args().out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "index.html").write_text(wrap((WEB / "index.html").read_text()))
    for name in PAGES + ASSETS:
        shutil.copy2(WEB / name, out / name)
    print(f"Built {out}/: {', '.join(sorted(p.name for p in out.iterdir()))}")


if __name__ == "__main__":
    main()
