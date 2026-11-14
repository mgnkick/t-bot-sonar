#!/usr/bin/env python3
"""Safely add an idempotent link to the guide hub in the existing site navigation."""

from datetime import datetime
import os
from pathlib import Path
import shutil
import stat
import sys


site_root = Path(sys.argv[1] if len(sys.argv) > 1 else "/var/www/wifi-sonar")
index = site_root / "index.html"
if not index.is_file():
    raise SystemExit(f"Site homepage not found: {index}")

with index.open("r", encoding="utf-8", newline="") as source:
    html = source.read()

marker = 'href="/guides/"'
if marker in html:
    print("Guide link already present in homepage navigation")
    raise SystemExit(0)

nav_end = html.lower().find("</nav>")
if nav_end < 0:
    raise SystemExit("Could not find a navigation block; homepage left unchanged")

link = '<a href="/guides/" aria-label="Практические инструкции по Wi-Fi">Инструкции</a>'
updated = html[:nav_end] + link + html[nav_end:]
stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = index.with_name(f"index.html.bak-before-guides-{stamp}")
shutil.copy2(index, backup)

temp = index.with_name("index.html.guides-tmp")
with temp.open("w", encoding="utf-8", newline="") as destination:
    destination.write(updated)
os.chmod(temp, stat.S_IMODE(index.stat().st_mode))
os.replace(temp, index)
print(f"Added guide navigation link; backup: {backup}")
