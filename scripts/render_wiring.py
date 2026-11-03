#!/usr/bin/env python3
"""Render the wiring sheet. Requires Pillow, DejaVu Sans and Chromium on Linux.

Run from any directory: python3 scripts/render_wiring.py
Uses a temporary browser profile; never accesses a user's profile.
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

root = Path(__file__).resolve().parents[1]
out = root / "docs" / "images"
browser = shutil.which("chromium") or shutil.which("chromium-browser")
if not browser:
    raise SystemExit("Chromium is required to render the PNG.")
subprocess.run([sys.executable, str(root / "scripts" / "draw_wiring.py")], check=True)
with tempfile.TemporaryDirectory(prefix="wiring-render-") as scratch:
    wrapper = Path(scratch) / "index.html"
    # Inline the SVG so the renderer needs no network or file access beyond this page.
    svg = (out / "pico-i2c-wiring.svg").read_text(encoding="utf-8")
    svg = svg[svg.index("<svg"):]
    wrapper.write_text('<!doctype html><meta charset="utf-8">'
                       '<style>html,body{margin:0;padding:0;background:#fff;}'
                       'svg{display:block}</style>' + svg, encoding="utf-8")
    command = [browser, "--headless", "--disable-gpu", "--hide-scrollbars",
               "--no-first-run", "--disable-background-networking",
               "--force-device-scale-factor=2", "--window-size=2000,2820",
               f"--user-data-dir={scratch}/profile",
               f"--screenshot={out / 'pico-i2c-wiring.png'}", wrapper.as_uri()]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        command.insert(1, "--no-sandbox")  # only this trusted, locally generated SVG
    subprocess.run(command, check=True, timeout=60, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE)
with Image.open(out / "pico-i2c-wiring.png") as image:
    assert image.size == (4000, 5640), image.size
    print(f"Rendered {image.size[0]} x {image.size[1]} PNG and editable SVG in {out}")
